import pytest

from app.learning import graph
from app.utils import transcription as T

ITEMS = {(p, q['question']): q for p, v in T.load_answer_keys().items() for q in v['questions']}


@pytest.mark.parametrize('print_no,question,answer,expected', [
    ('1', '練習29(1)', '2個', 'correct'),
    ('1', '練習30', '16/3>m', 'correct'),
    ('1', '練習30', 'm>16/3', 'wrong'),           # 不等号の向き
    ('1', '練習30', 'm≦16/3', 'wrong'),           # 等号の扱い
    ('1', '練習30', 'm<5', 'wrong'),              # 近似
    ('1', '練習31', 'm=-6 のとき x=3；m=2 のとき x=-1', 'correct'),
    ('1', '練習31', 'm=2 のとき x=-1', 'incomplete'),
    ('1', '練習31', 'm=2，m=-6', 'incomplete'),   # 重解を求めていない
    ('1', '練習31', 'm=2 のとき x=1；m=-6 のとき x=3', 'wrong'),  # 重解の符号
    ('2', '練習34', 'm<1/4 のとき 2；m=1/4 のとき 1；m>1/4 のとき 0', 'correct'),
    ('2', '練習34', 'm<4 のとき 2；m=4 のとき 1；m>4 のとき 0', 'wrong'),
    ('2', '練習1(1)', '(7,-18),(2,-3)', 'correct'),
    ('2', '練習2', 'k=-1', 'incomplete'),
    ('3', '練習39(1)', 'x≠-3', 'correct'),
    ('3', '練習39(3)', '解なし', 'correct'),
    ('3', '練習39(2)', '全ての実数', 'correct'),
    ('3', '練習39(2)', 'x=4', 'wrong'),
    ('8', '練習1(3)', '1/2, sqrt(3)/2, sqrt(3)', 'wrong'),    # 対辺・隣辺の取り違え（順序で判定）
    ('8', '練習1(3)', 'sqrt(3)/2, 1/2', 'incomplete'),
    ('8', '練習2(1)', 'sqrt(3)/2, sqrt(3)/3', 'correct'),
    ('8', '練習3(1)', '0.41', 'wrong'),                     # 表の値を粗く丸めた
    ('8', '練習3(3)', '2.246', 'correct'),
    ('8', '練習4(1)', 'sin15°', 'wrong'),
    ('8', '練習5', '171m, 460.85', 'wrong'),
    ('8', '練習6', '26.8m', 'correct'),
    ('8', '練習7', '約6°', 'correct'),
    ('9', '練習10(1)', 'sin74°', 'wrong'),                  # 値は等しいが求める形ではない
    ('9', '練習10(3)', '1/tan25°', 'correct'),
    ('7', '練習1(1)', 'y=x+2', 'unmarked'),                 # グラフは自動判定しない
    ('1', '練習30', '', 'blank'),
])
def test_grade_item(print_no, question, answer, expected):
    assert T.grade_item(ITEMS[(print_no, question)], answer)['judgment'] == expected


def test_builtin_keys_are_consistent():
    for print_no, entry in T.load_answer_keys().items():
        assert entry['unit'] in graph.unit_ids()
        for q in entry['questions']:
            assert not graph.validate_node_ids(q['nodes'], entry['unit']), (print_no, q['question'])
            if q['kind'] not in ('graph',):
                # 正解そのものは正解と判定される
                expected = q['expected']
                answer = '；'.join(f'{c} のとき {v}' for c, v in expected) if q['kind'] == 'cases' else (
                    q.get('accept', [expected])[0] if q['kind'] == 'text' else expected)
                assert T.grade_item(q, answer)['judgment'] == 'correct', (print_no, q['question'])
    key = T.answer_key_for(8)
    assert key['unit'] == 'trigonometry' and key['nodes']['練習5'] == ['application_modeling', 'trig_table_reading']
    assert T.answer_key_for(99) is None


def test_form_issue_and_review_flags():
    q = ITEMS[('8', '練習4(1)')]
    item = T.grade_transcribed(q, {'initial_math': 'sin15°', 'form_issue': True, 'confidence': 'low'})
    assert item['judgment'] == 'form_issue' and item['needs_review'] is True
    assert T.grade_transcribed(q, {'initial_math': '', 'blank': True})['judgment'] == 'blank'
    assert T.grade_transcribed(q, {'initial_math': '15', 'unreadable': True})['judgment'] == 'unreadable'
