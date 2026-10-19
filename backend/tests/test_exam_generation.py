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
    job = _job(client, exam_id, classes=['1-1'])
    assert job['status'] == 'completed' and job['total'] == 3
    assert (job['generated'], job['reused'], job['skipped'], job['done']) == (2, 1, 0, 3)
    rows = _individual_rows(client)
    by_student = {sid: (rid, src, snap) for rid, sid, src, snap in rows}
    assert by_student['1-1-2'][1] == by_student['1-1-1'][0]  # 同じ判断の生徒の解説を使い回す
    assert by_student['1-1-10'][1] is None
    assert by_student['1-1-10'][2]['node_states'] == STRONG['node_states']  # 記録は生徒自身の定着度

    # 判断が変わっていなければ作り直さない
    calls = len(llm.calls)
    job = _job(client, exam_id, classes=['1-1'])
    assert (job['generated'], job['reused'], job['skipped']) == (0, 0, 3) and len(llm.calls) == calls

    # 1-1-10 が弱くなった → 既にある解説を使い回す。1-2-1 は強い生徒の解説を使い回す
    _set_profile(client, '1-1-10', WEAK)
    job = _job(client, exam_id)
    assert (job['generated'], job['reused'], job['skipped']) == (0, 2, 2) and len(llm.calls) == calls

    # 使い回しをやめると生徒ごとに生成する
    _set_profile(client, '1-1-10', {'node_states': {}, 'error_tendencies': {}})
    job = _job(client, exam_id, student_ids=['1-1-10'], reuse=False)
    assert job['generated'] == 1


def test_exam_generation_pauses_on_quota_and_resumes(client):  # noqa: F811
    state = {'fail': True}

    def writer(user):
        if state['fail'] and '今回書くステップ: 1.' in user:
            raise RuntimeError('429 RESOURCE_EXHAUSTED: quota exceeded, retry in 41s')
        return correct_writer(user)

    exam_id = _setup(client, FakeLLM(writer=writer))
    job = _job(client, exam_id, classes=['1-1'])
    assert job['status'] == 'paused' and '41s' in job['message']
    assert _individual_rows(client) == []

    state['fail'] = False
    res = client.post(f'/api/learning/exam-generation-jobs/{job["id"]}/resume')
    assert res.status_code == 202
    job = res.get_json()['job']
    assert job['status'] == 'completed' and job['generated'] == 2 and job['reused'] == 1
    assert client.get(f'/api/learning/exam-generation-jobs/{job["id"]}').get_json()['job']['done'] == 3


def test_master_and_subtractive_for_exam(client):  # noqa: F811
    llm = FakeLLM(writer=correct_writer)
    exam_id = _setup(client, llm)
    job = _job(client, exam_id, student_ids=['1-1-1'], include_subtractive=True)
    assert job['status'] == 'completed'
    with client.application.app_context():
        kinds = sorted(r.kind for r in GeneratedExplanation.query.all())
    assert kinds == ['individual', 'master', 'subtractive']
    # マスター解説も指紋が同じなら作り直さない
    calls = len(llm.calls)
    _job(client, exam_id, student_ids=['1-1-2'], include_subtractive=True)
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
    assert booklet['students'][0]['items'][1]['excluded']  # 解答ステップのない大問10は対象外

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


def test_exam_generation_requires_students_and_steps(client):  # noqa: F811
    exam_id = _setup(client, FakeLLM(writer=correct_writer))
    res = client.post(f'/api/learning/exams/{exam_id}/generation-jobs', json={'classes': ['3-9']})
    assert res.status_code == 400
    with client.application.app_context():
        problem_id = TestProblem.query.filter_by(exam_id=exam_id, number='10').first().id
    res = client.post(f'/api/learning/exams/{exam_id}/generation-jobs', json={'problem_ids': [problem_id]})
    assert res.status_code == 400
    assert client.post('/api/learning/exams/nope/generation-jobs', json={}).status_code == 404
