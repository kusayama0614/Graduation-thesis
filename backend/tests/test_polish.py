"""生成した解説の文章を整える（文体・誤字・注意の重複と長さ）テスト。"""
import json
import re

from app import db
from app.learning import polish
from app.models import GeneratedExplanation
from tests.test_api_core import client  # noqa: F401  (fixture の再利用)
from tests.test_learning import FakeLLM
from tests.test_tex_template import TEMPLATE, _setup


def to_plain_style(text):
    for polite, plain in (('しましょう。', 'しよう。'), ('ましょう。', 'よう。'), ('します。', 'する。'), ('です。', 'である。'),
                          ('なります。', 'なる。')):
        text = text.replace(polite, plain)
    return text


class PolishingLLM(FakeLLM):
    """手順を書くときは敬体・誤字・重複した注意を混ぜ，整えるときは敬体を常体に直して重複した注意を削る。"""

    def __init__(self, change_math=False):
        super().__init__(writer=self.write)
        self.template_nodes = {1: ['discriminant_setup'], 2: ['double_root']}
        self.change_math = change_math

    @staticmethod
    def write(user):
        if '今回書くステップ: 1.' in user and '判別式' in user:
            return ('判別式を計算します。$D=m^2+4m-12$ となります。下にとつな放物線を思い浮かべましょう。 [[m^2-4*1*(-m+3) = m^2+4m-12]]\n'
                    '\\begin{chui}\n係数の符号に注意する。\n\\end{chui}')
        if '今回書くステップ: 2.' in user:
            return '$(m+6)(m-2)=0$ より $m=2,\\ -6$ である。 [[(2+6)*(2-2) = 0]]'
        return '$x=-\\dfrac{m}{2}$ に代入する。 [[-2/2 = -1]]'

    def generate_json(self, *, system, user, schema, schema_name, temperature=0.0, thinking_level=None):
        if schema_name != 'polished_steps':
            return super().generate_json(system=system, user=user, schema=schema, schema_name=schema_name,
                                         temperature=temperature, thinking_level=thinking_level)
        self.calls.append((schema_name, user))
        steps = []
        for number, body in re.findall(r'### 手順(\d+)\n指摘:\n(?:- .*\n)*本文（TeX）:\n(.*?)(?=\n\n### |\Z)', user, re.S):
            body = re.sub(r'\\begin\{chui\}\n係数の符号に注意する。\n\\end\{chui\}', '', to_plain_style(body)).strip()
            if self.change_math:  # 数式は印に置き換えて渡されるので，新しい数式を書き足す形で数式を変える
                body = body.replace('⟦式1⟧', '$D=m^2+4m-13$')
            steps.append({'step': int(number), 'body': body})
        return {'steps': steps}


def test_checks_find_polite_style_duplicates_and_long_cautions():
    assert polish.fix_text('下にとつな放物線と上に とつ') == '下に凸な放物線と上に凸'
    issues = polish.style_issues('結果を確かめましょう。$x=1$ です。\\begin{chui}\n係数の符号に注意する。\n\\end{chui}',
                                 ['係数の符号に注意する。'])
    assert any('敬体' in i and '2 か所' in i for i in issues) and any('同じなので削る' in i for i in issues)
    long = '\\begin{chui}' + '一つ目である。二つ目である。三つ目である。四つ目である。' + '\\end{chui}'
    assert any('長すぎる' in i for i in polish.style_issues(long, []))
    assert polish.style_issues('$D=b^2-4ac$ を計算する。', []) == []


def test_changes_to_math_are_not_accepted():
    old = '$D=m^2+4m-12$ となります。'
    assert polish.acceptable(old, '$D=m^2+4m-12$ となる。', [])
    assert not polish.acceptable(old, '$D=m^2+4m-13$ となる。', [])  # 数式が変わった
    assert not polish.acceptable(old, 'D はこうなる。', [])  # 注意の外の数式が消えた
    assert not polish.acceptable('⟦図1⟧ です。', 'である。', [])  # 図の印が消えた


def _rows(client):
    with client.application.app_context():
        rows = GeneratedExplanation.query.filter_by(kind='individual').order_by(GeneratedExplanation.created_at).all()
        return [(r.id, r.student_id, r.content, r.reused_from) for r in rows]


def test_generation_polishes_once_and_job_polishes_existing(client):  # noqa: F811
    llm = PolishingLLM()
    exam_id = _setup(client, llm)
    client.post(f'/api/learning/exam-imports/{exam_id}/template', json={'tex': TEMPLATE})
    client.post(f'/api/learning/exams/{exam_id}/generation-jobs', json={'classes': ['1-1']})
    # 生成のときに1回だけ整える: 敬体・誤字・テンプレートと同じ注意が残らない
    contents = [c for _, _, c, _ in _rows(client)]
    assert contents and all('ます。' not in c and 'しましょう' not in c and 'とつ' not in c for c in contents)
    assert all('下に凸な放物線' in c for c in contents if '判別式を計算' in c)
    assert all(c.count('係数の符号に注意する。') <= 1 for c in contents)  # テンプレートの注意だけが残る
    assert any(kind == 'polished_steps' for kind, _ in llm.calls)

    # 以前に生成した解説（敬体のまま）を，ボタンで整える
    with client.application.app_context():
        for row in GeneratedExplanation.query.filter_by(kind='individual').all():
            trace = json.loads(row.trace)
            trace['steps'][0]['text'] = '判別式を計算します。$D=m^2+4m-12$ となります。下にとつな放物線である。'
            row.trace = json.dumps(trace, ensure_ascii=False)
        db.session.commit()
    plan = client.get(f'/api/learning/exams/{exam_id}/polish-plan?classes=1-1').get_json()['plan']
    assert plan['groups'] >= 1 and plan['llm_calls'] == plan['llm_groups'] >= 1
    calls = len(llm.calls)
    res = client.post(f'/api/learning/exams/{exam_id}/polish-jobs', json={'classes': ['1-1']})
    assert res.status_code == 202
    job = res.get_json()['job']
    assert job['status'] == 'completed' and job['generated'] == plan['llm_groups']
    assert len([k for k, _ in llm.calls[calls:] if k == 'polished_steps']) == plan['llm_groups']  # 解説1件につき1回
    with client.application.app_context():
        latest = {}
        for row in GeneratedExplanation.query.filter_by(kind='individual').order_by(GeneratedExplanation.created_at).all():
            latest[(row.problem_id, row.student_id)] = row
        steps = [json.loads(r.trace)['steps'][0] for r in latest.values()]
    assert all('ます。' not in s['text'] and '$D=m^2+4m-12$' in s['text'] and '凸' in s['text'] for s in steps)
    assert all(s['attempts'][-1]['polished'] for s in steps)
    assert client.post(f'/api/learning/exams/{exam_id}/polish-jobs', json={'classes': ['1-1']}).status_code == 400


def test_polish_keeps_original_when_math_would_change(client):  # noqa: F811
    llm = PolishingLLM(change_math=True)
    exam_id = _setup(client, llm)
    client.post(f'/api/learning/exam-imports/{exam_id}/template', json={'tex': TEMPLATE})
    client.post(f'/api/learning/exams/{exam_id}/generation-jobs', json={'student_ids': ['1-1-1']})
    contents = [c for _, _, c, _ in _rows(client)]
    # 数式を変えた直しは採用しない（誤字だけはプログラムで直る）。直せなかったステップはもう1回だけ頼む
    assert any('$D=m^2+4m-12$ となります' in c for c in contents)
    assert len([k for k, _ in llm.calls if k == 'polished_steps']) == polish.MAX_POLISH_CALLS
    assert all('m^2+4m-13' not in c and 'とつ' not in c for c in contents)


def test_literal_backslash_n_is_turned_back_into_a_newline():
    from app.learning import tex_template
    text = 'まず計算する。\\nさらに手順2で $x\\neq 0$ とし，\\newline 次へ。\\\\ \\noindent 以上。'
    cleaned = tex_template.clean_generated(text)
    assert cleaned == 'まず計算する。\nさらに手順2で $x\\neq 0$ とし，\\newline 次へ。\\\\ \\noindent 以上。'
    assert polish.fix_text('下にとつ\\nさらに') == '下に凸\nさらに'
    # PDF の文書を組み立てるときにも直す（既に保存された解説のため）
    parsed = tex_template.parse(TEMPLATE)
    section = tex_template.sections(parsed)['1(1)']
    bodies = tex_template.step_bodies(section)
    bodies[0] += '\\nさらに手順2へ進む。'
    document = tex_template.build_document(parsed, [('1-1-1', {'1(1)': tex_template.assemble(section, bodies)})])
    assert '\\nさらに' not in document and '\nさらに手順2へ進む。' in document


def test_polish_job_fixes_stored_literal_newlines_without_llm(client):  # noqa: F811
    llm = PolishingLLM()
    exam_id = _setup(client, llm)
    client.post(f'/api/learning/exam-imports/{exam_id}/template', json={'tex': TEMPLATE})
    client.post(f'/api/learning/exams/{exam_id}/generation-jobs', json={'student_ids': ['1-1-1']})
    with client.application.app_context():
        for row in GeneratedExplanation.query.filter_by(kind='individual').all():
            trace = json.loads(row.trace)
            trace['steps'][0]['text'] = trace['steps'][0]['text'] + '\\nさらに確かめる。'
            row.trace = json.dumps(trace, ensure_ascii=False)
        db.session.commit()
    plan = client.get(f'/api/learning/exams/{exam_id}/polish-plan?student_ids=1-1-1').get_json()['plan']
    assert plan['typo_only'] >= 1 and plan['llm_calls'] == 0
    calls = len(llm.calls)
    job = client.post(f'/api/learning/exams/{exam_id}/polish-jobs', json={'student_ids': ['1-1-1']}).get_json()['job']
    assert job['status'] == 'completed' and job['skipped'] >= 1 and len(llm.calls) == calls
    with client.application.app_context():
        row = (GeneratedExplanation.query.filter_by(kind='individual')
               .order_by(GeneratedExplanation.created_at.desc()).first())
        assert '\\nさらに' not in row.content and '\nさらに確かめる。' in row.content


def test_polite_endings_are_found_wherever_they_are_followed_by():
    body = ('絶対値記号をそのまま外します\n$|x-1|=x-1$\n頂点は(1, 1)になります\nグラフは下に凸である。'
            '\\begin{chui}\n2つの公式を使い分けましょう。符号には注意が必要です。\n\\end{chui}')
    issues = polish.style_issues(body, [])
    assert any('敬体' in i and '4 か所' in i for i in issues)
    assert polish.style_issues('$x$ を代入する。頂点は $(1,\\ 1)$ である。', []) == []


def test_cautions_with_the_same_point_are_duplicates_but_different_points_are_not():
    master = polish.plain_text('両辺に $-1$ をかけると不等号の向きが逆になる。')
    added = polish.plain_text('負の数をかけると不等号の向きが逆になる。')
    assert polish._similar(added, master) and not polish._same(added, master)  # 言い換え: LLM に確かめさせる
    assert polish._same('係数の符号に注意する。', '係数の符号に注意する')  # ほぼ同じ文: プログラムで削る
    context = polish.context_pairs({'problem_text': '放物線 $y=x^2$ と直線 $y=mx-1$ の共有点について答えよ。'})
    touch = polish.plain_text('直線と放物線が接するときは判別式 $D=0$ である。')
    apart = polish.plain_text('直線と放物線が共有点をもたないときは判別式 $D<0$ である。')
    assert not polish._same(apart, touch, context)  # 似ていても趣旨が違うものはプログラムでは削らない
    body = ('$-2x>4$ の両辺を $-2$ で割って $x<-2$ である。\n'
            '\\begin{chui}\n負の数をかけると不等号の向きが逆になる。\n\\end{chui}')
    assert polish.drop_duplicate_cautions([body], [master]) == [body]
    issues = polish.style_issues(body, [master])
    assert len(issues) == 1 and issues[0].startswith(polish.SOFT_MARK) and polish.hard_issues(body, [master]) == []
    same = body.replace('負の数をかけると', '両辺に $-1$ をかけると')
    assert polish.drop_duplicate_cautions([same], [master]) == ['$-2x>4$ の両辺を $-2$ で割って $x<-2$ である。']


def test_math_is_masked_and_restored_exactly():
    text, found = polish.mask_math('$x=\\dfrac{1}{2}$ になります。\\[ y=1 \\]')
    assert text == '⟦式1⟧ になります。⟦式2⟧' and found == ['$x=\\dfrac{1}{2}$', '\\[ y=1 \\]']
    assert polish.unmask_math('⟦式1⟧ になる。⟦式2⟧', found) == '$x=\\dfrac{1}{2}$ になる。\\[ y=1 \\]'
    assert polish.unmask_math('⟦式3⟧ になる。', found) is None
    assert polish.unmask_math('⟦式1⟧ と ⟦式1⟧', found) is None


class WholeStepLLM:
    """手順のほぼ全部が敬体のとき: 数式の印を残したまま常体に直す（数式そのものは見えない）。"""

    def __init__(self):
        self.users = []

    def generate_json(self, *, system, user, schema, schema_name, temperature=0.0, thinking_level=None):
        self.users.append(user)
        steps = []
        for number, body in re.findall(r'### 手順(\d+)\n指摘:\n(?:- .*\n)*本文（TeX）:\n(.*?)(?=\n\n### |\Z)', user, re.S):
            steps.append({'step': int(number), 'body': body.replace('外します', '外す').replace('になります', 'になる')})
        return {'steps': steps}


def test_polish_rewrites_whole_polite_steps_without_touching_math():
    from app.learning import tex_template
    section = tex_template.sections(tex_template.parse(TEMPLATE))['1(1)']
    bodies = tex_template.step_bodies(section)
    bodies[0] = '絶対値記号をそのまま外します。\n\\[ |x-1| = x-1 \\]\n頂点は $(1,\\ 1)$ になります。'
    previous = {'format': 'latex', 'content': '', 'steps': [{'text': b, 'attempts': []} for b in bodies]}
    llm = WholeStepLLM()
    out = polish.polish(llm, {'template': section, 'problem_text': ''}, previous)
    assert out['llm_steps'] == [0] and len(llm.users) == 1
    assert '|x-1|' not in llm.users[0]  # 数式は LLM に渡さない
    text = out['result']['steps'][0]['text']
    assert text == '絶対値記号をそのまま外す。\n\\[ |x-1| = x-1 \\]\n頂点は $(1,\\ 1)$ になる。'


class CautionJudgeLLM:
    """似ている注意を確かめさせると，言い換えなら削る。"""

    def __init__(self):
        self.users = []

    def generate_json(self, *, system, user, schema, schema_name, temperature=0.0, thinking_level=None):
        self.users.append(user)
        steps = []
        for number, body in re.findall(r'### 手順(\d+)\n指摘:\n(?:- .*\n)*本文（TeX）:\n(.*?)(?=\n\n### |\Z)', user, re.S):
            body = re.sub(r'\\begin\{chui\}\n負の数.*?\\end\{chui\}', '', body, flags=re.S).strip()
            steps.append({'step': int(number), 'body': body})
        return {'steps': steps}


def test_reworded_duplicate_caution_is_judged_once_by_the_llm():
    from app.learning import tex_template
    section = tex_template.sections(tex_template.parse(TEMPLATE))['1(1)']
    bodies = tex_template.step_bodies(section)
    bodies[0] = '$-m>2$ より $m<-2$ である。\n\\begin{chui}\n負の数をかけると，符号の向きが逆になる。\n\\end{chui}'
    previous = {'format': 'latex', 'content': '', 'steps': [{'text': b, 'attempts': []} for b in bodies]}
    problem = {'template': section, 'problem_text': ''}
    llm = CautionJudgeLLM()
    out = polish.polish(llm, problem, previous, earlier_cautions=['$-1$ をかけると，符号の向きが逆になる。'])
    assert len(llm.users) == 1 and out['llm_steps'] == [0]
    assert out['result']['steps'][0]['text'] == '$-m>2$ より $m<-2$ である。'

    # LLM が「別の注意」として残した場合も，同じ解説をもう一度確かめさせない（API の呼び出しを増やさない）
    keep = KeepLLM()
    out = polish.polish(keep, problem, previous, earlier_cautions=['$-1$ をかけると，符号の向きが逆になる。'])
    assert len(keep.users) == 1 and out['result']['steps'][0]['polish_checked']
    again = polish.polish(None, problem, out['result'], earlier_cautions=['$-1$ をかけると，符号の向きが逆になる。'],
                          dry_run=True)
    assert not again['needs_llm']


class KeepLLM(CautionJudgeLLM):
    def generate_json(self, *, system, user, schema, schema_name, temperature=0.0, thinking_level=None):
        self.users.append(user)
        return {'steps': [{'step': int(n), 'body': b} for n, b in
                          re.findall(r'### 手順(\d+)\n指摘:\n(?:- .*\n)*本文（TeX）:\n(.*?)(?=\n\n### |\Z)', user, re.S)]}
