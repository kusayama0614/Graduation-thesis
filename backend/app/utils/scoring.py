"""採点ユーティリティ

- parse_answers_from_ocr: OCRテキストから問番号と解答を抽出（簡易）
- score_with_key: 提供された解答キーと抽出解答を比較して点数を算出
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple, Any

ANSWER_LINE_PATTERNS = [
    r"^(\d{1,3})[\).:\-\s]+([A-Za-z0-9一-龥ぁ-んァ-ン\+\-]+)$",
    r"^(\d{1,3})\s+([A-Za-z0-9一-龥ぁ-んァ-ン\+\-]+)$",
]


def parse_answers_from_ocr(ocr_text: str) -> Dict[str, str]:
    """
    OCRテキストから「番号 解答」の形式を抽出して辞書で返す。
    戻り値: { '1': 'A', '2': 'B', ... }
    """
    answers: Dict[str, str] = {}
    lines = [ln.strip() for ln in ocr_text.splitlines() if ln.strip()]
    for ln in lines:
        for pat in ANSWER_LINE_PATTERNS:
            m = re.match(pat, ln)
            if m:
                q = m.group(1)
                a = m.group(2).strip()
                answers[q] = a
                break
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
        if actual and expected is not None:
            # 比較: 大文字小文字無視、全角/半角簡易正規化
            act_norm = str(actual).strip().upper().translate(str.maketrans({'　': ' ', '％': '%'}))
            exp_norm = str(expected).strip().upper().translate(str.maketrans({'　': ' ', '％': '%'}))
            if act_norm == exp_norm:
                is_correct = True
        if is_correct:
            correct += 1
        details.append({'q': str(q), 'expected': expected, 'actual': actual, 'correct': is_correct})

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
