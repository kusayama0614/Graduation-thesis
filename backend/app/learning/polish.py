"""生成した解説の文章を整える（文体・誤字・注意の重複と長さ）。

テンプレート（マスター解説の TeX）で個別化した解説では，書き足した部分の文体がマスター解説（常体）と違うと，
解説A・B を伏せて配っても，どちらが個別化したものか気づかれてしまう。また，同じ趣旨の注意の繰り返しや，
要点を詰め込んだ長い注意は読みにくい。そこで，

1. 決まった誤字（「下にとつ」→「下に凸」など）はプログラムで直す（LLM は呼ばない）
2. マスター解説や前に書いた注意と同じ趣旨の注意（chui 環境）は，プログラムで削る（LLM は呼ばない）
3. 敬体（です・ます）・長すぎる注意があるステップだけを，解説1件につき1回の LLM 呼び出しでまとめて直す。
   数式は ⟦式1⟧ のような印に置き換えて渡し，返ってきた文章に元の数式を戻す（LLM が数式を書き換えられない）。
   直しきれなかったステップがあれば，そのステップだけもう1回呼ぶ（解説1件につき最大2回）。
   それでも残った敬体は，地の文の語尾を機械的に常体に直す（「外します」→「外す」など）
4. 直した文章は，数式が1つでも変わっていたら採用しない（検算済みの計算を変えない）。図の印・TeX の形も確かめる
"""
from __future__ import annotations

import difflib
import re
from collections import Counter
from typing import Any, Dict, List, Optional

from app.learning import tex_template

# 決まった誤字の直し方（LLM は呼ばない）
TEXT_FIXES = [
    (re.compile(r'(上|下)に\s*と\s*つ'), r'\1に凸'),
    (re.compile(r'(上|下)にトツ'), r'\1に凸'),
]

_MATH = re.compile(r'\$[^$]+\$|\\\[.*?\\\]|\\begin\{(align|gather|equation)\*?\}.*?\\end\{\1\*?\}', re.S)
_CHUI = re.compile(r'\\begin\{chui\}(.*?)\\end\{chui\}', re.S)
# 敬体の語尾。後ろに何が続いても数える（「外します $|x|$」「なります\n」「しますが」なども敬体）
_POLITE = re.compile(r'でしょう|でした|です|ましょう|ました|ません|まして|ます|ください')
_MATH_MARK = re.compile(r'⟦式(\d+)⟧')
# 注意どうしの内容語（2文字の組）の重なり。短い方を基準にこの割合以上なら「同じ趣旨かもしれない」として LLM に確かめさせ，
# 長い方を基準にしても SAME_CAUTION 以上なら（ほぼ同じ文）プログラムで削る。
# 文字が似ていても趣旨が違う注意（「接するとき」と「共有点をもたないとき」）があるので，似ているだけでは削らない
SIMILAR_CAUTION = 0.5
SAME_CAUTION = 0.85
CONTAINED_CAUTION = 0.9
_FILLER = re.compile(r'注意|必要|忘れ|気をつけ|間違え|誤り|ミス')
MAX_POLISH_CALLS = 2
LONG_CAUTION_SENTENCES = 2
LONG_CAUTION_CHARS = 150


def fix_text(text: str) -> str:
    text = tex_template.clean_generated(text)
    for pattern, replacement in TEXT_FIXES:
        text = pattern.sub(replacement, text)
    return text


def plain_text(tex: str) -> str:
    """数式と TeX の命令を除いた地の文（文体や注意の内容を比べるため）。"""
    text = _MATH.sub(' ', tex)
    text = re.sub(r'\\[a-zA-Z]+\*?(\[[^\]]*\])?', ' ', text)
    return re.sub(r'[{}\s]+', ' ', text).strip()


def math_segments(tex: str) -> Counter:
    return Counter(re.sub(r'\s+', '', m.group(0)) for m in _MATH.finditer(tex))


def cautions(tex: str) -> List[str]:
    return [plain_text(c) for c in _CHUI.findall(tex)]


def template_cautions(template: Optional[Dict[str, Any]]) -> List[str]:
    """マスター解説（テンプレート）のこの小問の注意（固定の部分にある chui 環境）。"""
    if not template:
        return []
    return [c for segment in template['segments'] if segment['type'] == 'fixed' for c in cautions(segment['tex'])]


def _content_pairs(text: str, context: frozenset = frozenset()) -> set:
    """隣り合う2文字の組のうち，ひらがなだけでないもの（内容を表す組）。問題文にある組（context）は除く。"""
    text = re.sub(r'[\s、。，．,.:：;；()（）「」『』\[\]{}]', '', _FILLER.sub('', text))
    pairs = {text[i:i + 2] for i in range(len(text) - 1)}
    return {p for p in pairs if not re.fullmatch(r'[ぁ-ん]+', p)} - context


def context_pairs(problem: Optional[Dict[str, Any]]) -> frozenset:
    """問題文の内容語の組。問題の題材（「直線」「放物線」など）が同じだけの注意を，同じ趣旨とみなさないため。"""
    return frozenset(_content_pairs(plain_text(str((problem or {}).get('problem_text') or ''))))


def caution_overlap(a: str, b: str, context: frozenset = frozenset()):
    """注意どうしの内容語の重なり（短い方を基準，長い方を基準）。"""
    if not (a and b):
        return 0.0, 0.0
    pa, pb = _content_pairs(a, context), _content_pairs(b, context)
    if not pa or not pb:
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        return ratio, ratio
    common = len(pa & pb)
    return common / min(len(pa), len(pb)), common / max(len(pa), len(pb))


def _same(a: str, b: str, context: frozenset = frozenset()) -> bool:
    """同じ内容の注意（プログラムで削ってよい）: ほぼ同じ文か，一方の内容語がもう一方にほぼ全部含まれる。

    例:「両辺に $-1$ をかけると不等号の向きが逆になる」と「両辺に負の数をかけると不等号の向きが逆になる」。
    「接するとき」と「共有点をもたないとき」のように条件が違うものは，含まれない語があるので削らない。
    """
    shorter, longer = caution_overlap(a, b, context)
    size = min(len(_content_pairs(a, context)), len(_content_pairs(b, context)))
    return longer >= SAME_CAUTION or (shorter >= CONTAINED_CAUTION and size >= 5)


def _similar(a: str, b: str, context: frozenset = frozenset()) -> bool:
    """同じ趣旨かもしれない注意。例:「$-1$ をかけると不等号の向きが逆になる」と「負の数をかけると不等号の向きが逆になる」。"""
    return caution_overlap(a, b, context)[0] >= SIMILAR_CAUTION


SOFT_MARK = '（確認）'


def _issues(body: str, known_cautions: List[str], context: frozenset, soft: bool = True) -> List[str]:
    issues = []
    polite = _POLITE.findall(plain_text(body))
    if polite:
        issues.append(f'敬体（「です」「ます」「しましょう」など）が {len(polite)} か所ある。マスター解説と同じ常体（である調）にする')
    seen = list(known_cautions)
    for caution in cautions(body):
        if any(_same(caution, other, context) for other in seen):
            issues.append(f'注意「{caution[:40]}…」は，マスター解説や前に書いた注意と同じなので削る')
        elif soft and any(_similar(caution, other, context) for other in seen):
            other = next(o for o in seen if _similar(caution, o, context))
            issues.append(f'{SOFT_MARK}注意「{caution[:40]}…」は「{other[:40]}…」と似ている。同じ誤りへの注意（言い換え）なら削る。'
                          '別の場合や別の誤りへの注意なら，そのまま残す')
        sentences = [s for s in re.split(r'[。．]', caution) if s.strip()]
        if len(sentences) > LONG_CAUTION_SENTENCES or len(caution) > LONG_CAUTION_CHARS:
            issues.append(f'注意「{caution[:40]}…」は長すぎる。要点を1つに絞り，2文以内にする')
        seen.append(caution)
    return issues


def style_issues(body: str, known_cautions: List[str], context: frozenset = frozenset(), soft: bool = True) -> List[str]:
    """ステップの本文（書き直した TeX）の問題点。known_cautions はマスター解説や前に書いた注意。

    SOFT_MARK で始まるものは「似ている注意」で，削るかどうかは LLM に任せる（残しても問題として数えない）。
    """
    return _issues(body, known_cautions, context, soft)


def hard_issues(body: str, known_cautions: List[str], context: frozenset = frozenset()) -> List[str]:
    return _issues(body, known_cautions, context, soft=False)


def explanation_issues(bodies: List[str], known_cautions: List[str], context: frozenset = frozenset(),
                       checked: frozenset = frozenset(), soft: bool = True) -> Dict[int, List[str]]:
    """ステップの番号（0 から）ごとの問題点。前のステップの注意も「既に書いた注意」として数える。

    checked は，似ている注意を既に LLM に確かめさせたステップ（もう一度は確かめない）。
    """
    issues: Dict[int, List[str]] = {}
    seen = list(known_cautions)
    for index, body in enumerate(bodies):
        found = style_issues(body, seen, context, soft=soft and index not in checked)
        if found:
            issues[index] = found
        seen.extend(cautions(body))
    return issues


def drop_duplicate_cautions(bodies: List[str], known_cautions: List[str],
                            context: frozenset = frozenset()) -> List[str]:
    """マスター解説・前の小問・前のステップの注意とほぼ同じ文の注意（chui 環境）を削る（LLM は呼ばない）。"""
    seen = list(known_cautions)
    result = []
    for body in bodies:
        def replace(match):
            caution = plain_text(match.group(1))
            if any(_same(caution, other, context) for other in seen):
                return ''
            seen.append(caution)
            return match.group(0)
        new = _CHUI.sub(replace, body)
        if new != body:
            new = re.sub(r'\n{3,}', '\n\n', new).strip()
        result.append(new)
    return result


# ---------------------------------------------------------------- 敬体 → 常体（LLM で直しきれなかったときの最後の手段）

_KANJI = re.compile(r'[一-龥々]')
_HIRA = re.compile(r'[ぁ-ん]')
_GODAN = {  # い段 → (終止形, 過去形, 否定形の語幹)
    'し': ('す', 'した', 'さ'), 'き': ('く', 'いた', 'か'), 'ぎ': ('ぐ', 'いだ', 'が'), 'ち': ('つ', 'った', 'た'),
    'に': ('ぬ', 'んだ', 'な'), 'び': ('ぶ', 'んだ', 'ば'), 'み': ('む', 'んだ', 'ま'), 'り': ('る', 'った', 'ら'),
    'い': ('う', 'った', 'わ'),
}
# い段で終わるのに一段活用の動詞（「用います」→「用いる」）
_ICHIDAN_I = ('用い', '強い', '起き', '生き', '過ぎ', '落ち', '満ち', '足り', '借り', '降り', '浴び', '延び', '伸び', '帯び', 'でき')
_SAHEN_BEFORE = 'とにをくうがでもはよ'  # 「〜とします」「〜にします」「よくします」の「し」は「する」
_SAHEN_KANJI = '接属対関反有適達発課化熱察'  # 漢字1字でもサ変（「接します」→「接する」）


def _plain_verb(before: str, ending: str) -> Optional[str]:
    """「…外し」+「ます」→「…外す」のように，語尾の直前（before）と敬体の語尾から常体を作る。作れなければ None。"""
    form = {'ます': 0, 'ましょう': 0, 'ました': 1, 'ません': 2}[ending]
    if not before:
        return None
    last, prev, prev2 = before[-1], before[-2:-1], before[-3:-2]
    if last == 'し':
        sahen = (not prev or not (_KANJI.match(prev) or _HIRA.match(prev)) or prev in _SAHEN_BEFORE + _SAHEN_KANJI
                 or (_KANJI.match(prev) and prev2 and not _HIRA.match(prev2)))
        if sahen:  # 「計算し」「代入し」「⟦式1⟧とし」→ サ変
            return before[:-1] + ('する', 'した', 'しない')[form]
    if before.endswith('あり') and form == 2:
        return before[:-2] + 'ない'
    if (last in 'みき' or last == 'い') and prev in ('て', 'で'):  # 「〜てみます」「〜ています」「〜てきます」
        if last == 'き':
            return before[:-1] + ('くる', 'きた', 'こない')[form]
        return before + ('る', 'た', 'ない')[form]
    if _KANJI.match(last) or last in 'えけげせぜてでねべぺめれ' or before.endswith(_ICHIDAN_I) or last == 'じ':
        return before + ('る', 'た', 'ない')[form]  # 一段（「求め」「見」「用い」「生じ」）
    if last in _GODAN:
        dictionary, past, negative = _GODAN[last]
        if form == 1 and last == 'き' and before.endswith(('行き', 'いき')):
            past = 'った'
        return before[:-1] + (dictionary, past, negative + 'ない')[form]
    return None


_PLAIN_FIXED = [
    (re.compile(r'でしょう'), 'だろう'),
    (re.compile(r'でした'), 'であった'),
    (re.compile(r'です(?=[，、,])'), 'であり'),
    (re.compile(r'です'), 'である'),
    (re.compile(r'してください'), 'すること'),
]


def to_plain_style(text: str) -> str:
    """地の文の敬体を常体に機械的に直す（LLM で直しきれなかったときだけ使う。数式は印にしてから渡す）。

    「外します」→「外す」，「計算します」→「計算する」，「求めました」→「求めた」，「なりません」→「ならない」，
    「注意しましょう」→「注意する」，「範囲です」→「範囲である」。直し方がわからない語尾はそのまま残す。
    """
    out, pos = [], 0
    for match in re.finditer(r'ましょう|ました|ません|ます', text):
        if match.start() < pos:
            continue
        before = text[pos:match.start()]
        converted = _plain_verb(before, match.group(0))
        out.append(converted if converted is not None else before + match.group(0))
        pos = match.end()
    text = ''.join(out) + text[pos:]
    for pattern, replacement in _PLAIN_FIXED:
        text = pattern.sub(replacement, text)
    return text


def mask_math(tex: str):
    """数式を ⟦式1⟧ のような印に置き換える。戻り値: (置き換えた文, 元の数式のリスト)。"""
    found: List[str] = []

    def replace(match):
        found.append(match.group(0))
        return f'⟦式{len(found)}⟧'
    return _MATH.sub(replace, tex), found


def unmask_math(text: str, found: List[str]) -> Optional[str]:
    """印を元の数式に戻す。知らない印や，同じ印が2回出てくるときは None（採用しない）。"""
    numbers = [int(n) for n in _MATH_MARK.findall(text)]
    if len(numbers) != len(set(numbers)) or any(not 1 <= n <= len(found) for n in numbers):
        return None
    return _MATH_MARK.sub(lambda m: found[int(m.group(1)) - 1], text)


POLISH_SYSTEM = (
    'あなたは高等学校数学の解答解説の校正者です。指摘された問題だけを直し，内容（解き方・結果）は変えません。'
    '数式は ⟦式1⟧ のような印に置き換えてあります。印と ⟦図1⟧ のような印は，一字も変えず，順番も変えずにそのまま残します。'
    '新しい数式（$...$）は書き足しません。'
    '文は常体（である調）で書き，「です」「ます」「しましょう」「でしょう」は1つも残しません'
    '（例:「外します」→「外す」，「になります」→「になる」，「です」→「である」，「注意しましょう」→「注意する」）。'
    '注意（\\begin{chui} ... \\end{chui}）は，既に書かれた注意と同じ誤りへの注意（言い換え）なら削り，'
    '別の場合や別の誤りへの注意なら残します。長すぎるものは要点を1つに絞って2文以内にします。'
)


def _schema() -> Dict[str, Any]:
    return {
        'type': 'object',
        'properties': {'steps': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {'step': {'type': 'integer'}, 'body': {'type': 'string'}},
            'required': ['step', 'body'], 'additionalProperties': False}}},
        'required': ['steps'], 'additionalProperties': False,
    }


def acceptable(old: str, new: str, known_cautions: List[str], context: frozenset = frozenset()) -> bool:
    """直した本文を採用してよいか: 数式は増やさず，注意の外の数式は全て残し，図の印と TeX の形を保ち，問題が減っている。"""
    if not new.strip() or tex_template.check_latex(new) or _MATH_MARK.search(new):
        return False
    marks = re.compile(r'⟦図\d+⟧')
    if sorted(marks.findall(old)) != sorted(marks.findall(new)):
        return False
    old_math, new_math = math_segments(old), math_segments(new)
    if new_math - old_math:  # 新しい数式や書き換えた数式がある
        return False
    if math_segments(_CHUI.sub('', old)) - new_math:  # 注意の外の数式が消えた
        return False
    before, after = hard_issues(old, known_cautions, context), hard_issues(new, known_cautions, context)
    return len(after) < len(before) or not after or \
        len(_POLITE.findall(plain_text(new))) < len(_POLITE.findall(plain_text(old)))


def _ask(llm, fixed: List[str], issues: Dict[int, List[str]], known: List[str],
         context: frozenset) -> List[int]:
    """issues のあるステップを1回の呼び出しで直し，採用できたものを fixed に書き戻す。戻り値: 直したステップの番号。"""
    masked = {index: mask_math(fixed[index]) for index in issues}
    listing = '\n\n'.join(
        f'### 手順{index + 1}\n指摘:\n' + '\n'.join(f'- {i.replace(SOFT_MARK, "")}' for i in issues[index])
        + f'\n本文（TeX）:\n{masked[index][0]}'
        for index in sorted(issues))
    listed = '\n'.join(f'- {c}' for c in known) or '（なし）'
    user = (f'既に書かれた注意（マスター解説・前の小問）:\n{listed}\n\n'
            f'次の手順の本文を，指摘に従って直してください。step には手順の番号を入れます。\n\n{listing}')
    out = llm.generate_json(system=POLISH_SYSTEM, user=user, schema=_schema(), schema_name='polished_steps',
                            temperature=0.0)
    changed = []
    for item in out.get('steps', []):
        index = int(item.get('step', 0)) - 1
        if index not in issues or index in changed:
            continue
        text = re.sub(r'^```[a-zA-Z]*\n|\n?```$', '', str(item.get('body', ''))).strip()
        text = unmask_math(text, masked[index][1])
        if text is None:
            continue
        new = fix_text(text)
        seen = known + [c for b in fixed[:index] for c in cautions(b)]
        if acceptable(fixed[index], new, seen, context):
            fixed[index] = new
            changed.append(index)
    return changed


def polish(llm, problem: Dict[str, Any], previous: Dict[str, Any],
           earlier_cautions: Optional[List[str]] = None, dry_run: bool = False) -> Dict[str, Any]:
    """テンプレートで個別化した解説（previous）の文章を整える。

    戻り値: {'result': 整えた解説 or None（変更なし）, 'fixed_typos': bool, 'llm_steps': [直したステップの番号]}
    fixed_typos は LLM を呼ばずに直したもの（誤字・重複した注意の削除）があるか。
    dry_run=True なら LLM を呼ばず，LLM で直す必要があるかだけを返す（'needs_llm'）。
    """
    template = problem.get('template')
    if not template or previous.get('format') != 'latex':
        return {'result': None, 'fixed_typos': False, 'llm_steps': [], 'needs_llm': False}
    steps = [dict(s) for s in previous.get('steps', [])]
    known = template_cautions(template) + list(earlier_cautions or [])
    context = context_pairs(problem)
    bodies = [s.get('text', '') for s in steps]
    fixed = drop_duplicate_cautions([fix_text(b) for b in bodies], known, context)
    fixed_typos = fixed != bodies
    checked = frozenset(i for i, s in enumerate(steps) if s.get('polish_checked'))
    issues = explanation_issues(fixed, known, context, checked)
    if dry_run:
        return {'result': None, 'fixed_typos': fixed_typos, 'llm_steps': sorted(issues), 'needs_llm': bool(issues)}
    changed: List[int] = []
    newly_checked = set()
    calls = 0
    while issues and llm is not None and calls < MAX_POLISH_CALLS:
        calls += 1
        newly_checked.update(i for i, found in issues.items() if any(f.startswith(SOFT_MARK) for f in found))
        changed.extend(i for i in _ask(llm, fixed, issues, known, context) if i not in changed)
        # 2回目は，敬体・長い注意などが残ったステップだけ（似ている注意を残したのは LLM の判断として受け入れる）
        issues = explanation_issues(fixed, known, context, soft=False)
    if llm is not None:
        # それでも敬体が残った手順は，地の文の語尾を機械的に常体に直す（数式は印にして触らない）
        for index, body in enumerate(fixed):
            if not _POLITE.search(plain_text(body)):
                continue
            masked, found = mask_math(body)
            new = unmask_math(to_plain_style(masked), found)
            seen = known + [c for b in fixed[:index] for c in cautions(b)]
            if new is not None and new != body and acceptable(body, fix_text(new), seen, context):
                fixed[index] = fix_text(new)
                if index not in changed:
                    changed.append(index)
        issues = explanation_issues(fixed, known, context, soft=False)
    changed = [i for i in changed if fixed[i] != bodies[i]]
    if not changed and not fixed_typos and not newly_checked:
        return {'result': None, 'fixed_typos': False, 'llm_steps': [], 'needs_llm': bool(issues), 'llm_called': calls > 0}
    for index, (step, body) in enumerate(zip(steps, fixed)):
        if index in newly_checked:
            step['polish_checked'] = True
        if body != step.get('text', ''):
            attempts = list(step.get('attempts', []))
            last = dict(attempts[-1]) if attempts else {}
            last.update({'attempt': len(attempts) + 1, 'text': body, 'polished': True})
            step.update(text=body, attempts=attempts + [last], attempt_count=len(attempts) + 1)
    content = tex_template.assemble(template, fixed)
    result = {**previous, 'content': content, 'steps': steps}
    return {'result': result, 'fixed_typos': fixed_typos, 'llm_steps': sorted(changed), 'needs_llm': bool(issues),
            'llm_called': calls > 0}
