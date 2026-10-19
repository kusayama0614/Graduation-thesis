import hashlib
import io
import json

import pymupdf
import pytest

from app.models import AnswerSheet, ScannedPage
from app.utils import bulk_import as bulk
from tests.test_api_core import _register_and_login, client  # noqa: F401  (fixture の再利用)


def _pdf(pages):
    """テスト用の PDF（ページごとに違う文字を書いて，画像が区別できるようにする）。"""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=24)
    data = doc.tobytes()
    doc.close()
    return data


class FakeVision:
    """ページ画像（の内容）ごとに決めた見出しを返す。"""

    model_name = 'fake-vision'

    def __init__(self):
        self.by_hash = {}
        self.calls = 0
        self.fail = set()

    def set(self, image_path, reading):
        with open(image_path, 'rb') as f:
            self.by_hash[hashlib.sha1(f.read()).hexdigest()] = reading

    def read_image_json(self, *, system, user, image_bytes, mime_type, schema, schema_name, thinking_level=None):
        self.calls += 1
        digest = hashlib.sha1(image_bytes).hexdigest()
        if digest in self.fail:
            raise RuntimeError('quota')
        return self.by_hash.get(digest, {'has_header': False, 'print_no': 0, 'title': '', 'grade': 0,
                                         'class_no': 0, 'student_no': 0, 'confidence': 'high', 'note': ''})


def front(print_no, grade, class_no, number, confidence='high', title='2次関数'):
    return {'has_header': True, 'print_no': print_no, 'title': title, 'grade': grade, 'class_no': class_no,
            'student_no': number, 'confidence': confidence, 'note': ''}


@pytest.fixture()
def setup(client, tmp_path):  # noqa: F811
    _register_and_login(client)
    vision = FakeVision()
    client.application.config.update(BULK_VISION_CLIENT=vision, BULK_JOBS_INLINE=True,
                                     UPLOAD_FOLDER=str(tmp_path / 'uploads'))
    return client, vision


def _upload(client, batch_id, name, pages, **form):
    data = {'file': (io.BytesIO(_pdf(pages)), name), **{k: str(v) for k, v in form.items()}}
    res = client.post(f'/api/bulk/batches/{batch_id}/files', data=data, content_type='multipart/form-data')
    assert res.status_code == 201, res.get_json()
    return res.get_json()['file']


def _pages(client, batch_id):
    with client.application.app_context():
        rows = ScannedPage.query.filter_by(batch_id=batch_id).order_by(ScannedPage.file_id, ScannedPage.page_index).all()
        return [(r.id, r.file_id, r.page_index, r.image_path) for r in rows]


def test_parse_file_name():
    parsed = bulk.parse_file_name('1(3)-No2-0916.pdf')
    assert parsed['grade'] == 1 and parsed['class_no'] == 3 and parsed['date'].endswith('-09-16')
    assert bulk.parse_file_name('scan.pdf') == {'grade': None, 'class_no': None, 'date': None}


def test_bulk_import_reads_groups_and_registers(setup):
    client, vision = setup
    batch = client.post('/api/bulk/batches', json={'name': '9月の演習'}).get_json()['batch']
    # 1組のファイル: No.1（片面）2人分，No.2（両面）1人分＋裏面，No.1 の自信の低い読み取り，同じ生徒の出し直し
    info = _upload(client, batch['id'], '1(1)-No1-0916.pdf',
                   ['A-front', 'B-front', 'C-front', 'C-back', 'D-front', 'E-front', 'F-front'])
    assert info['pages'] == 7 and info['class_no'] == 1 and info['grade'] == 1
    pages = _pages(client, batch['id'])
    paths = {index: path for (_, _, index, path) in pages}
    vision.set(paths[0], front(1, 1, 1, 20))
    vision.set(paths[1], front(1, 1, 1, 26))
    vision.set(paths[2], front(2, 1, 1, 6, title='グラフと2次関数'))
    # paths[3] は見出しなし（裏面）
    vision.set(paths[4], front(1, 1, 1, 9, confidence='low'))
    vision.set(paths[5], front(1, 1, 1, 30))
    vision.set(paths[6], front(1, 1, 1, 30))

    res = client.post(f'/api/bulk/batches/{batch["id"]}/read')
    assert res.status_code == 202 and vision.calls == 7
    detail = client.get(f'/api/bulk/batches/{batch["id"]}').get_json()
    assert detail['batch']['pages_read'] == 7 and detail['batch']['status'] == 'ready'
    groups = {g['front_page_id']: g for g in detail['groups']}
    ids = [pid for (pid, _, _, _) in pages]
    assert groups[ids[0]]['student_id'] == '1-1-20' and groups[ids[0]]['ok']
    assert groups[ids[2]]['page_ids'] == [ids[2], ids[3]]  # 裏面は直前の表面にまとまる
    assert groups[ids[4]]['issues'] == ['low_confidence']
    assert 'duplicate' in groups[ids[5]]['issues'] and 'duplicate' in groups[ids[6]]['issues']
    summary = {(r['print_no'], r['class_no']): r for r in detail['summary']}
    assert summary[(1, 1)]['sheets'] == 5 and summary[(1, 1)]['needs_review'] == 3

    # 確認: 自信の低いページを確認済みにし，重複の片方を対象外にする
    assert client.post(f'/api/bulk/batches/{batch["id"]}/confirm', json={'page_ids': [ids[4]]}).get_json()['confirmed'] == 1
    assert client.patch(f'/api/bulk/pages/{ids[6]}', json={'kind': 'excluded'}).status_code == 200
    detail = client.get(f'/api/bulk/batches/{batch["id"]}').get_json()
    assert all(g['ok'] for g in detail['groups'])
    assert client.get(f'/api/bulk/pages/{ids[0]}/image').status_code == 200

    key = {'answers': {'練習29': '2, 0, 1'}, 'nodes': {'練習29': ['discriminant_sign']}}
    res = client.post(f'/api/bulk/batches/{batch["id"]}/register', json={
        'prints': {'1': {'test_name': 'No.1 判別式', 'unit': 'quadratic_functions', 'answer_key': key},
                   '2': {}}})
    body = res.get_json()
    assert res.status_code == 200 and len(body['created']) == 5 and body['skipped'] == []
    with client.application.app_context():
        sheets = {s.student_id: s for s in AnswerSheet.query.all()}
        assert set(sheets) == {'1-1-20', '1-1-26', '1-1-6', '1-1-9', '1-1-30'}
        assert sheets['1-1-20'].test_name == 'No.1 判別式' and sheets['1-1-20'].student_class == '1'
        assert json.loads(sheets['1-1-20'].answer_key)['unit'] == 'quadratic_functions'
        assert sheets['1-1-20'].exam_date.isoformat().endswith('-09-16')
        assert sheets['1-1-6'].test_name == 'No.2 グラフと2次関数' and sheets['1-1-6'].answer_key is None
        with pymupdf.open(sheets['1-1-6'].file_path) as doc:
            assert doc.page_count == 2  # 表面と裏面
    # 登録済みは二重に登録しない・変更できない
    again = client.post(f'/api/bulk/batches/{batch["id"]}/register', json={'prints': {'1': {}}}).get_json()
    assert again['created'] == [] and {s['reason'] for s in again['skipped']} == {'登録済み'}
    assert client.patch(f'/api/bulk/pages/{ids[0]}', json={'student_no': 3}).status_code == 409


def test_read_failures_can_be_resumed_and_fixed(setup):
    client, vision = setup
    batch = client.post('/api/bulk/batches', json={'name': '再開'}).get_json()['batch']
    _upload(client, batch['id'], '1(2)-No4-0920.pdf', ['back-first', 'X-front', 'Y-front'])
    pages = _pages(client, batch['id'])
    ids = [p[0] for p in pages]
    vision.set(pages[1][3], front(4, 1, 3, 12))  # ファイルは2組なのに3組と読めた
    with open(pages[2][3], 'rb') as f:
        vision.fail.add(hashlib.sha1(f.read()).hexdigest())

    client.post(f'/api/bulk/batches/{batch["id"]}/read')
    detail = client.get(f'/api/bulk/batches/{batch["id"]}').get_json()
    assert detail['batch']['pages_error'] == 1
    assert detail['unresolved'] == [{'page_id': ids[2], 'issue': 'error', 'error': 'RuntimeError: quota'}]
    groups = {g['front_page_id']: g for g in detail['groups']}
    assert groups[ids[0]]['issues'] == ['orphan_back']
    assert groups[ids[1]]['issues'] == ['class_mismatch']

    vision.fail.clear()
    vision.set(pages[2][3], front(4, 1, 2, 0))  # 出席番号が読めない
    client.post(f'/api/bulk/batches/{batch["id"]}/read')
    detail = client.get(f'/api/bulk/batches/{batch["id"]}').get_json()
    groups = {g['front_page_id']: g for g in detail['groups']}
    assert detail['unresolved'] == [] and groups[ids[2]]['issues'] == ['missing_number']

    # 手で直す: 出席番号を入れる，クラスを確認済みにする，表面のない裏面は対象外にする
    assert client.patch(f'/api/bulk/pages/{ids[2]}', json={'student_no': '15'}).status_code == 200
    assert client.patch(f'/api/bulk/pages/{ids[1]}', json={'class_no': 2}).status_code == 200
    assert client.patch(f'/api/bulk/pages/{ids[0]}', json={'kind': 'excluded'}).status_code == 200
    assert client.patch(f'/api/bulk/pages/{ids[2]}', json={'student_no': 'abc'}).status_code == 400
    detail = client.get(f'/api/bulk/batches/{batch["id"]}').get_json()
    assert [g['student_id'] for g in detail['groups'] if g['ok']] == ['1-2-12', '1-2-15']

    # 登録されていないプリントは対象外，確認の残る答案は登録しない
    body = client.post(f'/api/bulk/batches/{batch["id"]}/register', json={'prints': {'9': {}}}).get_json()
    assert body['created'] == []


def test_bulk_requires_login_and_vision(client, tmp_path):  # noqa: F811
    assert client.get('/api/bulk/batches').status_code == 401
    _register_and_login(client)
    client.application.config.update(UPLOAD_FOLDER=str(tmp_path / 'u'), BULK_JOBS_INLINE=True)
    batch = client.post('/api/bulk/batches', json={'name': 'x'}).get_json()['batch']
    assert client.post('/api/bulk/batches', json={}).status_code == 400
    res = client.post(f'/api/bulk/batches/{batch["id"]}/files',
                      data={'file': (io.BytesIO(b'hello'), 'notes.txt')}, content_type='multipart/form-data')
    assert res.status_code == 400
    assert client.post(f'/api/bulk/batches/{batch["id"]}/read').status_code == 503
    assert client.delete(f'/api/bulk/batches/{batch["id"]}').status_code == 200
    assert client.get(f'/api/bulk/batches/{batch["id"]}').status_code == 404
