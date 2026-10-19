"""マスター解説の TeX をテンプレートにして，体裁はそのまま内容だけを個別化するテスト。"""
import json
import re

import pytest

from app import db
from app.learning import tex_template
from app.models import ExamImport, GeneratedExplanation, TestProblem
from tests.test_api_core import client  # noqa: F401  (fixture の再利用)
from tests.test_exam_generation import STRONG, WEAK, _set_profile
from tests.test_learning import FakeLLM, _login, _teacher_db_id

TEMPLATE = r"""\documentclass{bxjsarticle}
\newcommand{\kaisetsulabel}{解説A}
\newcommand{\daimon}[2]{#1}
\newcommand{\shomon}[1]{#1}
\newcommand{\tejun}[1]{#1}
\newcommand{\kangae}[1]{#1}
\begin{document}
\twocolumn[{%
\noindent 数学Ⅰ『解答解説』\fbox{\kaisetsulabel}\par
\noindent\hfill 1年\underline{\hspace{2.2em}}組\underline{\hspace{2.2em}}番\par
}]

%======================================================================
\daimon{1}{重解}
\kangae{大問1に共通の考え方。}
\shomon{(1)\ \ $x^2+mx-m+3=0$ が重解をもつ $m$}
\begin{kotae}$m=2,\ -6$\end{kotae}
\resettejun
\tejun{判別式を立てる}
$D=m^2-4(-m+3)=m^2+4m-12$
\tejun{$D=0$ を解く}
$(m+6)(m-2)=0$ より $m=2,\ -6$
\begin{center}
\begin{tikzpicture}\draw (0,0)--(1,1);\end{tikzpicture}
\end{center}
\begin{chui}
係数の符号に注意する。
\end{chui}

\shomon{(2)\ \ そのときの重解}
\begin{kotae}$x=-1,\ 3$\end{kotae}
\resettejun
\tejun{重解を求める}
$x=-\dfrac{m}{2}$ に代入する。

%======================================================================
\daimon{2}{グラフ}
\begin{kotae}グラフは下図\end{kotae}
\begin{center}\begin{tikzpicture}\draw (0,0) circle (1);\end{tikzpicture}\end{center}
\kangae{絶対値は中身の符号で場合分け。}

\end{document}
"""

TEXT = '2次方程式 x^2 + mx - m + 3 = 0 が重解をもつような定数 m の値と，そのときの重解を求めよ。'


def test_parse_and_assemble_round_trip():
    parsed = tex_template.parse(TEMPLATE)
    sections = tex_template.sections(parsed)
    assert list(sections) == ['1(1)', '1(2)', '2']
    first = sections['1(1)']
    assert '大問1に共通の考え方' in first['tex'] and '大問1に共通の考え方' not in sections['1(2)']['tex']
    steps = tex_template.template_steps(first)
    assert [s['title'] for s in steps] == ['判別式を立てる', '$D=0$ を解く']
    assert steps[0]['result'] == 'D=m^2-4(-m+3)=m^2+4m-12' and '⟦図1⟧' in tex_template.step_bodies(first)[1]
    assert tex_template.template_steps(sections['2']) == []  # 手順のない大問
    # 本文を変えずに組み立てるとテンプレートに戻る
    for section in sections.values():
        assert tex_template.assemble(section, tex_template.step_bodies(section)) == section['tex']
    # 図の印が落ちても図は残す
    rebuilt = tex_template.assemble(first, ['書き直した手順1', '書き直した手順2（図の印なし）'])
    assert 'tikzpicture' in rebuilt and '\\begin{kotae}$m=2,\\ -6$\\end{kotae}' in rebuilt
    assert '\\begin{chui}\n係数の符号に注意する。' in rebuilt
    document = tex_template.build_document(parsed, [('1-1-20', {}), ('1-1-3', {'1(2)': 'X'})])
    assert document.count('\\twocolumn[') == 2 and '\\clearpage' in document
    assert '\\underline{\\makebox[2.2em]{1}}組\\underline{\\makebox[2.2em]{20}}番' in document
    assert '\\providecommand{\\fukushu}' in document


def test_parse_rejects_non_template():
    with pytest.raises(tex_template.TemplateError):
        tex_template.parse('just text')
    with pytest.raises(tex_template.TemplateError):
        tex_template.parse('\\begin{document}no daimon\\end{document}')


def template_writer(user):
    """マスター解説の手順の本文を受け取り，生徒向けに書き直した本文（検算できる計算つき）を返す。"""
    body = user.split('（TeX。これを土台にする）:\n', 1)[1].split('\n\n含める要素', 1)[0]
    extra = '詳しく：' if '途中の式変形を補い' in user else '簡潔に：'
    if '今回書くステップ: 1.' in user and '判別式' in user:
        return f'{extra}{body} [[m^2-4*1*(-m+3) = m^2+4m-12]]'
    if '今回書くステップ: 2.' in user:
        return f'{extra}$(m+6)(m-2)=0$ [[(2+6)*(2-2) = 0]]'  # 図の印を落としても図は残る
    return f'{extra}{body} [[-2/2 = -1]]'


def _setup(client, llm):
    _login(client, llm)
    _set_profile(client, '1-1-1', WEAK)
    _set_profile(client, '1-1-2', STRONG)
    with client.application.app_context():
        teacher = _teacher_db_id(client)
        exam = ExamImport(teacher_id=teacher, name='中間試験', status='saved')
        db.session.add(exam)
        db.session.flush()
        for number, title in [('1(1)', '重解の条件'), ('1(2)', '重解'), ('2', 'グラフ'), ('3', 'テンプレートなし')]:
            db.session.add(TestProblem(teacher_id=teacher, exam_id=exam.id, number=number, unit='quadratic_functions',
                                       title=f'大問{number} {title}', problem_text=TEXT, correct_answer='-1, 3',
                                       steps='[]', chain1_status='error'))
        db.session.commit()
        return exam.id


def _rows(client, kind):
    with client.application.app_context():
        return {(r.problem_id, r.student_id): (r.content, json.loads(r.trace))
                for r in GeneratedExplanation.query.filter_by(kind=kind).all()}


def test_template_upload_generation_and_tex_booklet(client):  # noqa: F811
    llm = FakeLLM(writer=template_writer)
    llm.template_nodes = {1: ['discriminant_setup'], 2: ['double_root']}
    exam_id = _setup(client, llm)

    res = client.post(f'/api/learning/exam-imports/{exam_id}/template', json={'tex': TEMPLATE, 'name': 'master.tex'})
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert [m['number'] for m in body['matched']] == ['1(1)', '1(2)', '2']
    assert body['problems_without_template'] == ['3'] and body['unused_sections'] == []
    assert body['exam']['template']['problems'] == ['1(1)', '1(2)', '2']
    with client.application.app_context():
        p11 = TestProblem.query.filter_by(exam_id=exam_id, number='1(1)').first()
        steps = json.loads(p11.steps)
        assert p11.steps_source == 'template' and p11.chain1_status == 'done'
        assert [s['nodes'] for s in steps] == [['discriminant_setup'], ['double_root']]  # ノードだけ LLM が付ける
        ids = {p.number: p.id for p in TestProblem.query.filter_by(exam_id=exam_id).all()}
    tagging = [u for kind, u in llm.calls if kind == 'template_step_nodes']
    assert len(tagging) == 2 and '$D=m^2-4(-m+3)=m^2+4m-12$' in tagging[0]

    calls_before = len(llm.calls)
    res = client.post(f'/api/learning/exams/{exam_id}/generation-jobs',
                      json={'classes': ['1-1'], 'problem_ids': [ids['1(1)'], ids['2']], 'include_master': True})
    job = res.get_json()['job']
    assert job['status'] == 'completed', job

    individual = _rows(client, 'individual')
    weak_content, weak_trace = individual[(ids['1(1)'], '1-1-1')]
    strong_content, _ = individual[(ids['1(1)'], '1-1-2')]
    assert weak_trace['format'] == 'latex'
    # 体裁（答・考え方・手順の見出し・図・注意）はテンプレートのまま，手順の本文だけが生徒ごとに違う
    for content in (weak_content, strong_content):
        assert content.startswith('\n\\kangae{大問1に共通の考え方。}')
        assert '\\begin{kotae}$m=2,\\ -6$\\end{kotae}' in content
        assert '\\tejun{判別式を立てる}' in content and '\\tejun{$D=0$ を解く}' in content
        assert 'tikzpicture' in content and '\\begin{chui}\n係数の符号に注意する。' in content
    assert '詳しく：' in weak_content and '簡潔に：' in strong_content
    prompts = [u for kind, u in llm.calls[calls_before:] if kind == 'text']
    assert all('マスター解説のこの手順の本文' in u for u in prompts)
    assert any('\\fukushu' in u for u in prompts)  # 定着度が低い生徒には復習の指示

    # マスター解説はテンプレートそのもの，手順のない大問2は全員共通（どちらも LLM を呼ばない）
    master = _rows(client, 'master')
    assert master[(ids['1(1)'], None)][0] == tex_template.sections(tex_template.parse(TEMPLATE))['1(1)']['tex']
    common = _rows(client, 'common')
    assert 'circle (1)' in common[(ids['2'], None)][0]
    assert len([u for kind, u in llm.calls[calls_before:] if kind == 'text']) == 4  # 2人 × 2手順のみ

    summary = client.get(f'/api/learning/exams/{exam_id}/explanations-summary').get_json()
    assert summary['problems'][0]['answer_check']['status'] == 'template'

    res = client.get(f'/api/learning/exams/{exam_id}/booklet.tex?classes=1-1')
    assert res.status_code == 200 and 'attachment' in res.headers['Content-Disposition']
    document = res.get_data(as_text=True)
    assert document.startswith('\\documentclass{bxjsarticle}') and document.rstrip().endswith('\\end{document}')
    assert document.count('\\twocolumn[') == 2 and '詳しく：' in document and '簡潔に：' in document
    assert '\\underline{\\makebox[2.2em]{1}}組\\underline{\\makebox[2.2em]{2}}番' in document
    assert '\\daimon{2}{グラフ}' in document and 'circle (1)' in document
    assert re.search(r'\\shomon\{\(2\)', document)  # 生成していない小問はテンプレートのまま

    booklet = client.get(f'/api/learning/exams/{exam_id}/booklet?student_ids=1-1-1').get_json()
    assert booklet['students'][0]['items'][0]['format'] == 'latex'

    # テンプレートを外すと，問題文からの分解に戻る
    res = client.delete(f'/api/learning/exam-imports/{exam_id}/template')
    assert res.status_code == 200 and res.get_json()['exam']['template'] is None
    with client.application.app_context():
        assert TestProblem.query.filter_by(exam_id=exam_id, number='1(1)').first().template is None


def test_template_upload_errors(client):  # noqa: F811
    exam_id = _setup(client, FakeLLM())
    assert client.post(f'/api/learning/exam-imports/{exam_id}/template', json={'tex': 'abc'}).status_code == 400
    assert client.post(f'/api/learning/exam-imports/{exam_id}/template', json={}).status_code == 400
    assert client.get(f'/api/learning/exams/{exam_id}/booklet.tex').status_code == 400


def test_check_latex_finds_broken_tex():
    assert tex_template.check_latex('$x=1$ \\[ y \\] \\begin{align*}a&=b\\\\ c&=d\\end{align*} 50\\% と \\{ ⟦図1⟧') == []
    assert tex_template.check_latex('\\begin{align*} x=1') == ['\\begin{align*} が閉じていません']
    assert tex_template.check_latex('\\begin{itemize}\\item a\\end{enumerate}')[0].startswith('\\end{enumerate}')
    assert tex_template.check_latex('$x=1') == ['$ の数が奇数です（数式が閉じていません）']
    assert tex_template.check_latex('\\frac{1}{2') == ['{ と } の数が合っていません']


def test_broken_latex_is_sent_back_and_rewritten(client):  # noqa: F811
    state = {'first': True}

    def writer(user):
        if '今回書くステップ: 1.' in user and '判別式' in user and state['first']:
            state['first'] = False
            return '\\begin{align*} D=m^2+4m-12 [[m^2-4*1*(-m+3) = m^2+4m-12]]'  # 環境が閉じていない
        if '今回書くステップ: 1.' in user and '判別式' in user:
            assert 'TeX の書き方に誤りがあります' in user  # 差し戻しの理由が渡る
        return template_writer(user)

    llm = FakeLLM(writer=writer)
    llm.template_nodes = {1: ['discriminant_setup'], 2: ['double_root']}
    exam_id = _setup(client, llm)
    client.post(f'/api/learning/exam-imports/{exam_id}/template', json={'tex': TEMPLATE})
    with client.application.app_context():
        problem_id = TestProblem.query.filter_by(exam_id=exam_id, number='1(1)').first().id
    job = client.post(f'/api/learning/exams/{exam_id}/generation-jobs',
                      json={'student_ids': ['1-1-1'], 'problem_ids': [problem_id]}).get_json()['job']
    assert job['status'] == 'completed'
    content, trace = next(iter(_rows(client, 'individual').values()))
    step1 = trace['steps'][0]
    assert step1['attempt_count'] == 2 and step1['attempts'][0]['latex_errors'] == ['\\begin{align*} が閉じていません']
    assert tex_template.check_latex(content) == []
