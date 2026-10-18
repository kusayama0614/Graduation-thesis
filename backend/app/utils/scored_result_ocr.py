"""Extract likely score and per-question marks from OCR text."""

from __future__ import annotations

import re
from typing import Any

from app.learning.judgments import ALL_JUDGMENTS, RESULT_OF_JUDGMENT


_QUESTION_MARK_RE = re.compile(
    r"(?:問|Q)?\s*(\d{1,3})\s*[.．、:：)）\-]?\s*"
    r"(○|◯|〇|✓|✔|✅|×|✕|✖|✗|✘|Ｘ|X|x|正解|不正解)"
)
_SCORE_FRACTION_RE = re.compile(
    r"(?:得点|点数|スコア|score)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_SCORE_VALUE_RE = re.compile(
    r"(?:得点|点数|スコア|score)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(?:点|%)?",
    re.IGNORECASE,
)
_CORRECT_COUNT_RE = re.compile(
    r"(?:正答数|正解数|正答|正解)\s*[:：]?\s*(\d+)(?:\s*/\s*(\d+))?"
)


def extract_scored_result_suggestions(ocr_text: str) -> dict[str, Any]:
    """Return OCR suggestions only; values must be reviewed before saving."""
    question_results: dict[str, bool] = {}
    for line in ocr_text.splitlines():
        for match in _QUESTION_MARK_RE.finditer(line):
            question_number, mark = match.groups()
            question_results[question_number] = mark in {
                "○", "◯", "〇", "✓", "✔", "✅", "正解",
            }

    score = None
    total_questions = None
    correct_count = None

    fraction_match = _SCORE_FRACTION_RE.search(ocr_text)
    if fraction_match:
        score_value, maximum_value = map(float, fraction_match.groups())
        if maximum_value > 0:
            score = round((score_value / maximum_value) * 100, 1)

    correct_match = _CORRECT_COUNT_RE.search(ocr_text)
    if correct_match:
        correct_count = int(correct_match.group(1))
        if correct_match.group(2):
            total_questions = int(correct_match.group(2))

    if score is None:
        score_match = _SCORE_VALUE_RE.search(ocr_text)
        if score_match:
            score = float(score_match.group(1))

    if question_results:
        marked_correct = sum(question_results.values())
        if correct_count is None:
            correct_count = marked_correct
        if total_questions is None:
            total_questions = len(question_results)

    if total_questions is None and correct_count is not None and correct_match and correct_match.group(2):
        total_questions = int(correct_match.group(2))

    if score is None and total_questions and correct_count is not None:
        score = round((correct_count / total_questions) * 100, 1)

    return {
        "score": score,
        "correct_count": correct_count,
        "total_questions": total_questions,
        "question_results": [
            {
                "question": question,
                "result": "correct" if is_correct else "incorrect",
            }
            for question, is_correct in sorted(question_results.items(), key=lambda item: int(item[0]))
        ],
    }


def normalize_question_results(value: Any) -> list[dict[str, Any]]:
    """Validate and normalize teacher-confirmed per-question results."""
    if not isinstance(value, list):
        raise ValueError("question_results must be a list")

    normalized = []
    seen_questions = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each question result must be an object")
        question = str(item.get("question", "")).strip()
        result = item.get("result")
        if not question or len(question) > 32:
            raise ValueError("Each question must have a valid question number")
        if question in seen_questions:
            raise ValueError(f"Duplicate question number: {question}")
        if result not in ("correct", "incorrect", "unmarked"):
            raise ValueError("Question result must be correct, incorrect, or unmarked")
        seen_questions.add(question)
        entry = {"question": question, "result": result}

        # 任意: 学習者プロファイル用の詳細（判定ラベル・初答・訂正の有無）
        judgment = item.get("judgment")
        if judgment is not None:
            if judgment not in ALL_JUDGMENTS:
                raise ValueError(f"judgment must be one of: {', '.join(ALL_JUDGMENTS)}")
            if RESULT_OF_JUDGMENT[judgment] != result:
                raise ValueError(f"judgment '{judgment}' is inconsistent with result '{result}' (question {question})")
            entry["judgment"] = judgment
        answer = item.get("answer")
        if answer is not None:
            answer = str(answer).strip()
            if len(answer) > 200:
                raise ValueError(f"answer is too long (question {question})")
            entry["answer"] = answer
        corrected = item.get("corrected")
        if corrected is not None:
            if not isinstance(corrected, bool):
                raise ValueError(f"corrected must be a boolean (question {question})")
            entry["corrected"] = corrected
        normalized.append(entry)
    return normalized
