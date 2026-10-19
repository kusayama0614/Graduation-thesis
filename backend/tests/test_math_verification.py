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


# ---- OCR行パーサ: 負の答え・解答内の空白 -----------------------------------

@pytest.mark.parametrize('line, expected', [
    ('1 -2', ('1', '-2')),              # 空白 + "-" + 数字 は負号
    ('1. -3', ('1', '-3')),
    ('1) -3', ('1', '-3')),
    ('1 - -3', ('1', '-3')),
    ('1)-2', ('1', '-2')),
    ('1.-2', ('1', '-2')),
    ('1--2', ('1', '-2')),
    ('1 −1/2', ('1', '−1/2')),          # Unicodeのマイナス
    ('1-A', ('1', 'A')),                # 番号に直接付く "-" は区切り（従来どおり）
    ('1 - 4', ('1', '4')),              # 後ろが空白の "-" は区切り（従来どおり）
    ('1 m=-6, 2', ('1', 'm=-6, 2')),    # 演算子・カンマの隣の空白は解答の一部
    ('1 x = 2', ('1', 'x = 2')),
    ('1 m < 16/3', ('1', 'm < 16/3')),
    ('1 (1, 2)', ('1', '(1, 2)')),
    ('1 x=±√3', ('1', 'x=±√3')),
    ('１０．　５', ('10', '５')),       # 全角の番号・区切り
])
def test_ocr_parser_line_formats(line, expected):
    from app.utils.scoring import parse_answers_from_ocr
    q, a = expected
    assert parse_answers_from_ocr(line) == {q: a}


@pytest.mark.parametrize('line', ['1 A B', '1 2 3', '2024年度 期末'])
def test_ocr_parser_rejects_ambiguous_lines(line):
    from app.utils.scoring import parse_answers_from_ocr
    assert parse_answers_from_ocr(line) == {}


def test_score_with_key_handles_negative_and_spaced_answers():
    ocr_text = '1 -2\n2 m=-6, 2\n3 2\n4 x = ±√3'
    key = {'1': '-2', '2': '2, m=-6', '3': '-2', '4': 'x=√3, x=-√3'}
    by_q = {d['q']: d for d in score_with_key(ocr_text, key)['details']}
    assert by_q['1']['correct'] is True
    assert by_q['2']['correct'] is True and by_q['2']['method'] == 'list_equal'
    assert by_q['3']['correct'] is False   # 符号の付け忘れは不正解
    assert by_q['4']['correct'] is True


# ---- 判定エンジンの追加ケース ---------------------------------------------

def test_plus_minus_expands_to_two_answers():
    assert verdict('x=±√3', '√3, -√3') == (True, 'list_equal')
    assert verdict('x=±√3', 'x=√3') == (False, 'incomplete')
    assert verdict('±2', '2,-2')[0] is True


def test_multi_digit_and_negative_superscripts():
    assert verdict('x^10', 'x¹⁰') == (True, 'symbolic_equal')
    assert verdict('1/x', 'x⁻¹') == (True, 'symbolic_equal')


def test_unicode_minus_and_fullwidth_digits():
    assert verdict('-2', '−2')[0] is True
    assert verdict('5', '５')[0] is True


def test_decimal_tolerance_inside_equation_and_lists():
    assert verdict('x=23.6', '23.62') == (True, 'numeric_equal')
    assert verdict('1.4, 2.2', '2.24, 1.41') == (True, 'list_equal')


def test_list_matching_is_not_greedy():
    # 1.45 は 1.4 にも 1.45 にも許容誤差内で一致するが、全体として1対1に対応付けられる
    assert verdict('1.4, 1.45', '1.45, 1.4') == (True, 'list_equal')


def test_latex_notation_is_understood():
    assert verdict('m<49/8', r'm < \frac{49}{8}') == (True, 'set_equal')
    assert verdict('√2/4', r'$\dfrac{\sqrt{2}}{4}$')[0] is True
    assert verdict('x<=2', r'x \leq 2')[0] is True
    assert verdict('1/2', r'\sin 30^\circ')[0] is True


# ---- 解説の検算（Chain 4）: 複数解の並記・プライム・中黒 ----------------------

from app.learning import explanation as chain  # noqa: E402
from app.utils.math_verification import check_claim, check_relation_step, solved_values  # noqa: E402


def _relations(*items):
    return {'claims': [], 'final_answer': '',
            'relations': [{'expr': e, 'text': e, 'transformed_from_previous': t} for e, t in items]}


def test_multiple_solutions_written_together_are_one_solution_set():
    for written in ('x=1-\\sqrt{6}, 1+\\sqrt{6}', 'x=1-\\sqrt{6},\; 1+\\sqrt{6}', 'x=1\\pm\\sqrt{6}', 'x=1\\pm\\sqrt6'):
        assert check_relation_step('x^2-2*x-5=0', written)['status'] == 'ok', written
    assert check_relation_step('x^2-2*x-5>=0', 'x\\leqq 1-\\sqrt6,\\quad 1+\\sqrt6\\leqq x')['status'] == 'ok'
    assert judge_answer('x=1+sqrt(6), 1-sqrt(6)', 'x=1\\pm\\sqrt6')['is_correct']


def test_solutions_extracted_separately_are_merged_not_compared_with_each_other():
    # 「x = 1-√6, 1+√6」が別々の式として抜き出され，2つ目に「変形」の印が付いても誤りにしない
    result = chain.verify_step(1, _relations(('x^2-2*x-5=0', False), ('x=1-sqrt(6)', True), ('1+sqrt(6)', True)), {})
    assert result['ok'] and [(r['status'], r['expr']) for r in result['relations']] == [
        ('condition', 'x^2-2*x-5=0'), ('ok', 'x=1-sqrt(6), 1+sqrt(6)')]
    result = chain.verify_step(1, _relations(('(m+6)*(m-2)=0', False), ('m=2', True), ('m=-6', False)), {})
    assert result['ok'] and result['relations'][1]['expr'] == 'm=2, -6'


def test_merging_solutions_still_catches_real_errors():
    result = chain.verify_step(1, _relations(('x^2-2*x-5=0', False), ('x=1-sqrt(6)', True), ('x=1+sqrt(5)', True)), {})
    assert not result['ok'] and result['relations'][1]['status'] == 'ng'
    # 不等式はまとめない（x>2 → x>3 は誤り）
    result = chain.verify_step(1, _relations(('x>2', False), ('x>3', True)), {})
    assert [r['status'] for r in result['relations']] == ['condition', 'ng']


def test_solved_values():
    assert solved_values('x=1-sqrt(6)') == ('x', ['1-sqrt(6)'])
    assert solved_values('1+\\sqrt{6}') == (None, ['1+ sqrt(6)'])
    assert solved_values('x>2') is None and solved_values('x=y+1') is None


def test_prime_variables_are_separate_letters():
    assert check_claim("b'", '-1')['status'] == 'definition'
    definitions = {"b'": '-1', 'a': '1', 'c': '-5'}
    assert check_claim("b'^2-ac", '(-1)^2-1\\cdot(-5)', definitions)['status'] == 'ok'
    wrong = check_claim("b'^2-ac", '7', definitions)
    assert wrong['status'] == 'ng' and wrong['true_value'] == '6'
    assert check_claim("b'^2-b''", "b'^2-b''")['status'] == 'ok'  # b' と b'' は別の文字
    # 解説の中で b' = -1 と定義してから b'^2 - ac を検算できる
    result = chain.verify_step(1, {'claims': [
        {'lhs': "b'", 'rhs': '-1', 'lhs_text': "b'", 'rhs_text': '-1'},
        {'lhs': 'a', 'rhs': '1', 'lhs_text': 'a', 'rhs_text': '1'},
        {'lhs': 'c', 'rhs': '-5', 'lhs_text': 'c', 'rhs_text': '-5'},
        {'lhs': "b'^2-a*c", 'rhs': '6', 'lhs_text': "b'^2-ac", 'rhs_text': '6'}], 'relations': [], 'final_answer': ''}, {})
    assert [c['status'] for c in result['claims']] == ['definition', 'definition', 'definition', 'ok']
    assert result['claims'][0]['detail'] == "b' の定義として記録しました"


def test_middle_dots_and_sqrt_without_braces():
    for lhs, rhs in [('1\\cdot(-5)', '-5'), ('1·(-5)', '-5'), ('1・(-5)', '-5'), ('4·1·(-m+3)', '-4*m+12'),
                     ('\\sqrt{(-1)^2-1\\cdot(-5)}', '\\sqrt6'),
                     ('x^2-2\\cdot 2\\sqrt2\\cdot x+(2\\sqrt2)^2', '(x-2\\sqrt2)^2')]:
        assert check_claim(lhs, rhs)['status'] == 'ok', (lhs, rhs)
    assert check_claim('1·(-5)', '5')['status'] == 'ng'


def test_slow_sympy_work_is_cut_off_instead_of_hanging():
    import time as _time

    from app.utils import math_verification as mv

    with pytest.raises(mv._TimeLimit):
        mv._with_time_limit(_time.sleep, 2, limit=0.2)
    assert mv._with_time_limit(lambda a, b: a + b, 1, 2, limit=1) == 3
    with pytest.raises(ZeroDivisionError):  # 計算中の例外はそのまま伝える
        mv._with_time_limit(lambda: 1 / 0, limit=1)
    # 入れ子の絶対値は解集合を求めずに「検算不能」にする（求めると止まらないことがある）
    start = _time.time()
    result = check_relation_step('abs(abs(x^2-2*x)-1)<1/2', 'x>0')
    assert result['status'] == 'unverifiable' and '入れ子' in result['detail'] and _time.time() - start < 2
    assert check_relation_step('abs(x^2-2*x)=1/2', 'x=1+sqrt(6)/2, 1-sqrt(6)/2, 1+sqrt(2)/2, 1-sqrt(2)/2')['status'] == 'ok'


def test_time_limit_applies_to_claims(monkeypatch):
    from app.utils import math_verification as mv

    def slow(*args):
        import time as _time
        _time.sleep(1)

    monkeypatch.setattr(mv, '_compare_exprs', slow)
    monkeypatch.setattr(mv, 'TIME_LIMIT', 0.1)
    result = check_claim('1+1', '2')
    assert result['status'] == 'unverifiable' and '時間がかかりすぎた' in result['detail']
