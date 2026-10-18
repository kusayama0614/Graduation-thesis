import io
import json
import re
from datetime import date

import pytest

from app import db
from app.learning import explanation as chain
from app.learning import graph
from app.learning.profile import compute_profile
from app.models import AnswerSheet, Teacher
from app.utils.scoring import score_with_key
from tests.test_api_core import _register_and_login, client  # noqa: F401  (fixture の再利用)


# ---------------------------------------------------------------- テスト用の LLM

class FakeLLM:
    """決めたとおりに振る舞う LLM。計算は [[左辺 = 右辺]]，結論は「答え: X」で書く約束にしておく。"""

    model_name = 'fake-llm'

    def __init__(self, steps=None, writer=None, diagnosis=None):
        self.steps = steps
        self.writer = writer or (lambda user: '結果を示します。')
        self.diagnosis = diagnosis
        self.calls = []

    def generate_json(self, *, system, user, schema, schema_name, temperature=0.0):
        self.calls.append((schema_name, user))
        if schema_name == 'solution_steps':
            return {'steps': self.steps}
        if schema_name == 'error_diagnosis':
            enum = schema['properties']['node']['enum']
            return {'node': self.diagnosis if self.diagnosis in enum else enum[-1], 'reason': 'テスト用の理由'}
        if schema_name == 'calculation_claims':
            text = user.split('解説文:\n', 1)[1]
            claims = [{'lhs': l.strip(), 'rhs': r.strip(), 'lhs_text': l.strip(), 'rhs_text': r.strip()}
                      for l, r in re.findall(r'\[\[(.+?)=(.+?)\]\]', text)]
            final = re.search(r'答え[:：]\s*(\S+)', text)
            return {'claims': claims, 'final_answer': final.group(1) if final else ''}
        raise AssertionError(schema_name)

    def generate_text(self, *, system, user, temperature=0.3):
        self.calls.append(('text', user))
        return self.writer(user)

    def prompts_for_step(self, step_id):
        return [u for kind, u in self.calls if kind == 'text' and f'今回書くステップ: {step_id}.' in u]


# No.1 練習31: x^2 + mx - m + 3 = 0 が重解をもつ定数 m の値と，そのときの重解
STEPS = [
    {'title': '判別式を立てる', 'description': '係数を読み取り D を計算する', 'result': 'D = m^2+4m-12',
     'nodes': ['discriminant_setup']},
    {'title': 'D = 0 を解く', 'description': '重解をもつ条件 D=0 から m を求める', 'result': 'm = 2, -6',
     'nodes': ['discriminant_sign']},
    {'title': '重解を求める', 'description': 'x = -b/2a に代入する', 'result': 'x = -1, 3',
     'nodes': ['double_root']},
]
PROBLEM_TEXT = 'x^2 + mx - m + 3 = 0 が重解をもつような定数 m の値と，そのときの重解を求めよ。'


def correct_writer(user):
    if '今回書くステップ: 1.' in user:
        return 'D = m^2 - 4·1·(-m+3) です。[[m^2-4*1*(-m+3) = m^2+4m-12]]'
    if '今回書くステップ: 2.' in user:
        return '(m+6)(m-2)=0 より m = 2, -6 です。[[(2+6)*(2-2) = 0]]'
    return 'm=2 のとき [[-2/2 = -1]]，m=-6 のとき [[6/2 = 3]]。答え: -1,3'


# ---------------------------------------------------------------- 共通の準備

def _teacher_db_id(client):
    with client.application.app_context():
        return Teacher.query.filter_by(teacher_id='teacher_learning').first().id


def _login(client, llm=None):
    _register_and_login(client, teacher_id='teacher_learning', password='secret12')
    client.application.config['LEARNING_JOBS_INLINE'] = True
    client.application.config['LEARNING_LLM_CLIENT'] = llm


def _make_sheet(client, *, student_id, ocr_text, answer_key, test_name='No.1', exam_date='2026-09-16'):
    data = {
        'files': (io.BytesIO(b'sample'), 'sheet.pdf'),
        'test_name': test_name, 'subject': '数学I', 'exam_date': exam_date, 'student_id': student_id,
        'ocr_process': 'false', 'auto_score': 'false', 'generate_report': 'false',
    }
    res = client.post('/api/upload/answer-sheet', data=data, content_type='multipart/form-data')
    assert res.status_code == 200
    sheet_id = res.get_json()['results'][0]['record_id']
    with client.application.app_context():
        row = db.session.get(AnswerSheet, sheet_id)
        row.ocr_text = ocr_text
        row.answer_key = json.dumps(answer_key, ensure_ascii=False)
        db.session.commit()
    return sheet_id


NO1_KEY = {
    'unit': 'quadratic_functions',
    'answers': {'1': 'm<16/3', '2': 'm=2, -6'},
    'nodes': {'1': ['inequality_manipulation', 'exact_value'], '2': ['double_root']},
}


# ---------------------------------------------------------------- 前提知識依存グラフ

def test_graph_matches_thesis_tables():
    assert len(graph.node_ids('quadratic_functions')) == 11
    assert len(graph.node_ids('trigonometry')) == 7
    assert graph.unit_of_node('complementary_angle') == 'trigonometry'
    assert 'discriminant_setup' in graph.prerequisites('discriminant_sign')


def test_graph_cycle_is_rejected():
    with pytest.raises(graph.GraphError):
        graph._check_acyclic('u', ['a', 'b'], [{'from': 'a', 'to': 'b'}, {'from': 'b', 'to': 'a'}])


def test_graph_endpoint_requires_login_and_returns_rules(client):
    assert client.get('/api/learning/graph').status_code == 401
    _login(client)
    payload = client.get('/api/learning/graph').get_json()
    assert set(payload['graph']['units']) == {'quadratic_functions', 'trigonometry'}
    assert payload['decision_rules']['alpha'] == 0.3
    assert [e['element'] for e in payload['decision_rules']['elements']][0] == 'result'


# ---------------------------------------------------------------- 定着度の計算（3.2節）

def _rec(sheet, judgment, nodes, error_node=None, day=1):
    return {'answer_sheet_id': sheet, 'exercise_date': date(2026, 9, day), 'judgment': judgment,
            'nodes': nodes, 'error_node': error_node, 'exercise_name': sheet}


def test_confidence_is_exponential_moving_average():
    records = [
        _rec('s1', 'correct', ['A']), _rec('s1', 'wrong', ['A'], 'A'),  # r=0.5 → 0.3*0.5+0.7*0.5 = 0.5
        _rec('s2', 'wrong', ['A'], 'A', day=2),                          # r=0   → 0.7*0.5 = 0.35
    ]
    profile = compute_profile(records)
    state = profile['node_states']['A']
    assert state['confidence'] == pytest.approx(0.35)
    assert state['evidence_count'] == 3
    assert [h['correct_rate'] for h in state['history']] == [0.5, 0.0]
    assert profile['exercise_count'] == 2
    assert profile['error_tendencies'] == {'A': 2}


def test_exercises_are_applied_in_date_order():
    late_first = [_rec('late', 'correct', ['A'], day=9), _rec('early', 'wrong', ['A'], 'A', day=1)]
    state = compute_profile(late_first)['node_states']['A']
    # 早い演習(0) → 遅い演習(1): 0.35 → 0.3 + 0.245 = 0.545
    assert state['confidence'] == pytest.approx(0.545)


def test_error_is_attributed_only_to_diagnosed_node():
    profile = compute_profile([_rec('s1', 'wrong', ['A', 'B'], 'B')])
    assert 'A' not in profile['node_states']
    assert profile['node_states']['B']['confidence'] == pytest.approx(0.35)


def test_blank_counts_against_all_nodes_and_unreadable_is_ignored():
    profile = compute_profile([_rec('s1', 'blank', ['A', 'B']), _rec('s1', 'unreadable', ['C'])])
    assert profile['node_states']['A']['confidence'] == pytest.approx(0.35)
    assert profile['node_states']['B']['confidence'] == pytest.approx(0.35)
    assert 'C' not in profile['node_states']
    assert profile['judgment_counts'] == {'blank': 1, 'unreadable': 1}


def test_correct_with_form_issue_counts_as_correct():
    profile = compute_profile([_rec('s1', 'correct_form_issue', ['A'])])
    assert profile['node_states']['A']['confidence'] == pytest.approx(0.65)


# ---------------------------------------------------------------- フェーズA の API

def test_apply_exercise_with_math_verification_and_llm_diagnosis(client):
    llm = FakeLLM(diagnosis='inequality_manipulation')
    _login(client, llm)
    sheet_id = _make_sheet(client, student_id='S001', ocr_text='1 m≦16/3\n2 m=2', answer_key=NO1_KEY)

    res = client.post(f'/api/learning/answer-data/{sheet_id}/apply')
    assert res.status_code == 200
    body = res.get_json()
    items = {i['question']: i for i in body['items']}
    assert items['1']['judgment'] == 'wrong'          # 等号の有無が異なる（解集合で判定）
    assert items['2']['judgment'] == 'incomplete'     # 組の答えの一部だけ
    diagnoses = {d['question']: d for d in body['diagnoses']}
    assert diagnoses['1'] == {'question': '1', 'error_node': 'inequality_manipulation',
                              'diagnosis_method': 'llm', 'note': 'テスト用の理由'}
    assert diagnoses['2']['diagnosis_method'] == 'single_node'

    states = body['profile']['node_states']
    assert states['inequality_manipulation']['confidence'] == pytest.approx(0.35)
    assert states['double_root']['confidence'] == pytest.approx(0.35)
    assert 'exact_value' not in states  # 誤りは診断したノードだけに帰属
    assert body['profile']['error_tendencies'] == {'inequality_manipulation': 1, 'double_root': 1}

    # 同じプリントを再反映しても二重に更新されない
    again = client.post(f'/api/learning/answer-data/{sheet_id}/apply').get_json()
    assert again['profile']['node_states'] == body['profile']['node_states']
    profile = client.get('/api/learning/profiles/S001').get_json()['profile']
    assert len(profile['records']) == 2
    assert profile['records'][0]['initial_answer'] == 'm≦16/3'


def test_apply_without_llm_falls_back_to_first_node(client):
    _login(client, llm=None)
    sheet_id = _make_sheet(client, student_id='S001', ocr_text='1 m>16/3\n2 m=2, -6', answer_key=NO1_KEY)
    body = client.post(f'/api/learning/answer-data/{sheet_id}/apply').get_json()
    assert body['diagnoses'][0]['diagnosis_method'] == 'fallback_first_node'
    assert body['diagnoses'][0]['error_node'] == 'inequality_manipulation'
    assert body['profile']['node_states']['double_root']['confidence'] == pytest.approx(0.65)


def test_teacher_confirmed_results_take_priority(client):
    _login(client)
    sheet_id = _make_sheet(client, student_id='S002', ocr_text='', answer_key=NO1_KEY)
    res = client.put(f'/api/analysis/answer-data/{sheet_id}/scored-results', json={
        'score': 50, 'correct_count': 1, 'total_questions': 2,
        'question_results': [
            {'question': '1', 'result': 'incorrect', 'judgment': 'form_issue', 'answer': 'D<16/3', 'corrected': True},
            {'question': '2', 'result': 'correct'},
        ],
    })
    assert res.status_code == 200
    body = client.post(f'/api/learning/answer-data/{sheet_id}/apply').get_json()
    items = {i['question']: i for i in body['items']}
    assert items['1']['judgment'] == 'form_issue' and items['1']['corrected'] is True
    assert items['1']['answer'] == 'D<16/3'
    assert items['2']['judgment'] == 'correct' and items['2']['judge_method'] == 'teacher'


def test_inconsistent_teacher_judgment_is_rejected(client):
    _login(client)
    sheet_id = _make_sheet(client, student_id='S002', ocr_text='', answer_key=NO1_KEY)
    res = client.put(f'/api/analysis/answer-data/{sheet_id}/scored-results', json={
        'score': 50, 'correct_count': 1, 'total_questions': 2,
        'question_results': [{'question': '1', 'result': 'correct', 'judgment': 'wrong'}],
    })
    assert res.status_code == 400


def test_apply_requires_student_id_and_results(client):
    _login(client)
    no_student = _make_sheet(client, student_id='', ocr_text='1 A', answer_key=NO1_KEY)
    assert client.post(f'/api/learning/answer-data/{no_student}/apply').status_code == 400
    no_results = _make_sheet(client, student_id='S003', ocr_text='', answer_key=NO1_KEY)
    assert client.post(f'/api/learning/answer-data/{no_results}/apply').status_code == 400


def test_node_tags_endpoint_validates_nodes(client):
    _login(client)
    sheet_id = _make_sheet(client, student_id='S001', ocr_text='1 2', answer_key={'1': '2'})
    bad = client.put(f'/api/learning/answer-data/{sheet_id}/node-tags',
                     json={'unit': 'quadratic_functions', 'nodes': {'1': ['side_identification']}})
    assert bad.status_code == 400  # 別の単元のノード
    ok = client.put(f'/api/learning/answer-data/{sheet_id}/node-tags',
                    json={'unit': 'quadratic_functions', 'nodes': {'1': ['exact_value']}})
    assert ok.status_code == 200
    assert ok.get_json()['answer_key'] == {'answers': {'1': '2'}, 'unit': 'quadratic_functions',
                                           'nodes': {'1': ['exact_value']}}


def test_score_with_key_ignores_key_metadata():
    result = score_with_key('1 2', {'1': '2', 'unit': 'quadratic_functions', 'nodes': {'1': ['exact_value']}})
    assert result['total_questions'] == 1 and result['correct_count'] == 1


def test_csv_answer_key_with_nodes_column(tmp_path):
    from app.utils.answer_key_parser import parse_answer_key_file
    path = tmp_path / 'key.csv'
    path.write_text('question,answer,nodes\n1,m<16/3,inequality_manipulation;exact_value\n2,"m=2, -6",double_root\n',
                    encoding='utf-8')
    parsed = parse_answer_key_file(str(path), 'key.csv')
    assert parsed['candidates'][0]['nodes'] == ['inequality_manipulation', 'exact_value']
    assert parsed['candidates'][1] == {'question': '2', 'answer': 'm=2, -6', 'nodes': ['double_root']}


# ---------------------------------------------------------------- Chain 2 の判断（方式(1)）

def test_decision_switches_elements_by_confidence():
    step = {'id': 1, 'title': 't', 'nodes': ['double_root', 'discriminant_sign']}
    weak = chain.decide_step(step, {'double_root': {'confidence': 0.1, 'evidence_count': 4}}, {})
    assert weak['weakest_node'] == 'double_root'
    assert weak['elements'] == ['result', 'reasoning', 'definition_review', 'similar_check']
    assert any('0.10' in r and '0.5' in r for r in weak['reasons'])

    mid = chain.decide_step(step, {'double_root': {'confidence': 0.4, 'evidence_count': 4}}, {})
    assert mid['elements'] == ['result', 'reasoning']

    # 記録がないノードは初期値 0.5 → 閾値を下回らないので簡潔に書く
    fresh = chain.decide_step(step, {}, {})
    assert fresh['elements'] == ['result']


def test_error_caution_depends_on_tendency_and_confidence():
    step = {'id': 1, 'title': 't', 'nodes': ['double_root']}
    with_caution = chain.decide_step(step, {'double_root': {'confidence': 0.6, 'evidence_count': 3}}, {'double_root': 2})
    assert with_caution['elements'] == ['result', 'error_caution']
    assert with_caution['cautions'][0]['error_count'] == 2
    mastered = chain.decide_step(step, {'double_root': {'confidence': 0.8, 'evidence_count': 9}}, {'double_root': 2})
    assert mastered['elements'] == ['result']


def test_step_without_nodes_is_concise():
    decision = chain.decide_step({'id': 1, 'title': 't', 'nodes': []}, {}, {})
    assert decision['elements'] == ['result']


# ---------------------------------------------------------------- Chain 1 / 問題の登録

def test_create_problem_requires_llm_or_steps(client):
    _login(client, llm=None)
    base = {'unit': 'quadratic_functions', 'title': '練習31', 'problem_text': PROBLEM_TEXT}
    assert client.post('/api/learning/problems', json=base).status_code == 503
    res = client.post('/api/learning/problems', json={**base, 'steps': STEPS})
    assert res.status_code == 201
    problem = res.get_json()['problem']
    assert problem['steps_source'] == 'teacher'
    assert [s['id'] for s in problem['steps']] == [1, 2, 3]
    bad = client.post('/api/learning/problems', json={**base, 'steps': [{'title': 'x', 'nodes': ['no_such_node']}]})
    assert bad.status_code == 400


def test_chain1_output_is_constrained_to_graph_nodes(client):
    llm = FakeLLM(steps=STEPS)
    _login(client, llm)
    res = client.post('/api/learning/problems',
                      json={'unit': 'quadratic_functions', 'title': '練習31', 'problem_text': PROBLEM_TEXT})
    assert res.status_code == 201
    assert res.get_json()['problem']['steps_source'] == 'chain1'
    schema_call = [u for kind, u in llm.calls if kind == 'solution_steps'][0]
    assert 'double_root' in schema_call

    llm.steps = [{'title': 'x', 'description': '', 'result': '', 'nodes': ['side_identification']}]
    bad = client.post('/api/learning/problems',
                      json={'unit': 'quadratic_functions', 'title': '練習31', 'problem_text': PROBLEM_TEXT})
    assert bad.status_code == 400  # 単元外のノードは受け付けない


# ---------------------------------------------------------------- Chain 2〜4 と生成ジョブ

def _setup_problem_and_profiles(client, llm, correct_answer='-1, 3'):
    _login(client, llm)
    weak_sheet = _make_sheet(client, student_id='S001', ocr_text='1 m≦16/3\n2 m=2', answer_key=NO1_KEY)
    strong_sheet = _make_sheet(client, student_id='S002', ocr_text='1 16/3>m\n2 m=-6, 2', answer_key=NO1_KEY)
    for sheet_id in (weak_sheet, strong_sheet):
        assert client.post(f'/api/learning/answer-data/{sheet_id}/apply').status_code == 200
    res = client.post('/api/learning/problems', json={
        'unit': 'quadratic_functions', 'title': '練習31', 'problem_text': PROBLEM_TEXT,
        'correct_answer': correct_answer, 'steps': STEPS,
    })
    return res.get_json()['problem']['id']


def test_plan_explains_why_each_step_is_detailed(client):
    problem_id = _setup_problem_and_profiles(client, FakeLLM())
    plan = client.get(f'/api/learning/problems/{problem_id}/plan?student_id=S001').get_json()['plan']
    step3 = plan[2]
    assert step3['weakest_node'] == 'double_root' and step3['confidence'] == pytest.approx(0.35)
    assert step3['elements'] == ['result', 'reasoning', 'error_caution']
    assert any('0.35' in r and '閾値 0.5' in r for r in step3['reasons'])
    strong = client.get(f'/api/learning/problems/{problem_id}/plan?student_id=S002').get_json()['plan']
    assert strong[2]['elements'] == ['result']


def test_generation_writes_with_decided_policy_and_verifies(client):
    llm = FakeLLM(writer=correct_writer)
    problem_id = _setup_problem_and_profiles(client, llm)
    res = client.post(f'/api/learning/problems/{problem_id}/explanations', json={'student_ids': ['S001', 'S002']})
    assert res.status_code == 202
    job = res.get_json()['job']
    assert job['status'] == 'completed' and job['done'] == 2 and len(job['explanation_ids']) == 2

    detail = client.get(f'/api/learning/explanations/{job["explanation_ids"][0]}').get_json()['explanation']
    assert detail['status'] == 'ok' and detail['student_id'] == 'S001'
    assert detail['content'].startswith('# 練習31')
    step3 = detail['trace']['steps'][2]
    assert step3['attempts'][0]['counts']['ok'] == 2
    assert step3['attempts'][0]['final_check']['is_correct'] is True
    assert detail['profile_snapshot']['node_states']['double_root']['confidence'] == pytest.approx(0.35)

    # 方針の切り替えが LLM への指示そのものに反映されている
    weak_prompt, strong_prompt = llm.prompts_for_step(3)
    assert '途中の式変形を省略せず' in weak_prompt and '誤りの記録が1回' in weak_prompt
    assert '結果と使う公式だけを簡潔に' in strong_prompt and '誤りの記録' not in strong_prompt


def test_wrong_calculation_is_sent_back_to_chain2(client):
    def writer(user):
        if '今回書くステップ: 2.' in user and '前回書いた説明には' not in user:
            return 'D = 0 の判別式の値は [[(-4)^2 - 4*(-12) = 60]] です。'
        return correct_writer(user)

    llm = FakeLLM(writer=writer)
    problem_id = _setup_problem_and_profiles(client, llm)
    job = client.post(f'/api/learning/problems/{problem_id}/explanations',
                      json={'student_ids': ['S001']}).get_json()['job']
    detail = client.get(f'/api/learning/explanations/{job["explanation_ids"][0]}').get_json()['explanation']
    step2 = detail['trace']['steps'][1]
    assert detail['status'] == 'ok'
    assert step2['attempt_count'] == 2
    assert step2['attempts'][0]['claims'][0]['status'] == 'ng'
    assert '正しくは 64' in step2['attempts'][0]['feedback']
    assert '正しくは 64' in llm.prompts_for_step(2)[1]  # 差し戻し文が書き直しの指示に入る


def test_repeated_errors_escalate_to_teacher_review(client):
    def writer(user):
        if '今回書くステップ: 3.' in user:
            return '[[6/2 = 4]] 答え: -1,4'
        return correct_writer(user)

    problem_id = _setup_problem_and_profiles(client, FakeLLM(writer=writer))
    job = client.post(f'/api/learning/problems/{problem_id}/explanations',
                      json={'student_ids': ['S001']}).get_json()['job']
    detail = client.get(f'/api/learning/explanations/{job["explanation_ids"][0]}').get_json()['explanation']
    step3 = detail['trace']['steps'][2]
    assert detail['status'] == 'needs_review'
    assert step3['attempt_count'] == chain.MAX_ATTEMPTS
    assert step3['attempts'][0]['final_check']['is_correct'] is False
    assert any('教員の確認' in w for w in detail['trace']['warnings'])


def test_step_without_extracted_calculation_is_warned(client):
    def writer(user):
        if '今回書くステップ: 2.' in user:
            return 'm の値を求めます。'
        return correct_writer(user)

    problem_id = _setup_problem_and_profiles(client, FakeLLM(writer=writer))
    job = client.post(f'/api/learning/problems/{problem_id}/explanations',
                      json={'student_ids': ['S001']}).get_json()['job']
    detail = client.get(f'/api/learning/explanations/{job["explanation_ids"][0]}').get_json()['explanation']
    assert detail['status'] == 'ok'
    assert any('ステップ2: 計算が一つも抜き出されなかった' in w for w in detail['trace']['warnings'])


def test_master_subtractive_and_comparison(client):
    problem_id = _setup_problem_and_profiles(client, FakeLLM(writer=correct_writer))
    job = client.post(f'/api/learning/problems/{problem_id}/explanations', json={
        'student_ids': ['S001', 'S002'], 'include_master': True, 'include_subtractive': True,
    }).get_json()['job']
    assert job['total'] == 3 and job['errors'] == []
    kinds = [e['kind'] for e in client.get(f'/api/learning/problems/{problem_id}/explanations')
             .get_json()['explanations']]
    assert sorted(kinds) == ['individual', 'individual', 'master', 'subtractive', 'subtractive']

    comparison = client.get(f'/api/learning/problems/{problem_id}/comparison').get_json()
    weak = next(s for s in comparison['students'] if s['student_id'] == 'S001')
    assert weak['individual']['metrics']['weak_steps_detailed_ratio'] == 1.0
    assert weak['subtractive']['metrics']['weak_steps_detailed_ratio'] == 1.0
    assert weak['master_metrics']['weak_step_count'] == weak['individual']['metrics']['weak_step_count']
    assert comparison['between_students']['individual']['pair_count'] == 1


def test_generation_requires_llm_and_profiles(client):
    problem_id = _setup_problem_and_profiles(client, FakeLLM(writer=correct_writer))
    job = client.post(f'/api/learning/problems/{problem_id}/explanations',
                      json={'student_ids': ['S999']}).get_json()['job']
    assert job['errors'][0]['error'] == '学習者プロファイルがありません'
    assert job['status'] == 'failed'

    client.application.config['LEARNING_LLM_CLIENT'] = None
    res = client.post(f'/api/learning/problems/{problem_id}/explanations', json={'student_ids': ['S001']})
    assert res.status_code == 503


def test_problems_are_isolated_per_teacher(client):
    problem_id = _setup_problem_and_profiles(client, FakeLLM())
    client.post('/api/auth/logout')
    _register_and_login(client, teacher_id='other_teacher', password='secret12')
    assert client.get(f'/api/learning/problems/{problem_id}').status_code == 404
    assert client.get('/api/learning/profiles').get_json()['total'] == 0
