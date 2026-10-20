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
    for polite, plain in (('しましょう。', 'しよう。'), ('します。', 'する。'), ('です。', 'である。'), ('なります。', 'なる。')):
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
            if self.change_math:
                body = body.replace('m^2+4m-12', 'm^2+4m-13')
            steps.append({'step': int(number), 'body': body})
        return {'steps': steps}


def test_checks_find_polite_style_duplicates_and_long_cautions():
    assert polish.fix_text('下にとつな放物線と上に とつ') == '下に凸な放物線と上に凸'
    issues = polish.style_issues('結果を確かめましょう。$x=1$ です。\\begin{chui}\n係数の符号に注意する。\n\\end{chui}',
                                 ['係数の符号に注意する。'])
    assert any('敬体' in i and '2 か所' in i for i in issues) and any('同じ趣旨' in i for i in issues)
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
    # 数式を変えた直しは採用しない（誤字だけはプログラムで直る）
    assert any('$D=m^2+4m-12$ となります' in c for c in contents)
    assert all('m^2+4m-13' not in c and 'とつ' not in c for c in contents)
