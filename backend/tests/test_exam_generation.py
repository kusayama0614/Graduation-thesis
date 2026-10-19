"""定期試験の解説をまとめて生成し，教員が確かめて生徒ごとに配布する流れのテスト。"""
import json

from app import db
from app.learning import exam_generation
from app.models import ExamImport, GeneratedExplanation, LearnerProfile, TestProblem
from tests.test_api_core import client  # noqa: F401  (fixture の再利用)
from tests.test_learning import PROBLEM_TEXT, STEPS, FakeLLM, _login, _teacher_db_id, correct_writer

WEAK = {'node_states': {'double_root': {'confidence': 0.3, 'evidence_count': 2}}, 'error_tendencies': {'double_root': 2}}
STRONG = {'node_states': {n: {'confidence': 0.9, 'evidence_count': 3}
                          for n in ('discriminant_setup', 'discriminant_sign', 'double_root')},
          'error_tendencies': {}}


def _set_profile(client, student_id, state):
    with client.application.app_context():
        teacher = _teacher_db_id(client)
        row = LearnerProfile.query.filter_by(teacher_id=teacher, student_id=student_id).first()
        if row is None:
            row = LearnerProfile(teacher_id=teacher, student_id=student_id)
            db.session.add(row)
        row.node_states = json.dumps(state['node_states'])
        row.error_tendencies = json.dumps(state['error_tendencies'])
        db.session.commit()


def _setup(client, llm):
    _login(client, llm)
    _set_profile(client, '1-1-1', WEAK)
    _set_profile(client, '1-1-2', WEAK)
    _set_profile(client, '1-1-10', STRONG)
    _set_profile(client, '1-2-1', STRONG)
    with client.application.app_context():
        teacher = _teacher_db_id(client)
        exam = ExamImport(teacher_id=teacher, name='2学期中間試験', status='saved')
        db.session.add(exam)
        db.session.flush()
        steps = [{'id': i + 1, **s} for i, s in enumerate(STEPS)]
        db.session.add(TestProblem(teacher_id=teacher, exam_id=exam.id, number='2(1)', unit='quadratic_functions',
                                   title='重解', problem_text=PROBLEM_TEXT, correct_answer='-1, 3',
                                   steps=json.dumps(steps, ensure_ascii=False), chain1_status='done'))
        db.session.add(TestProblem(teacher_id=teacher, exam_id=exam.id, number='10', unit='quadratic_functions',
                                   title='未分解', problem_text='y=|x^2-2x| のグラフをかけ', steps='[]',
                                   chain1_status='error'))
        db.session.commit()
        return exam.id


def _job(client, exam_id, **body):
    res = client.post(f'/api/learning/exams/{exam_id}/generation-jobs', json=body)
    assert res.status_code == 202, res.get_json()
    return res.get_json()['job']


def _pid(client, exam_id, number):
    with client.application.app_context():
        return TestProblem.query.filter_by(exam_id=exam_id, number=number).first().id


def _individual_rows(client):
    with client.application.app_context():
        rows = GeneratedExplanation.query.filter_by(kind='individual').order_by(GeneratedExplanation.created_at).all()
        return [(r.id, r.student_id, r.reused_from, json.loads(r.profile_snapshot)) for r in rows]


def test_number_key_and_class():
    assert sorted(['10', '2(1)', '1(2)', '1(1)'], key=exam_generation.number_key) == ['1(1)', '1(2)', '2(1)', '10']
    assert sorted(['1-1-10', '1-1-2'], key=exam_generation.number_key) == ['1-1-2', '1-1-10']
    assert exam_generation.class_of('1-1-20') == '1-1' and exam_generation.class_of('S001') is None


def test_plan_counts_distinct_decisions_without_calling_llm(client):  # noqa: F811
    llm = FakeLLM(writer=correct_writer)
    exam_id = _setup(client, llm)
    plan = client.get(f'/api/learning/exams/{exam_id}/generation-plan?classes=1-1').get_json()
    assert plan['student_count'] == 3
    first, second = plan['problems']
    assert first['number'] == '2(1)' and second['number'] == '10' and second['step_count'] == 0
    # 弱い2人は判断が同じなので1回だけ生成し，もう1人に使い回す
    assert (first['generate'], first['reuse'], first['skip']) == (2, 1, 0)
    assert first['llm_calls'] == 2 * exam_generation.estimate_calls(STEPS)
    assert not llm.calls


def test_exam_generation_reuses_skips_and_resumes(client):  # noqa: F811
    llm = FakeLLM(writer=correct_writer)
    exam_id = _setup(client, llm)
    only = [_pid(client, exam_id, '2(1)')]
    job = _job(client, exam_id, classes=['1-1'], problem_ids=only)
    assert job['status'] == 'completed' and job['total'] == 3
    assert (job['generated'], job['reused'], job['skipped'], job['done']) == (2, 1, 0, 3)
    rows = _individual_rows(client)
    by_student = {sid: (rid, src, snap) for rid, sid, src, snap in rows}
    assert by_student['1-1-2'][1] == by_student['1-1-1'][0]  # 同じ判断の生徒の解説を使い回す
    assert by_student['1-1-10'][1] is None
    assert by_student['1-1-10'][2]['node_states'] == STRONG['node_states']  # 記録は生徒自身の定着度

    # 判断が変わっていなければ作り直さない
    calls = len(llm.calls)
    job = _job(client, exam_id, classes=['1-1'], problem_ids=only)
    assert (job['generated'], job['reused'], job['skipped']) == (0, 0, 3) and len(llm.calls) == calls

    # 1-1-10 が弱くなった → 既にある解説を使い回す。1-2-1 は強い生徒の解説を使い回す
    _set_profile(client, '1-1-10', WEAK)
    job = _job(client, exam_id, problem_ids=only)
    assert (job['generated'], job['reused'], job['skipped']) == (0, 2, 2) and len(llm.calls) == calls

    # 使い回しをやめると生徒ごとに生成する
    _set_profile(client, '1-1-10', {'node_states': {}, 'error_tendencies': {}})
    job = _job(client, exam_id, student_ids=['1-1-10'], reuse=False, problem_ids=only)
    assert job['generated'] == 1


def test_exam_generation_pauses_on_quota_and_resumes(client):  # noqa: F811
    state = {'fail': True}

    def writer(user):
        if state['fail'] and '今回書くステップ: 1.' in user:
            raise RuntimeError('429 RESOURCE_EXHAUSTED: quota exceeded, retry in 41s')
        return correct_writer(user)

    exam_id = _setup(client, FakeLLM(writer=writer))
    job = _job(client, exam_id, classes=['1-1'], problem_ids=[_pid(client, exam_id, '2(1)')])
    assert job['status'] == 'paused' and '41s' in job['message']
    assert _individual_rows(client) == []

    state['fail'] = False
    res = client.post(f'/api/learning/exam-generation-jobs/{job["id"]}/resume')
    assert res.status_code == 202
    job = res.get_json()['job']
    assert job['status'] == 'completed' and job['generated'] == 2 and job['reused'] == 1
    assert client.get(f'/api/learning/exam-generation-jobs/{job["id"]}').get_json()['job']['done'] == 3


def test_master_for_exam(client):  # noqa: F811
    llm = FakeLLM(writer=correct_writer)
    exam_id = _setup(client, llm)
    only = [_pid(client, exam_id, '2(1)')]
    job = _job(client, exam_id, student_ids=['1-1-1'], include_master=True, problem_ids=only)
    assert job['status'] == 'completed'
    with client.application.app_context():
        kinds = sorted(r.kind for r in GeneratedExplanation.query.all())
    assert kinds == ['individual', 'master']  # 引き算型は作らない
    # マスター解説も指紋が同じなら作り直さない
    calls = len(llm.calls)
    _job(client, exam_id, student_ids=['1-1-2'], include_master=True, problem_ids=only)
    with client.application.app_context():
        assert GeneratedExplanation.query.filter_by(kind='master').count() == 1
    assert len(llm.calls) == calls


def test_review_summary_and_booklet(client):  # noqa: F811
    exam_id = _setup(client, FakeLLM(writer=correct_writer))
    _job(client, exam_id, classes=['1-1'])
    rows = _individual_rows(client)
    weak_root = next(rid for rid, sid, src, _ in rows if sid == '1-1-1')
    with client.application.app_context():
        db.session.get(GeneratedExplanation, weak_root).status = 'needs_review'
        for row in GeneratedExplanation.query.filter_by(reused_from=weak_root).all():
            row.status = 'needs_review'
        db.session.commit()

    summary = client.get(f'/api/learning/exams/{exam_id}/explanations-summary?classes=1-1').get_json()
    first = summary['problems'][0]
    assert first['answer_check']['status'] == 'match'
    assert (first['with_explanation'], first['distinct_explanations'], first['pending_review']) == (3, 2, 2)
    assert summary['problems'][1]['answer_check']['status'] == 'unchecked'
    [item] = summary['review']
    assert item['explanation_id'] == weak_root and item['student_ids'] == ['1-1-1', '1-1-2']

    booklet = client.get(f'/api/learning/exams/{exam_id}/booklet?classes=1-1').get_json()
    assert [s['student_id'] for s in booklet['students']] == ['1-1-1', '1-1-2', '1-1-10']
    assert not booklet['students'][0]['ready'] and booklet['students'][0]['missing'] == 0
    common_item = booklet['students'][0]['items'][1]  # 解答ステップのない大問10は全員共通の解説
    assert common_item['common'] and common_item['ready'] and common_item['content'].startswith('# 未分解')
    assert len({s['items'][1]['explanation_id'] for s in booklet['students']}) == 1

    # 教員が直すと，同じ解説を使い回した生徒にも反映される
    res = client.put(f'/api/learning/explanations/{weak_root}', json={'content': '直した解説'})
    assert res.status_code == 200 and res.get_json()['updated'] == 2
    body = client.get(f'/api/learning/explanations/{weak_root}').get_json()['explanation']
    assert body['reviewed'] and body['edited'] and body['original_content'] != '直した解説'
    summary = client.get(f'/api/learning/exams/{exam_id}/explanations-summary?classes=1-1').get_json()
    assert summary['review'] == [] and summary['problems'][0]['reviewed'] == 2
    booklet = client.get(f'/api/learning/exams/{exam_id}/booklet?student_ids=1-1-2').get_json()
    assert booklet['students'][0]['items'][0]['content'] == '直した解説'
    assert booklet['students'][0]['items'][0]['ready']

    assert client.put(f'/api/learning/explanations/{weak_root}', json={'content': '  '}).status_code == 400


def test_answer_check_flags_mismatch(client):  # noqa: F811
    exam_id = _setup(client, FakeLLM(writer=correct_writer))
    with client.application.app_context():
        problem = TestProblem.query.filter_by(exam_id=exam_id, number='2(1)').first()
        problem.correct_answer = '-1, 5'
        db.session.commit()
    summary = client.get(f'/api/learning/exams/{exam_id}/explanations-summary').get_json()
    assert summary['problems'][0]['answer_check']['status'] == 'mismatch'


def test_exam_generation_requires_students_and_problems(client):  # noqa: F811
    exam_id = _setup(client, FakeLLM(writer=correct_writer))
    res = client.post(f'/api/learning/exams/{exam_id}/generation-jobs', json={'classes': ['3-9']})
    assert res.status_code == 400
    res = client.post(f'/api/learning/exams/{exam_id}/generation-jobs', json={'problem_ids': ['nope']})
    assert res.status_code == 400
    assert client.post('/api/learning/exams/nope/generation-jobs', json={}).status_code == 404


def test_problem_without_steps_gets_one_common_explanation(client):  # noqa: F811
    llm = FakeLLM(writer=correct_writer)
    exam_id = _setup(client, llm)
    plan = client.get(f'/api/learning/exams/{exam_id}/generation-plan?classes=1-1').get_json()
    common_plan = plan['problems'][1]
    assert common_plan['common'] and (common_plan['generate'], common_plan['reuse'], common_plan['llm_calls']) == (1, 2, 2)

    job = _job(client, exam_id, classes=['1-1'], problem_ids=[_pid(client, exam_id, '10')])
    assert job['status'] == 'completed' and (job['generated'], job['reused'], job['done'], job['total']) == (1, 2, 3, 3)
    common_prompts = [u for kind, u in llm.calls if kind == 'text']
    assert len(common_prompts) == 1 and 'y=|x^2-2x|' in common_prompts[0]
    assert '**指針**' in common_prompts[0] and '……①' in common_prompts[0]  # 参考書の解答編の構成と文体で書かせる
    with client.application.app_context():
        [row] = GeneratedExplanation.query.filter_by(kind='common').all()
        assert row.student_id is None and row.status == 'ok'
        trace = json.loads(row.trace)
        assert trace['steps'][0]['attempts'][0]['claims']  # 共通の解説も Chain 4 で検算する
        common_id = row.id

    # 作り直さない（指紋が同じ）。全員分そろったので要確認もなし
    calls = len(llm.calls)
    job = _job(client, exam_id, classes=['1-1'], problem_ids=[_pid(client, exam_id, '10')])
    assert job['skipped'] == 3 and len(llm.calls) == calls
    summary = client.get(f'/api/learning/exams/{exam_id}/explanations-summary?classes=1-1').get_json()
    assert summary['problems'][1]['common'] and summary['problems'][1]['with_explanation'] == 3

    # 要確認の共通の解説は review に出て，確かめると全員分が配布できる
    with client.application.app_context():
        db.session.get(GeneratedExplanation, common_id).status = 'needs_review'
        db.session.commit()
    summary = client.get(f'/api/learning/exams/{exam_id}/explanations-summary?classes=1-1').get_json()
    assert [r['explanation_id'] for r in summary['review']] == [common_id] and summary['review'][0]['common']
    assert summary['problems'][1]['pending_review'] == 3
    assert client.put(f'/api/learning/explanations/{common_id}', json={}).get_json()['updated'] == 1
    booklet = client.get(f'/api/learning/exams/{exam_id}/booklet?student_ids=1-1-1').get_json()
    assert booklet['students'][0]['items'][1]['ready']


def test_common_explanation_is_regenerated_until_verified(client):  # noqa: F811
    def writer(user):
        return '[[2*3 = 7]] 答え: 7'
    exam_id = _setup(client, FakeLLM(writer=writer))
    _job(client, exam_id, student_ids=['1-1-1'], problem_ids=[_pid(client, exam_id, '10')])
    with client.application.app_context():
        [row] = GeneratedExplanation.query.filter_by(kind='common').all()
        trace = json.loads(row.trace)
    assert row.status == 'needs_review' and trace['steps'][0]['attempt_count'] == 3
    assert any('上限' in w for w in trace['warnings'])


def test_common_signature_changes_with_style_version(monkeypatch):
    problem = {'problem_text': 'y=|x^2-2x| のグラフをかけ', 'correct_answer': None, 'steps': []}
    before = exam_generation.common_signature(problem, 'm')
    monkeypatch.setattr(exam_generation.chain, 'COMMON_STYLE_VERSION', 99)
    assert exam_generation.common_signature(problem, 'm') != before  # 書き方を変えたら作り直す


# ---------------------------------------------------------------- 要確認の解説だけを直す

def _mark_needs_review(client, student_id, step_index, wrong_rhs=None):
    """生成済みの解説を「要確認」にする（wrong_rhs を与えると，そのステップの記録の計算を誤りにする）。"""
    with client.application.app_context():
        row = (GeneratedExplanation.query.filter_by(kind='individual', student_id=student_id)
               .order_by(GeneratedExplanation.created_at.desc()).first())
        root = row.reused_from or row.id
        for target in GeneratedExplanation.query.filter(db.or_(GeneratedExplanation.id == root,
                                                               GeneratedExplanation.reused_from == root)).all():
            trace = json.loads(target.trace)
            step = trace['steps'][step_index]
            step['status'] = 'needs_review'
            if wrong_rhs is not None:
                step['attempts'][-1]['claims'][0]['rhs'] = wrong_rhs
            target.trace = json.dumps(trace, ensure_ascii=False)
            target.status = 'needs_review'
        db.session.commit()
        return root


def _latest_individual(client, student_id):
    with client.application.app_context():
        row = (GeneratedExplanation.query.filter_by(kind='individual', student_id=student_id)
               .order_by(GeneratedExplanation.created_at.desc()).first())
        return row.id, row.status, row.reused_from, json.loads(row.trace)


def test_repair_fixes_by_recheck_without_calling_llm(client):  # noqa: F811
    llm = FakeLLM(writer=correct_writer)
    exam_id = _setup(client, llm)
    only = [_pid(client, exam_id, '2(1)')]
    _job(client, exam_id, classes=['1-1'], problem_ids=only)
    # 検算の不具合で要確認になっていた（記録の計算は正しい）解説: 1-1-1 と 1-1-2 は同じ解説を使い回している
    _mark_needs_review(client, '1-1-1', 1)

    plan = client.get(f'/api/learning/exams/{exam_id}/repair-plan?classes=1-1').get_json()['plan']
    assert (plan['groups'], plan['explanations'], plan['fixed_by_recheck'], plan['rewrite_steps'], plan['llm_calls']) \
        == (1, 2, 1, 0, 0)

    calls = len(llm.calls)
    res = client.post(f'/api/learning/exams/{exam_id}/repair-jobs', json={'classes': ['1-1']})
    assert res.status_code == 202
    job = res.get_json()['job']
    assert job['status'] == 'completed' and (job['generated'], job['skipped'], job['reused'], job['done']) == (0, 1, 1, 1)
    assert len(llm.calls) == calls  # Gemini は呼ばない
    first_id, status, reused_from, trace = _latest_individual(client, '1-1-1')
    assert status == 'ok' and reused_from is None and trace['steps'][1]['attempts'][-1]['rechecked']
    _, status, reused_from, _ = _latest_individual(client, '1-1-2')
    assert status == 'ok' and reused_from == first_id

    # もう直すものはない
    assert client.post(f'/api/learning/exams/{exam_id}/repair-jobs', json={'classes': ['1-1']}).status_code == 400


def test_repair_rewrites_only_the_failing_step(client):  # noqa: F811
    llm = FakeLLM(writer=correct_writer)
    exam_id = _setup(client, llm)
    _job(client, exam_id, student_ids=['1-1-10'], problem_ids=[_pid(client, exam_id, '2(1)')])
    _, _, _, before = _latest_individual(client, '1-1-10')
    _mark_needs_review(client, '1-1-10', 1, wrong_rhs='5')  # ステップ2の記録の計算が誤り

    plan = client.get(f'/api/learning/exams/{exam_id}/repair-plan?student_ids=1-1-10').get_json()['plan']
    assert (plan['rewrite_groups'], plan['rewrite_steps'], plan['llm_calls']) == (1, 1, 2)

    calls = len(llm.calls)
    job = client.post(f'/api/learning/exams/{exam_id}/repair-jobs', json={'student_ids': ['1-1-10']}).get_json()['job']
    assert job['status'] == 'completed' and job['generated'] == 1
    new_calls = llm.calls[calls:]
    texts = [u for kind, u in new_calls if kind == 'text']
    assert len(texts) == 1 and '今回書くステップ: 2.' in texts[0] and '誤り' in texts[0]  # 指摘を添えてステップ2だけ
    _, status, _, after = _latest_individual(client, '1-1-10')
    assert status == 'ok'
    assert after['steps'][0]['text'] == before['steps'][0]['text'] and after['steps'][2]['text'] == before['steps'][2]['text']
    assert after['steps'][1]['attempt_count'] == before['steps'][1]['attempt_count'] + 1


def test_repair_skips_reviewed_explanations(client):  # noqa: F811
    exam_id = _setup(client, FakeLLM(writer=correct_writer))
    _job(client, exam_id, student_ids=['1-1-10'], problem_ids=[_pid(client, exam_id, '2(1)')])
    root = _mark_needs_review(client, '1-1-10', 1, wrong_rhs='5')
    client.put(f'/api/learning/explanations/{root}', json={})  # 教員が確認済みにした
    assert client.get(f'/api/learning/exams/{exam_id}/repair-plan').get_json()['plan']['groups'] == 0
    assert client.post(f'/api/learning/exams/{exam_id}/repair-jobs', json={}).status_code == 400


def test_repair_common_explanation_by_recheck(client):  # noqa: F811
    llm = FakeLLM(writer=correct_writer)
    exam_id = _setup(client, llm)
    _job(client, exam_id, classes=['1-1'], problem_ids=[_pid(client, exam_id, '10')])
    with client.application.app_context():
        row = GeneratedExplanation.query.filter_by(kind='common').first()
        row.status = 'needs_review'
        original = row.content
        db.session.commit()
    calls = len(llm.calls)
    job = client.post(f'/api/learning/exams/{exam_id}/repair-jobs', json={'classes': ['1-1']}).get_json()['job']
    assert job['status'] == 'completed' and job['skipped'] == 1 and len(llm.calls) == calls
    with client.application.app_context():
        latest = GeneratedExplanation.query.filter_by(kind='common').order_by(GeneratedExplanation.created_at.desc()).first()
        assert latest.status == 'ok' and latest.content == original
