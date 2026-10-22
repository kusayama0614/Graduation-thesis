"""生徒ごとの解答解説 PDF（A: 個別化，B: マスター）とクラスごとの一括ダウンロードのテスト。"""
import io
import zipfile

from app.learning import pdf_export
from tests.test_api_core import client  # noqa: F401  (fixture の再利用)
from tests.test_learning import FakeLLM
from tests.test_tex_template import TEMPLATE, _setup, template_writer


class FakeCompiler:
    def __init__(self):
        self.sources = []

    def __call__(self, tex):
        self.sources.append(tex)
        return b'%PDF-1.5 fake ' + str(len(self.sources)).encode()


def test_file_stem_and_evidence_rule():
    assert pdf_export.file_stem('1-1-20', 'A') == '1年1組20番_解答解説A'
    assert pdf_export.file_stem('S/01', 'B') == 'S_01_解答解説B'
    step = lambda counts: {'decision': {'node_confidences': {n: {'confidence': 0.5, 'evidence_count': c}
                                                             for n, c in counts.items()}}}
    enough = pdf_export.personalization_evidence([{'steps': [step({'a': 3}), step({'a': 4, 'b': 3})]}])
    assert enough['ratio'] == 1.0 and not enough['insufficient']
    few = pdf_export.personalization_evidence([{'steps': [step({'a': 3}), step({'a': 3, 'b': 1}), step({'c': 0})]}])
    assert few['evidenced_steps'] == 1 and few['insufficient'] and few['low_evidence_nodes'] == {'b': 1, 'c': 0}
    assert pdf_export.personalization_evidence([])['insufficient']  # 個別化した解説がない


def test_pdf_export_per_student_and_class_zip(client, tmp_path):  # noqa: F811
    llm = FakeLLM(writer=template_writer)
    llm.template_nodes = {1: ['discriminant_setup'], 2: ['double_root']}
    exam_id = _setup(client, llm)
    compiler = FakeCompiler()
    client.application.config.update(PDF_COMPILER=compiler, UPLOAD_FOLDER=str(tmp_path / 'uploads'))
    assert client.post(f'/api/learning/exams/{exam_id}/pdf-exports', json={}).status_code == 400  # テンプレートが必要
    client.post(f'/api/learning/exam-imports/{exam_id}/template', json={'tex': TEMPLATE})
    client.post(f'/api/learning/exams/{exam_id}/generation-jobs', json={'classes': ['1-1']})

    res = client.post(f'/api/learning/exams/{exam_id}/pdf-exports', json={'classes': ['1-1']})
    assert res.status_code == 202
    job = res.get_json()['job']
    assert job['status'] == 'completed' and job['total'] == 4 and job['done'] == 4, job
    names = [f['filename'] for f in job['files']]
    # 1-1-1 は根拠（出題数）の少ないノードで判断しているので ※，1-1-2 は十分
    assert names == ['1年1組1番_解答解説A※.pdf', '1年1組1番_解答解説B.pdf',
                     '1年1組2番_解答解説A.pdf', '1年1組2番_解答解説B.pdf']
    assert job['files'][0]['insufficient'] and not job['files'][2]['insufficient']

    def source(kind, seat):  # 並列にコンパイルするので順番は決まらない
        return next(t for t in compiler.sources if f'\\renewcommand{{\\kaisetsulabel}}{{解説{kind}}}' in t
                    and f'\\underline{{\\makebox[2.2em]{{{seat}}}}}番' in t)

    a_weak, b_weak, a_strong = source('A', 1), source('B', 1), source('A', 2)
    assert '\\renewcommand{\\kaisetsulabel}{解説A}' in a_weak and '\\renewcommand{\\kaisetsulabel}{解説B}' in b_weak
    assert '詳しく：' in a_weak and '簡潔に：' in a_strong and '詳しく：' not in b_weak
    assert '$D=m^2-4(-m+3)=m^2+4m-12$' in b_weak  # B はテンプレート（マスター解説）のまま
    assert '\\underline{\\makebox[2.2em]{1}}組\\underline{\\makebox[2.2em]{1}}番' in b_weak
    for source in compiler.sources:  # どちらが個別化したものか，表題や本文の決まった部分からは分からない
        assert '個別' not in source and 'マスター' not in source

    res = client.get(f'/api/learning/pdf-exports/{job["id"]}/files/2')
    assert res.status_code == 200 and res.data.startswith(b'%PDF')
    assert "filename*=UTF-8''1%E5%B9%B4" in res.headers['Content-Disposition']
    res = client.get(f'/api/learning/pdf-exports/{job["id"]}/zip')
    assert res.status_code == 200 and '1%E5%B9%B41%E7%B5%84' in res.headers['Content-Disposition']  # 1年1組
    with zipfile.ZipFile(io.BytesIO(res.data)) as archive:
        assert sorted(archive.namelist()) == sorted(names)
    with zipfile.ZipFile(io.BytesIO(client.get(f'/api/learning/pdf-exports/{job["id"]}/zip?kind=A').data)) as archive:
        assert sorted(archive.namelist()) == ['1年1組1番_解答解説A※.pdf', '1年1組2番_解答解説A.pdf']

    # 同じ TeX は作り直さない
    client.post(f'/api/learning/exams/{exam_id}/pdf-exports', json={'student_ids': ['1-1-2']})
    assert len(compiler.sources) == 4

    booklet = client.get(f'/api/learning/exams/{exam_id}/booklet?classes=1-1').get_json()
    assert [s['insufficient'] for s in booklet['students']] == [True, False]


def test_pdf_export_reports_compile_errors(client, tmp_path):  # noqa: F811
    exam_id = _setup(client, FakeLLM(writer=template_writer))

    def broken(tex):
        raise pdf_export.PdfError('TeX のコンパイルに失敗しました: ! Undefined control sequence.')

    client.application.config.update(PDF_COMPILER=broken, UPLOAD_FOLDER=str(tmp_path / 'uploads'))
    client.post(f'/api/learning/exam-imports/{exam_id}/template', json={'tex': TEMPLATE})
    job = client.post(f'/api/learning/exams/{exam_id}/pdf-exports', json={'student_ids': ['1-1-1']}).get_json()['job']
    assert job['status'] == 'failed' and 'Undefined control sequence' in job['message']
    assert any(e.get('skipped') and 'B（マスター解説）だけを作りました' in e['error'] for e in job['errors'])


def test_tex_error_summary_names_the_missing_font():
    log = ('This is XeTeX\n! Package fontspec Error: \n(fontspec)                The font "Noto Sans CJK JP Black" cannot be\n'
           '(fontspec)                found.\n\nFor immediate help type H <return>.\nl.17 \\newfontfamily\\titlelatin{Noto Sans CJK JP Black}\n')
    summary = pdf_export.tex_error_summary(log)
    assert 'The font "Noto Sans CJK JP Black" cannot be found.' in summary and 'Mac に入っていません' in summary
    assert pdf_export.tex_error_summary('! Undefined control sequence.\nl.3 \\foo\n') == '! Undefined control sequence.'


def test_downloads_work_with_relative_upload_folder(client, tmp_path, monkeypatch):  # noqa: F811
    # 既定の UPLOAD_FOLDER=uploads（相対パス）でも，1件ずつ・ZIP のどちらもダウンロードできる
    monkeypatch.chdir(tmp_path)
    llm = FakeLLM(writer=template_writer)
    llm.template_nodes = {1: ['discriminant_setup'], 2: ['double_root']}
    exam_id = _setup(client, llm)
    client.application.config.update(PDF_COMPILER=FakeCompiler(), UPLOAD_FOLDER='uploads')
    client.post(f'/api/learning/exam-imports/{exam_id}/template', json={'tex': TEMPLATE})
    client.post(f'/api/learning/exams/{exam_id}/generation-jobs', json={'classes': ['1-1']})
    job = client.post(f'/api/learning/exams/{exam_id}/pdf-exports', json={'classes': ['1-1']}).get_json()['job']
    assert client.get(f'/api/learning/pdf-exports/{job["id"]}/files/0').status_code == 200
    res = client.get(f'/api/learning/pdf-exports/{job["id"]}/zip')
    assert res.status_code == 200
    with zipfile.ZipFile(io.BytesIO(res.data)) as archive:
        assert len(archive.namelist()) == 4


def test_students_without_explanations_get_only_the_master(client, tmp_path):  # noqa: F811
    from app import db
    from app.models import GeneratedExplanation

    llm = FakeLLM(writer=template_writer)
    llm.template_nodes = {1: ['discriminant_setup'], 2: ['double_root']}
    exam_id = _setup(client, llm)
    compiler = FakeCompiler()
    client.application.config.update(PDF_COMPILER=compiler, UPLOAD_FOLDER=str(tmp_path / 'uploads'))
    client.post(f'/api/learning/exam-imports/{exam_id}/template', json={'tex': TEMPLATE})
    client.post(f'/api/learning/exams/{exam_id}/generation-jobs', json={'classes': ['1-1']})
    with client.application.app_context():  # 1-1-2 の解説がない（生成が途中で止まった）
        GeneratedExplanation.query.filter_by(student_id='1-1-2').delete()
        db.session.commit()

    job = client.post(f'/api/learning/exams/{exam_id}/pdf-exports', json={'classes': ['1-1']}).get_json()['job']
    assert job['status'] == 'completed' and job['total'] == 3 and job['done'] == 3
    by_file = {(f['student_id'], f['kind']): f for f in job['files']}
    skipped = by_file[('1-1-2', 'A')]
    assert skipped['skipped'] and not skipped['ready'] and 'A（個別化した解説）は作らず' in skipped['reason']
    assert by_file[('1-1-2', 'B')]['ready'] and by_file[('1-1-1', 'A')]['ready']

    res = client.get(f"/api/learning/pdf-exports/{job['id']}/zip")
    names = zipfile.ZipFile(io.BytesIO(res.data)).namelist()
    assert '1年1組2番_解答解説B.pdf' in names and '1年1組2番_解答解説A.pdf' not in names
    assert pdf_export.SKIPPED_NOTE_NAME in names
    note = zipfile.ZipFile(io.BytesIO(res.data)).read(pdf_export.SKIPPED_NOTE_NAME).decode('utf-8')
    assert '1年1組2番（1-1-2）' in note
    only_b = client.get(f"/api/learning/pdf-exports/{job['id']}/zip?kind=B")
    assert pdf_export.SKIPPED_NOTE_NAME not in zipfile.ZipFile(io.BytesIO(only_b.data)).namelist()
