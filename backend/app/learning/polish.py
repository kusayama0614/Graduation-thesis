"""生成した解説の文章を整える（文体・誤字・注意の重複と長さ）。

テンプレート（マスター解説の TeX）で個別化した解説では，書き足した部分の文体がマスター解説（常体）と違うと，
解説A・B を伏せて配っても，どちらが個別化したものか気づかれてしまう。また，同じ趣旨の注意の繰り返しや，
要点を詰め込んだ長い注意は読みにくい。そこで，

1. 決まった誤字（「下にとつ」→「下に凸」など）はプログラムで直す（LLM は呼ばない）
2. 敬体（です・ます）・重複した注意・長すぎる注意があるステップだけを，解説1件につき1回の LLM 呼び出しでまとめて直す
3. 直した文章は，数式が1つでも変わっていたら採用しない（検算済みの計算を変えない）。図の印・TeX の形も確かめる
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
_POLITE = re.compile(r'(です|ます|ました|ましょう|ません|でしょう|ください)(?=\s*(?:[。．、，」）)\n]|$))')
SIMILAR_CAUTION = 0.55   # 注意どうしの文字列の一致度がこれ以上なら「同じ趣旨の注意」とみなす
LONG_CAUTION_SENTENCES = 2
LONG_CAUTION_CHARS = 150


def fix_text(text: str) -> str:
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


def _similar(a: str, b: str) -> bool:
    return bool(a and b) and difflib.SequenceMatcher(None, a, b).ratio() >= SIMILAR_CAUTION


def style_issues(body: str, known_cautions: List[str]) -> List[str]:
    """ステップの本文（書き直した TeX）の問題点。known_cautions はマスター解説や前に書いた注意。"""
    issues = []
    polite = _POLITE.findall(plain_text(body))
    if polite:
        issues.append(f'敬体（「です」「ます」「しましょう」など）が {len(polite)} か所ある。マスター解説と同じ常体（である調）にする')
    seen = list(known_cautions)
    for caution in cautions(body):
        if any(_similar(caution, other) for other in seen):
            issues.append(f'注意「{caution[:40]}…」は，マスター解説や前に書いた注意と同じ趣旨なので削る')
        sentences = [s for s in re.split(r'[。．]', caution) if s.strip()]
        if len(sentences) > LONG_CAUTION_SENTENCES or len(caution) > LONG_CAUTION_CHARS:
            issues.append(f'注意「{caution[:40]}…」は長すぎる。要点を1つに絞り，2文以内にする')
        seen.append(caution)
    return issues


def explanation_issues(bodies: List[str], known_cautions: List[str]) -> Dict[int, List[str]]:
    """ステップの番号（0 から）ごとの問題点。前のステップの注意も「既に書いた注意」として数える。"""
    issues: Dict[int, List[str]] = {}
    seen = list(known_cautions)
    for index, body in enumerate(bodies):
        found = style_issues(body, seen)
        if found:
            issues[index] = found
        seen.extend(cautions(body))
    return issues


POLISH_SYSTEM = (
    'あなたは高等学校数学の解答解説の校正者です。指摘された問題だけを直し，内容（解き方・結果・数式）は変えません。'
    '数式（$...$，\\[...\\]，align* 環境などの中身）と ⟦図1⟧ のような印は，一字も変えずにそのまま残します。'
    '文は常体（である調）で書き，「です」「ます」「しましょう」は使いません。'
    '注意（\\begin{chui} ... \\end{chui}）は，既に書かれた注意と同じ趣旨なら削り，長すぎるものは要点を1つに絞って2文以内にします。'
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


def acceptable(old: str, new: str, known_cautions: List[str]) -> bool:
    """直した本文を採用してよいか: 数式は増やさず，注意の外の数式は全て残し，図の印と TeX の形を保ち，問題が減っている。"""
    if not new.strip() or tex_template.check_latex(new):
        return False
    marks = re.compile(r'⟦図\d+⟧')
    if sorted(marks.findall(old)) != sorted(marks.findall(new)):
        return False
    old_math, new_math = math_segments(old), math_segments(new)
    if new_math - old_math:  # 新しい数式や書き換えた数式がある
        return False
    if math_segments(_CHUI.sub('', old)) - new_math:  # 注意の外の数式が消えた
        return False
    return len(style_issues(new, known_cautions)) < len(style_issues(old, known_cautions)) or \
        not style_issues(new, known_cautions)


def polish(llm, problem: Dict[str, Any], previous: Dict[str, Any],
           earlier_cautions: Optional[List[str]] = None, dry_run: bool = False) -> Dict[str, Any]:
    """テンプレートで個別化した解説（previous）の文章を整える。

    戻り値: {'result': 整えた解説 or None（変更なし）, 'fixed_typos': bool, 'llm_steps': [直したステップの番号]}
    dry_run=True なら LLM を呼ばず，LLM で直す必要があるかだけを返す（'needs_llm'）。
    """
    template = problem.get('template')
    if not template or previous.get('format') != 'latex':
        return {'result': None, 'fixed_typos': False, 'llm_steps': [], 'needs_llm': False}
    steps = [dict(s) for s in previous.get('steps', [])]
    known = template_cautions(template) + list(earlier_cautions or [])
    bodies = [s.get('text', '') for s in steps]
    fixed = [fix_text(b) for b in bodies]
    fixed_typos = fixed != bodies
    issues = explanation_issues(fixed, known)
    if dry_run:
        return {'result': None, 'fixed_typos': fixed_typos, 'llm_steps': sorted(issues), 'needs_llm': bool(issues)}
    changed = []
    if issues and llm is not None:
        listing = '\n\n'.join(
            f'### 手順{index + 1}\n指摘:\n' + '\n'.join(f'- {i}' for i in issues[index]) + f'\n本文（TeX）:\n{fixed[index]}'
            for index in sorted(issues))
        context = '\n'.join(f'- {c}' for c in known) or '（なし）'
        user = (f'既に書かれた注意（マスター解説・前の小問）:\n{context}\n\n'
                f'次の手順の本文を，指摘に従って直してください。step には手順の番号を入れます。\n\n{listing}')
        out = llm.generate_json(system=POLISH_SYSTEM, user=user, schema=_schema(), schema_name='polished_steps',
                                temperature=0.0)
        for item in out.get('steps', []):
            index = int(item.get('step', 0)) - 1
            if index not in issues:
                continue
            new = fix_text(re.sub(r'^```[a-zA-Z]*\n|\n?```$', '', str(item.get('body', ''))).strip())
            seen = known + [c for b in fixed[:index] for c in cautions(b)]
            if acceptable(fixed[index], new, seen):
                fixed[index] = new
                changed.append(index)
    if not changed and not fixed_typos:
        return {'result': None, 'fixed_typos': False, 'llm_steps': [], 'needs_llm': bool(issues)}
    for index, (step, body) in enumerate(zip(steps, fixed)):
        if body != step.get('text', ''):
            attempts = list(step.get('attempts', []))
            last = dict(attempts[-1]) if attempts else {}
            last.update({'attempt': len(attempts) + 1, 'text': body, 'polished': True})
            step.update(text=body, attempts=attempts + [last], attempt_count=len(attempts) + 1)
    content = tex_template.assemble(template, fixed)
    result = {**previous, 'content': content, 'steps': steps}
    return {'result': result, 'fixed_typos': fixed_typos, 'llm_steps': changed, 'needs_llm': bool(issues)}
