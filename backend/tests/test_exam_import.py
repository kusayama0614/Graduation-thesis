import io

import pymupdf

from app.learning import exam_import
from tests.test_api_core import client  # noqa: F401  (fixture の再利用)
from tests.test_learning import FakeLLM, _login

STEPS = [{'title': '判別式を立てる', 'description': '係数を読み取る', 'result': 'D = 16 - 8m', 'nodes': ['discriminant_setup']}]


def _pdf(pages):
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_text((72, 72), text, fontsize=20)
    data = doc.tobytes()
    doc.close()
    return data


class FakeExamReader:
    model_name = 'fake-vision'

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def read_images_json(self, *, system, user, images, schema, schema_name, thinking_level=None):
        self.calls.append((user, len(images), schema))
        if self.fail:
            raise RuntimeError('quota')
        return {'exam_title': '2学期中間試験', 'questions': [
            {'number': '1(1)', 'title': '2次方程式を解く', 'unit': 'quadratic_functions',
             'problem_text': '次の2次方程式を解け。$2x^2+5x-3=0$', 'correct_answer': 'x=-3, 1/2',
             'answer_display': 'x=−3, 1/2', 'answer_format': 'answer_only', 'points': 3, 'figure_note': '',
             'confidence': 'high'},
            {'number': '6(1)', 'title': '三角比の相互関係', 'unit': 'trigonometry',
             'problem_text': '$\\\\theta$ が鋭角で $\\\\cos\\\\theta=3/4$ のとき $\\\\sin\\\\theta, \\\\tan\\\\theta$ を求めよ。',
             'correct_answer': 'sqrt(7)/4, sqrt(7)/3', 'answer_display': '', 'answer_format': 'answer_only',
             'points': 4, 'figure_note': '', 'confidence': 'low'},
            {'number': '12', 'title': 'グラフと共有点', 'unit': 'unknown', 'problem_text': 'y=|x^2-2x| のグラフを書け',
             'correct_answer': '', 'answer_display': '', 'answer_format': 'written', 'points': 'x',
             'figure_note': 'グラフ', 'confidence': 'high'},
        ]}


def _upload(client, answer=True, name=''):  # noqa: F811
    data = {'problem_pdf': (io.BytesIO(_pdf(['problem 1', 'problem 2'])), 'exam.pdf'), 'name': name}
    if answer:
        data['answer_pdf'] = (io.BytesIO(_pdf(['answer 1'])), 'answer.pdf')
    return client.post('/api/learning/exam-imports', data=data, content_type='multipart/form-data')


def test_exam_pdf_import_reads_saves_and_decomposes(client, tmp_path):  # noqa: F811
    llm = FakeLLM(steps=STEPS)
    _login(client, llm)
    reader = FakeExamReader()
    client.application.config.update(BULK_VISION_CLIENT=reader, UPLOAD_FOLDER=str(tmp_path / 'uploads'))

    res = _upload(client)
    assert res.status_code == 201
    exam = res.get_json()['exam']
    assert exam['status'] == 'ready' and exam['name'] == '2学期中間試験'  # 名前が空ならファイル名→試験名
    assert [p['kind'] for p in exam['pages']] == ['problem', 'problem', 'answer']
    user, image_count, schema = reader.calls[0]
    assert image_count == 3 and '画像 1〜2 は定期試験の問題用紙' in user and '画像 3〜3 は模範解答' in user
    assert schema['properties']['questions']['items']['properties']['unit']['enum'] == ['quadratic_functions', 'trigonometry']
    candidates = exam['candidates']
    assert [c['number'] for c in candidates] == ['1(1)', '6(1)', '12']
    assert candidates[2]['unit'] == 'quadratic_functions' and candidates[2]['points'] is None  # 不正な値は直す
    assert client.get(f'/api/learning/exam-imports/{exam["id"]}/pages/2/image').status_code == 200
    assert client.get(f'/api/learning/exam-imports/{exam["id"]}/pages/9/image').status_code == 404

    to_save = candidates[:2]
    to_save[1]['correct_answer'] = 'sqrt(7)/4, sqrt(7)/3'
    res = client.post(f'/api/learning/exam-imports/{exam["id"]}/save', json={'questions': to_save})
    assert res.status_code == 201
    body = res.get_json()
    assert body['exam']['status'] == 'saved' and body['exam']['problem_count'] == 2
    # 三角比の問題に2次関数のノードを返したので，そちらは分解の検証で失敗として残る（1問の失敗で止めない）
    assert body['exam']['chain1'] == {'pending': 0, 'done': 1, 'error': 1}
    problems = {p['number']: p for p in client.get('/api/learning/problems').get_json()['problems']}
    assert problems['6(1)']['chain1_status'] == 'error' and problems['6(1)']['chain1_error']
    assert problems['1(1)']['title'] == '大問1(1) 2次方程式を解く' and problems['1(1)']['points'] == 3
    assert problems['6(1)']['unit'] == 'trigonometry' and problems['1(1)']['chain1_status'] == 'done'
    assert problems['1(1)']['steps'][0]['result'] == 'D = 16 - 8m'
    assert problems['1(1)']['correct_answer'] == 'x=-3, 1/2' and problems['1(1)']['exam_id'] == exam['id']
    assert client.post(f'/api/learning/exam-imports/{exam["id"]}/save', json={'questions': [
        {'number': '2', 'unit': 'other', 'problem_text': 'x'}]}).status_code == 400


def test_exam_import_failure_can_be_reread_and_chain1_retried(client, tmp_path):  # noqa: F811
    _login(client, None)  # Gemini のキーがないので Chain 1 は失敗する
    reader = FakeExamReader(fail=True)
    client.application.config.update(BULK_VISION_CLIENT=reader, UPLOAD_FOLDER=str(tmp_path / 'uploads'))
    exam = _upload(client, answer=False, name='中間試験').get_json()['exam']
    assert exam['status'] == 'error' and 'quota' in exam['error'] and exam['name'] == '中間試験'
    assert '模範解答の画像はありません' in reader.calls[0][0]
    reader.fail = False
    exam = client.post(f'/api/learning/exam-imports/{exam["id"]}/read').get_json()['exam']
    assert exam['status'] == 'ready' and len(exam['candidates']) == 3 and exam['name'] == '中間試験'
    body = client.post(f'/api/learning/exam-imports/{exam["id"]}/save', json={'questions': exam['candidates'][:1]}).get_json()
    assert body['exam']['chain1']['error'] == 1
    client.application.config['LEARNING_LLM_CLIENT'] = FakeLLM(steps=STEPS)
    exam = client.post(f'/api/learning/exam-imports/{exam["id"]}/chain1').get_json()['exam']
    assert exam['chain1'] == {'pending': 0, 'done': 1, 'error': 0}
    assert len(client.get('/api/learning/exam-imports').get_json()['exams']) == 1


def test_exam_import_requires_pdf_and_vision(client, tmp_path):  # noqa: F811
    _login(client)
    client.application.config.update(UPLOAD_FOLDER=str(tmp_path / 'uploads'))
    assert client.post('/api/learning/exam-imports', data={}, content_type='multipart/form-data').status_code == 400
    assert _upload(client).status_code == 503  # Gemini のキーがない
    assert exam_import.problem_title({'number': '3(2)', 'title': '接線'}) == '大問3(2) 接線'
