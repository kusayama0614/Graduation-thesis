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
    ('7', '練習1(1)', 'y=x+2', 'incomplete'),               # 形も点も読み取れていない（式は参考）
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


def _graph(question, **entry):
    return T.grade_transcribed(ITEMS[('7', question)], {'confidence': 'high', **entry})


def test_graph_grading():
    s1 = ITEMS[('7', '練習1(1)')]['expected_shape']
    s2 = ITEMS[('7', '練習1(2)')]['expected_shape']
    s3 = ITEMS[('7', '練習1(3)')]['expected_shape']
    assert _graph('練習1(1)', graph_shape=s1, graph_points='(-2,0),(0,2)')['judgment'] == 'correct'
    missing = _graph('練習1(1)', graph_shape=s1, graph_points='(-2, 0)')
    assert missing['judgment'] == 'incomplete' and '(0, 2)' in missing['grade_note']
    # 正解の点が書かれていれば，ほかの点や式が書かれていても正解（参考として説明に残す）
    extra = _graph('練習1(1)', graph_shape=s1, graph_points='(-2,0),(0,2),(2,4),(1,5)')
    assert extra['judgment'] == 'correct' and '(1, 5)' in extra['grade_note']
    assert _graph('練習1(1)', graph_shape=s1, graph_points='(-2,0),(0,2)',
                  initial_math='x≧-2 のとき y=x+2；x<-2 のとき y=x-2')['judgment'] == 'correct'
    # 正解の点が一つもなく，グラフ上にない点だけなら誤答（頂点の符号の誤り）
    off = _graph('練習1(1)', graph_shape=s1, graph_points='(2,0)')
    assert off['judgment'] == 'wrong' and off['method'] == 'graph_point_off_curve'
    assert _graph('練習1(1)', graph_shape=s1, graph_points='(2,0),(0,2)')['judgment'] == 'incomplete'
    assert _graph('練習1(1)', graph_shape='直線', graph_points='(-2,0),(0,2)')['judgment'] == 'wrong'
    assert _graph('練習1(2)', graph_shape=s2, graph_points='(-1,0),(3,0),(0,3),(1,4)',
                  initial_math='x≦-1, 3≦x のとき y=x^2-2x-3；-1<x<3 のとき y=-x^2+2x+3')['judgment'] == 'correct'
    assert _graph('練習1(2)', graph_shape=s2, graph_points='(-1,0),(3,0),(0,3),(1,-4)')['judgment'] == 'incomplete'
    assert _graph('練習1(3)', graph_shape=s3, graph_points='(-1,-1),(1,-1),(0,0),(-2,0),(2,0)')['judgment'] == 'correct'
    assert _graph('練習1(3)', graph_shape='', graph_points='')['judgment'] == 'blank'
    summary = T.question_results([_graph('練習1(1)', graph_shape=s1, graph_points='(-2,0),(0,2)')])[0]['answer']
    assert summary.startswith('形: V字') and '点: (-2,0),(0,2)' in summary
    assert '形の選択肢' in T.transcribe_prompt(T.key_items(7))
