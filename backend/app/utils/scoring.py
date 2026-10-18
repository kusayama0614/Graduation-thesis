"""採点ユーティリティ

- parse_answers_from_ocr: OCRテキストから問番号と解答を抽出（簡易）
- score_with_key: 提供された解答キーと抽出解答を比較して点数を算出
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Tuple, Any

from app.utils.math_verification import judge_answer

# 解答部分に許す文字: 英数（全角含む）・かな・漢字・数式記号（分数 / 、√、冪 ^ ²、関係記号、カンマ、括弧など）
_ANSWER_CHARS = (
    r"A-Za-z0-9Ａ-Ｚａ-ｚ０-９一-龥ぁ-んァ-ヶー"
    r"\+\-\*/\.\^＋－−＊／．＾√²³¹⁰⁴-⁹⁻°π×÷±=<>＝＜＞≦≧≠≤≥,、，\(\)（）"
)
# 解答内の空白は、演算子・カンマに隣接する場合だけ許す（"m=-6, 2" や "x = 2" はOK、"A B" はNG）
_SPACE_NEIGHBORS = r"=<>＝＜＞≦≧≠≤≥,、，\+\-＋－−\*/＊／×÷±"
_LOOSE_SPACE = re.compile(rf"(?<![{_SPACE_NEIGHBORS}]) (?![{_SPACE_NEIGHBORS}])")

# 問番号と解答の区切り。"-" は番号の数字に直接付く（"1-A"）か後ろが空白（"1 - A"）の場合だけ区切りとみなし、
# "1 -2" "1)-2" "1--2" のような、他の区切りの後ろで文字に付く "-" は負号として解答側に残す。
_SEPARATOR = r"(?:[\).:）．：\s]|[-－](?=\s)|(?<=\d)[-－])"

ANSWER_LINE_PATTERN = re.compile(
    rf"^(\d{{1,3}}){_SEPARATOR}+([{_ANSWER_CHARS}](?:[{_ANSWER_CHARS}\s]*[{_ANSWER_CHARS}])?)$"
)


def _parse_answer_line(line: str) -> Tuple[str, str] | None:
    m = ANSWER_LINE_PATTERN.match(line)
    if not m:
        return None
    answer = re.sub(r"\s+", " ", m.group(2)).strip()
    if _LOOSE_SPACE.search(answer):
        return None
    question = unicodedata.normalize('NFKC', m.group(1))
    return question, answer


def parse_answers_from_ocr(ocr_text: str) -> Dict[str, str]:
    """
    OCRテキストから「番号 解答」の形式を抽出して辞書で返す。
    戻り値: { '1': 'A', '2': 'B', ... }
    """
    answers: Dict[str, str] = {}
    lines = [ln.strip() for ln in ocr_text.splitlines() if ln.strip()]
    for ln in lines:
        parsed = _parse_answer_line(ln)
        if parsed:
            q, a = parsed
            answers[q] = a
    return answers


def extract_answer_key_candidates(ocr_text: str) -> List[Dict[str, str]]:
    """
    OCRテキストから解答キー候補を抽出する。
    返却形式: [{'question': '1', 'answer': 'A'}, ...]
    """
    parsed = parse_answers_from_ocr(ocr_text)
    candidates: List[Dict[str, str]] = []

    for question in sorted(parsed.keys(), key=lambda value: int(value) if str(value).isdigit() else str(value)):
        candidates.append({
            'question': str(question),
            'answer': parsed[question],
        })

    return candidates


def score_with_key(ocr_text: str, answer_key: Dict[str, Any]) -> Dict[str, Any]:
    """
    採点を行う。
    answer_key: { '1': 'A', '2': 'B', ... } または { 'total_questions': N, 'answers': {..} }

    戻り値: {
      'score': float, 'correct_count': int, 'total_questions': int,
      'details': [{'q':'1','expected':'A','actual':'B','correct':False}, ...],
      'error_patterns': [{'pattern': '〜', 'count': n, 'examples': [...]}, ...]
    }
    """
    parsed = parse_answers_from_ocr(ocr_text)

    if isinstance(answer_key, dict) and 'answers' in answer_key:
        key_answers = answer_key['answers']
    else:
        key_answers = answer_key if isinstance(answer_key, dict) else {}

    total = int(answer_key.get('total_questions') if isinstance(answer_key, dict) and 'total_questions' in answer_key else max(len(key_answers), len(parsed) if parsed else 0))
    if total == 0:
        total = max(len(key_answers), len(parsed), 0)

    correct = 0
    details: List[Dict[str, Any]] = []

    for q, expected in key_answers.items():
        actual = parsed.get(str(q)) or parsed.get(str(int(q)) if str(q).isdigit() else q) or ''
        is_correct = False
        method = 'empty'
        if actual and expected is not None:
            # 数学的同値性で判定（解釈できない解答は文字列一致にフォールバック）
            verdict = judge_answer(expected, actual)
            is_correct = verdict['is_correct']
            method = verdict['method']
        if is_correct:
            correct += 1
        details.append({'q': str(q), 'expected': expected, 'actual': actual, 'correct': is_correct, 'method': method})

    # 不正解パターン抽出（簡易）
    error_patterns = []
    wrong_items = [d for d in details if not d['correct']]
    if wrong_items:
        # 例: 出現頻度が高い誤答をパターン化（ここでは単純に個数）
        pattern = {'pattern': '誤答あり', 'count': len(wrong_items), 'examples': [f"Q{d['q']} expected={d['expected']} got={d['actual']}" for d in wrong_items[:5]]}
        error_patterns.append(pattern)

    score = round((correct / total) * 100, 1) if total > 0 else 0.0

    return {
        'score': score,
        'correct_count': correct,
        'total_questions': total,
        'details': details,
        'error_patterns': error_patterns,
    }
