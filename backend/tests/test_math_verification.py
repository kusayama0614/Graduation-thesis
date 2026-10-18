import time

import pytest

from app.utils.math_verification import judge_answer
from app.utils.scoring import score_with_key


def verdict(expected, actual):
    result = judge_answer(expected, actual)
    return result['is_correct'], result['method']


# ---- 値の同値性 ----------------------------------------------------------

def test_radical_forms_are_equal():
    assert verdict('1/√3', '√3/3') == (True, 'symbolic_equal')


def test_fullwidth_and_degree_notation():
    assert verdict('１／２', '0.5')[0] is True
    assert verdict('60', '60°') == (True, 'symbolic_equal')
    assert verdict('√3/2', 'sin60°') == (True, 'symbolic_equal')
    assert verdict('2√3', '√12')[0] is True


def test_expression_equivalence_with_variables():
    assert verdict('x^2+2x+1', '(x+1)²') == (True, 'symbolic_equal')
    assert verdict('x^2+2x+1', '(x+2)^2')[0] is False


def test_decimal_expected_uses_rounding_tolerance():
    assert verdict('26.8', '23.6')[0] is False
    assert verdict('23.6', '23.6')[0] is True
    assert verdict('0.6', '3/5')[0] is True
    assert verdict('1.4', '√2') == (True, 'numeric_equal')
    assert verdict('1.4', '1.5')[0] is False


def test_exact_expected_is_not_matched_by_rough_decimal():
    assert verdict('1/3', '0.333')[0] is False


# ---- 不等式・方程式 ------------------------------------------------------

def test_inequality_direction_is_equivalent():
    assert verdict('16/3>m', 'm<16/3') == (True, 'set_equal')
    assert verdict('x>5', '5<x') == (True, 'set_equal')


def test_inequality_boundary_is_distinguished():
    assert verdict('16/3>m', 'm≦16/3')[0] is False
    assert verdict('x>5', 'x≧5')[0] is False


def test_chained_inequality_and_unicode_operators():
    assert verdict('1<x<3', '3>x>1') == (True, 'set_equal')
    assert verdict('x≦2', 'x<=2')[0] is True


def test_equation_forms():
    assert verdict('y=2x+1', '2x-y+1=0') == (True, 'set_equal')
    assert verdict('y=2x+1', 'y=2x+2')[0] is False
    assert verdict('x=2', '2')[0] is True
    assert verdict('x=2', 'y=2')[0] is False


# ---- 組の答え ------------------------------------------------------------

def test_list_answers_are_order_independent():
    assert verdict('m=-6, 2', '2, m=-6') == (True, 'list_equal')
    assert verdict('3,5', '5、3')[0] is True


def test_list_answers_missing_or_extra_items():
    assert verdict('m=-6, 2', 'm=-6') == (False, 'incomplete')
    assert verdict('3,5', '3,5,7') == (False, 'extra_items')
    assert verdict('3,5', '3,6') == (False, 'list_mismatch')


def test_coordinate_pairs_are_ordered():
    assert verdict('(1,2)', '(1, 2)')[0] is True
    assert verdict('(1,2)', '(2,1)')[0] is False


# ---- 文字列フォールバック -------------------------------------------------

def test_choice_answers_use_string_fallback():
    assert verdict('ア', 'ア') == (True, 'string_exact')
    assert verdict('ア', 'イ') == (False, 'string_mismatch')
    assert verdict('A', 'a') == (True, 'string_exact')
    assert verdict('A', 'B') == (False, 'string_mismatch')


def test_empty_answer_is_incorrect():
    assert verdict('3', '') == (False, 'empty')
    assert verdict(None, '3') == (False, 'empty')


# ---- 安全性 --------------------------------------------------------------

@pytest.mark.parametrize('payload', [
    '().__class__',
    'import os',
    "__import__('os').system('echo hi')",
    'open(1)',
    '[x for x in range(3)]',
    'lambda: 1',
    'x.__class__.__mro__',
])
def test_dangerous_input_falls_back_safely(payload):
    result = judge_answer('3', payload)
    assert result['is_correct'] is False
    assert result['method'] in ('string_mismatch', 'symbolic_mismatch', 'numeric_mismatch', 'type_mismatch')
    # 期待値側に入っても同様に安全
    assert judge_answer(payload, payload)['is_correct'] is True  # 文字列として同一
    assert judge_answer(payload, '3')['is_correct'] is False


def test_dangerous_input_never_uses_math_path():
    for payload in ('().__class__', 'import os', "__import__('os')"):
        assert judge_answer(payload, payload)['method'] == 'string_exact'


def test_huge_power_does_not_hang():
    started = time.time()
    result = judge_answer('9^9^9^9', '9^9^9^9')
    assert time.time() - started < 5
    assert result['method'] == 'string_exact'
    assert judge_answer('2', '10**100000')['is_correct'] is False


def test_overlong_input_falls_back():
    long_text = '1+' * 150 + '1'
    assert judge_answer(long_text, long_text)['method'] == 'string_exact'


# ---- score_with_key への統合 ----------------------------------------------

def test_score_with_key_uses_math_equivalence_and_reports_method():
    ocr_text = '1 A\n2 3/6\n3 7'
    result = score_with_key(ocr_text, {'1': 'a', '2': '1/2', '3': '8'})
    assert result['correct_count'] == 2
    assert result['total_questions'] == 3
    assert result['score'] == pytest.approx(66.7)
    by_q = {d['q']: d for d in result['details']}
    assert by_q['1']['correct'] is True and by_q['1']['method'] == 'string_exact'
    assert by_q['2']['correct'] is True and by_q['2']['method'] == 'symbolic_equal'
    assert by_q['3']['correct'] is False
    assert set(result) == {'score', 'correct_count', 'total_questions', 'details', 'error_patterns'}


def test_score_with_key_missing_answer_is_incorrect():
    result = score_with_key('1 A', {'1': 'A', '2': 'B'})
    by_q = {d['q']: d for d in result['details']}
    assert by_q['2']['correct'] is False
    assert by_q['2']['method'] == 'empty'


def test_ocr_parser_keeps_math_answers():
    from app.utils.scoring import parse_answers_from_ocr
    parsed = parse_answers_from_ocr('1 A\n2 3/6\n3 √3/3\n4 m<16/3\n5 (1,2)\n6 x^2+1')
    assert parsed == {'1': 'A', '2': '3/6', '3': '√3/3', '4': 'm<16/3', '5': '(1,2)', '6': 'x^2+1'}
