"""演習プリントの答えの書き起こしと採点（卒業論文 3.5節の層2・層3）

層2（記述から式への変換）: 答案の画像を Gemini に渡し，小問ごとに鉛筆の初答と色ペンの訂正を分けて，
    書かれたとおりに書き起こす。正解は渡さない（生徒の誤答を正しい値に直してしまう「過剰修正」を防ぐ）。
層3（判定）: 初答を解答キーの正解と数式処理で照合する。正誤は常にここで決め，LLM には決めさせない。

解答キーは app/learning/data/answer_keys.json（No.1〜No.9）。小問の種類（kind）ごとに照合の仕方を変える:
    math       数式・不等式・座標など（math_verification.judge_answer）。strict_decimals なら表の値の桁も見る
    ordered    sinθ, cosθ, tanθ のように順序に意味がある組（入れ替わりは誤り）
    cases      「m=2 のとき x=−1」のような条件と値の組（一部だけなら不完全）
    text       「すべての実数」「解はない」など言葉の答え
    trig_form  「cos16°」のように三角比の形そのものを問うもの（値が等しくても別の形は誤り）
    graph      グラフ（自動判定の対象外。書き起こしだけ残す）
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from app.learning import judgments as J
from app.utils.math_verification import judge_answer

KEY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'learning', 'data', 'answer_keys.json')


@lru_cache(maxsize=1)
def load_answer_keys() -> Dict[str, Any]:
    with open(KEY_PATH, encoding='utf-8') as f:
        return json.load(f)['prints']


def answer_key_for(print_no: Optional[int]) -> Optional[Dict[str, Any]]:
    """組み込みの解答キーを，AnswerSheet.answer_key の形（answers・nodes・unit・items）で返す。"""
    entry = load_answer_keys().get(str(print_no)) if print_no else None
    if entry is None:
        return None
    return {
        'answers': {q['question']: q['answer'] for q in entry['questions']},
        'nodes': {q['question']: q['nodes'] for q in entry['questions']},
        'unit': entry['unit'],
        'print_no': int(print_no),
        'source': 'builtin',
    }


def key_items(print_no: Optional[int]) -> List[Dict[str, Any]]:
    entry = load_answer_keys().get(str(print_no)) if print_no else None
    return list(entry['questions']) if entry else []


# ---------------------------------------------------------------- 照合（層3）

_FULLWIDTH = str.maketrans('０１２３４５６７８９．，（）＝＜＞－＋／', '0123456789.,()=<>-+/')
_NUMERIC_EXPECTED = re.compile(r'^[\d.,\s/-]+$')


def _clean(text: str) -> str:
    return (text or '').translate(_FULLWIDTH).replace('−', '-').replace('ー', '-').strip()


def _strip_units(text: str) -> str:
    """数値を問う小問で，単位や「約」「θ≒」を外す（式を問う小問には使わない）。"""
    text = re.sub(r'^\s*(?:[θx]\s*[≒=]|約|≒)\s*', '', text)
    text = re.sub(r'(?<=\d)\s*(?:個|cm²|cm|km|m|度|°)', '', text)
    return text.strip()


def split_top_level(text: str) -> List[str]:
    """括弧の外のカンマ・読点で区切る（座標 (2, 1) は区切らない）。"""
    parts, depth, current = [], 0, ''
    for ch in text:
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth = max(0, depth - 1)
        if ch in ',，、' and depth == 0:
            parts.append(current.strip())
            current = ''
        else:
            current += ch
    parts.append(current.strip())
    return [p for p in parts if p != ''] if any(parts) else []


def _decimals(text: str) -> Optional[int]:
    match = re.fullmatch(r'\s*-?\d+(?:\.(\d+))?\s*', text)
    if not match:
        return None
    return len(match.group(1) or '')


def _judge(expected: str, actual: str) -> Tuple[bool, str]:
    try:
        result = judge_answer(expected, actual)
    except Exception:  # 解釈できない記述は誤りとして扱う（記述は残るので教員が確認できる）
        return False, 'unparsable'
    return bool(result.get('is_correct')), result.get('method', '')


def _grade_math(item: Dict[str, Any], actual: str) -> Tuple[str, str]:
    expected = str(item['expected'])
    numeric = bool(_NUMERIC_EXPECTED.match(expected))
    if numeric:
        actual = _strip_units(actual)
    if item.get('strict_decimals'):
        need = len(expected.split('.')[1].rstrip('0')) if '.' in expected else 0
        have = _decimals(actual)
        if have is not None and have < need:
            return J.WRONG, 'rounding'  # 表の値を指定より粗く丸めた（例: sin24°=0.41）
    ok, method = _judge(expected, actual)
    if ok:
        return J.CORRECT, method
    return (J.INCOMPLETE if method == 'incomplete' else J.WRONG), method


def _grade_ordered(item: Dict[str, Any], actual: str) -> Tuple[str, str]:
    expected_parts = split_top_level(str(item['expected']))
    actual_parts = split_top_level(actual)
    numeric = all(_NUMERIC_EXPECTED.match(p) for p in expected_parts)
    results = []
    for index, expected in enumerate(expected_parts):
        if index >= len(actual_parts) or not actual_parts[index]:
            results.append(None)
            continue
        part = _strip_units(actual_parts[index]) if numeric else actual_parts[index]
        results.append(_judge(expected, part)[0])
    if len(actual_parts) > len(expected_parts):
        return J.WRONG, 'extra_items'
    if all(r is True for r in results):
        return J.CORRECT, 'ordered_equal'
    if all(r in (True, None) for r in results):
        return J.INCOMPLETE, 'incomplete'
    return J.WRONG, 'ordered_mismatch'


def parse_cases(text: str) -> List[Tuple[str, str]]:
    """「m=2 のとき x=-1；m=-6 のとき x=3」を [(条件, 値)] にする。"""
    pairs = []
    if 'のとき' not in text:  # 値を書かずに条件だけ並べた答え（例: m=2，m=-6）
        return [(part, '') for part in split_top_level(text)]
    for chunk in re.split(r'[；;。\n]|[，,、](?=[^，,、]*のとき)', text):
        chunk = chunk.strip()
        if not chunk:
            continue
        if 'のとき' in chunk:
            condition, _, value = chunk.partition('のとき')
        else:
            condition, value = chunk, ''
        pairs.append((condition.strip(' ，,、'), value.strip(' ，,、')))
    return pairs


def _grade_cases(item: Dict[str, Any], actual: str) -> Tuple[str, str]:
    expected_pairs = [tuple(p) for p in item['expected']]
    actual_pairs = parse_cases(actual)
    matched, partial, wrong = set(), 0, 0
    for condition, value in actual_pairs:
        hit = None
        for index, (exp_condition, exp_value) in enumerate(expected_pairs):
            if index not in matched and _judge(exp_condition, condition)[0]:
                hit = index
                break
        if hit is None:
            wrong += 1
        elif not value:
            partial += 1
            matched.add(hit)
        elif _judge(expected_pairs[hit][1], _strip_units(value))[0]:
            matched.add(hit)
        else:
            wrong += 1
    if wrong:
        return J.WRONG, 'cases_mismatch'
    if len(matched) == len(expected_pairs) and not partial:
        return J.CORRECT, 'cases_equal'
    return J.INCOMPLETE, 'incomplete'


_TEXT_SYNONYMS = [
    (re.compile(r'^(?:x\s*は)?(?:すべて|全て|全部)の実数$|^実数全体$|^全実数$'), 'すべての実数'),
    (re.compile(r'^(?:解(?:は|が)?(?:ない|なし|存在しない)|なし|∅)$'), '解はない'),
]


def _canonical_text(text: str) -> str:
    text = re.sub(r'\s+', '', _clean(text)).replace('全て', 'すべて').replace('≠', '!=')
    for pattern, canonical in _TEXT_SYNONYMS:
        if pattern.match(text):
            return canonical
    return text


def _grade_text(item: Dict[str, Any], actual: str) -> Tuple[str, str]:
    accepted = {_canonical_text(a) for a in item.get('accept', [item['expected']])}
    if _canonical_text(actual) in accepted:
        return J.CORRECT, 'text_equal'
    for alternative in item.get('accept_math', []):
        if _judge(alternative, actual)[0]:
            return J.CORRECT, 'set_equal'
    return J.WRONG, 'text_mismatch'


def _canonical_trig(text: str) -> str:
    return re.sub(r'[\s°度θ()（）]', '', _clean(text)).lower().replace('÷', '/')


def grade_item(item: Dict[str, Any], actual: Optional[str]) -> Dict[str, str]:
    """書き起こした初答（式の形）を照合して {judgment, method} を返す。"""
    kind = item.get('kind', 'math')
    if kind == 'graph':
        return {'judgment': J.UNMARKED, 'method': 'not_graded'}
    actual = _clean(actual or '')
    if not actual:
        return {'judgment': J.BLANK, 'method': 'empty'}
    if kind == 'ordered':
        judgment, method = _grade_ordered(item, actual)
    elif kind == 'cases':
        judgment, method = _grade_cases(item, actual)
    elif kind == 'text':
        judgment, method = _grade_text(item, actual)
    elif kind == 'trig_form':
        ok = _canonical_trig(actual) == _canonical_trig(str(item['expected']))
        judgment, method = (J.CORRECT, 'form_equal') if ok else (J.WRONG, 'form_mismatch')
    else:
        judgment, method = _grade_math(item, actual)
    return {'judgment': judgment, 'method': method}


def judgment_with_form_note(judgment: str, form_issue: bool) -> str:
    """書き方の問題（角度を問われて sin15° と書く等）があれば，正解・誤答をそれぞれ書き方の問題として区別する。"""
    if not form_issue:
        return judgment
    if judgment == J.CORRECT:
        return J.CORRECT_FORM_ISSUE
    if judgment == J.WRONG:
        return J.FORM_ISSUE
    return judgment


# ---------------------------------------------------------------- 書き起こし（層2）

TRANSCRIBE_SYSTEM = (
    'あなたは高校の数学の演習プリントの答案画像から，生徒の答えを書き起こす係です。'
    '生徒の答えが誤っていても，絶対に直さず，書かれたとおりに書き起こしてください（≦は≦のまま，近似値は近似値のまま，'
    '分数を小数にしない）。正誤の判定はしません。氏名は読み取らないでください。'
    'このプリントでは，生徒は鉛筆で解いた後に略解を見て，赤・オレンジ・青などの色ペンで丸付けや書き直しをします。'
    '鉛筆（黒・灰色）で最初に書いた答えを「初答」，色ペンで後から書き加えた・書き直した答えを「訂正」として必ず分けてください。'
)


def transcribe_prompt(items: List[Dict[str, Any]]) -> str:
    lines = [f'- {q["question"]}: {q["problem"]}（書き方: {q["format"]}）' for q in items]
    return (
        '次の小問について，答案画像（表面・裏面）から生徒の答えを書き起こしてください。\n'
        + '\n'.join(lines) + '\n\n'
        '各小問について:\n'
        '- initial_text: 鉛筆の初答を，答えとして書かれた部分（下線や A. の後など。途中式は除く）だけ，見たとおりの文字で。\n'
        '- initial_math: 初答を上の「書き方」に従って式の形にしたもの。√ は sqrt()，分数は a/b，≦・≧・≠ はそのまま。'
        '値を直したり，書かれていない部分を補ったりしない。\n'
        '- corrected_text: 色ペンで書き加えた・書き直した答え（なければ空）。\n'
        '- blank: 鉛筆の初答が無い（空欄，または色ペンでしか書かれていない）なら true。\n'
        '- self_mark: 生徒が色ペンで付けた丸（○）なら "o"，バツ・チェック（×・✓）なら "x"，なければ "none"。\n'
        '- form_issue: 意図は分かるが書き方に問題がある（角度を問われて sin15° とだけ書く，m の範囲を D<… と書く等）なら true。\n'
        '- confidence: 文字をはっきり読めたら high，自信がなければ low。読めないときは unreadable を true に。\n'
        '- note: 気になった点があれば短く。\n'
        '画像にない小問も含め，上の全ての小問を一つずつ出力してください。'
    )


TRANSCRIBE_SCHEMA = {
    'type': 'object',
    'properties': {
        'answers': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'question': {'type': 'string'},
                    'initial_text': {'type': 'string'},
                    'initial_math': {'type': 'string'},
                    'corrected_text': {'type': 'string'},
                    'blank': {'type': 'boolean'},
                    'self_mark': {'type': 'string', 'enum': ['o', 'x', 'none']},
                    'form_issue': {'type': 'boolean'},
                    'confidence': {'type': 'string', 'enum': ['high', 'low']},
                    'unreadable': {'type': 'boolean'},
                    'note': {'type': 'string'},
                },
                'required': ['question', 'initial_text', 'initial_math', 'corrected_text', 'blank', 'self_mark',
                             'form_issue', 'confidence', 'unreadable', 'note'],
            },
        },
    },
    'required': ['answers'],
}


def build_items(key: List[Dict[str, Any]], raw_answers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """LLM の書き起こしを小問の順に並べ，照合して判定を付ける。"""
    by_question = {str(a.get('question', '')).strip(): a for a in raw_answers if isinstance(a, dict)}
    items = []
    for q in key:
        raw = by_question.get(q['question'], {})
        items.append(grade_transcribed(q, {
            'initial_text': str(raw.get('initial_text') or '').strip(),
            'initial_math': str(raw.get('initial_math') or '').strip(),
            'corrected_text': str(raw.get('corrected_text') or '').strip(),
            'blank': bool(raw.get('blank')),
            'self_mark': raw.get('self_mark') if raw.get('self_mark') in ('o', 'x', 'none') else 'none',
            'form_issue': bool(raw.get('form_issue')),
            'confidence': raw.get('confidence') if raw.get('confidence') in ('high', 'low') else 'low',
            'unreadable': bool(raw.get('unreadable')),
            'note': str(raw.get('note') or '').strip() or ('書き起こしに含まれていなかった' if not raw else ''),
            'edited': False,
        }))
    return items


def grade_transcribed(q: Dict[str, Any], entry: Dict[str, Any]) -> Dict[str, Any]:
    """1つの小問の書き起こしに判定を付ける（教員が直したときにも使う）。"""
    if entry.get('unreadable'):
        judgment, method = J.UNREADABLE, 'unreadable'
    elif entry.get('blank') or not entry.get('initial_math'):
        judgment, method = J.BLANK, 'empty'
    else:
        graded = grade_item(q, entry['initial_math'])
        judgment = judgment_with_form_note(graded['judgment'], entry.get('form_issue', False))
        method = graded['method']
    return {
        **entry,
        'question': q['question'],
        'expected': q['answer'],
        'kind': q.get('kind', 'math'),
        'judgment': judgment,
        'method': method,
        'needs_review': (entry.get('confidence') == 'low' or bool(entry.get('unreadable'))) and not entry.get('edited'),
    }


def question_results(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """学習者プロファイルへの反映（build_exercise_items）が読む形にする。"""
    return [{
        'question': item['question'],
        'result': J.RESULT_OF_JUDGMENT[item['judgment']],
        'judgment': item['judgment'],
        'answer': item['initial_math'] or item['initial_text'] or None,
        'corrected': bool(item['corrected_text']),
        'corrected_answer': item['corrected_text'] or None,
        'self_mark': item['self_mark'],
        'source': 'transcription',
    } for item in items]


def ocr_text_of(items: List[Dict[str, Any]]) -> str:
    """一覧表示用に，小問ごとの初答を「小問 答え」の行にする。"""
    return '\n'.join(f'{item["question"]} {item["initial_text"] or "（空欄）"}'
                     + (f'  ［訂正: {item["corrected_text"]}］' if item['corrected_text'] else '') for item in items)
