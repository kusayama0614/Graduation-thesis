"""定期試験の PDF（問題用紙・模範解答）からの問題の取り込み

問題用紙と模範解答の全ページを Gemini に画像として渡し，小問ごとに問題文・正しい答え・配点などを
読み取らせる（数式を含む PDF は文字の層が崩れていることが多いため，画像として読む）。読み取った候補は
教員が確かめて直してから定期テストの問題（TestProblem）として登録し，そのあと Chain 1（解答ステップへの
分解）を裏で順に実行する。
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, List, Optional

from app.learning import graph

EXAM_DPI = 170  # 小さな数式も読めるよう，演習プリントより少し細かく描画する

READ_SYSTEM = (
    'あなたは高校の数学の定期試験の問題用紙と模範解答を読み取り，問題ごとのデータに整理する係です。'
    '問題文と答えは画像に書かれているとおりに写し，自分で解き直したり書き換えたりしないでください。'
)


def read_prompt(problem_pages: int, answer_pages: int) -> str:
    units = '\n'.join(f'- {uid}: {graph.get_unit(uid)["label"]}（{graph.get_unit(uid)["scope"]}）' for uid in graph.unit_ids())
    answer_range = (f'画像 {problem_pages + 1}〜{problem_pages + answer_pages} は模範解答（解答用紙に正解を書き込んだもの）です。'
                    if answer_pages else '模範解答の画像はありません（正しい答えは空にしてください）。')
    return (
        f'画像 1〜{problem_pages} は定期試験の問題用紙です。{answer_range}\n'
        '問題用紙の問題を，小問（(1)(2)…）ごとに1件として書き出してください。小問がない大問は大問で1件。'
        '会話文の空欄［ア］〜［カ］のように一続きの穴埋めは，大問で1件にまとめてください。\n'
        '各件について:\n'
        '- number: 問題番号（例: 1(1)，3(2)，12，13）。\n'
        '- title: 内容が分かる短い題（例: 2次方程式を解く，接線の条件と接点）。\n'
        f'- unit: 次のどれか。\n{units}\n'
        '- problem_text: 問題文を省略せずに。大問の共通の文（「次の2次方程式を解け」など）も含めて，その小問だけで解ける文にする。'
        '数式は $...$ の LaTeX で書く（例: $2x^2+5x-3=0$）。図や表があれば，数値や位置関係を文章で書き込む（例: 三角比の表の値，図の辺の長さと角）。\n'
        '- correct_answer: 模範解答の答えを，数式処理で照合できる形で（√ は sqrt()，分数は a/b，≦・≧ はそのまま。'
        '例: x=-3, 1/2 ／ m<=2 ／ k=1/2, (1/2, 3) ／ すべての実数 ／ 解なし）。照合しにくい答え（グラフ・証明・文章）は空にする。\n'
        '- answer_display: 模範解答の答えを，書かれている見た目のとおりに（記述式の答案なら要点も）。\n'
        '- answer_format: 答えのみなら answer_only，記述式なら written（問題用紙の注意書きに従う）。\n'
        '- points: 配点（模範解答に書かれていれば。なければ 0）。\n'
        '- figure_note: 図やグラフを描く問題・図を使う問題なら，その内容の補足（なければ空）。\n'
        '- confidence: 読み取りに自信があれば high，判読しにくい箇所があれば low。\n'
        '問題用紙にある問題は，番号の順にすべて出力してください。'
    )


READ_SCHEMA = {
    'type': 'object',
    'properties': {
        'exam_title': {'type': 'string'},
        'questions': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'number': {'type': 'string'},
                    'title': {'type': 'string'},
                    'unit': {'type': 'string', 'enum': None},  # 単元の id（読み込み時に埋める）
                    'problem_text': {'type': 'string'},
                    'correct_answer': {'type': 'string'},
                    'answer_display': {'type': 'string'},
                    'answer_format': {'type': 'string', 'enum': ['answer_only', 'written']},
                    'points': {'type': 'integer'},
                    'figure_note': {'type': 'string'},
                    'confidence': {'type': 'string', 'enum': ['high', 'low']},
                },
                'required': ['number', 'title', 'unit', 'problem_text', 'correct_answer', 'answer_display',
                             'answer_format', 'points', 'figure_note', 'confidence'],
            },
        },
    },
    'required': ['exam_title', 'questions'],
}


def read_schema() -> Dict[str, Any]:
    schema = json.loads(json.dumps(READ_SCHEMA))
    schema['properties']['questions']['items']['properties']['unit']['enum'] = graph.unit_ids()
    return schema


def normalize_candidates(out: Dict[str, Any]) -> List[Dict[str, Any]]:
    units = set(graph.unit_ids())
    candidates = []
    for index, q in enumerate(out.get('questions') or []):
        if not isinstance(q, dict):
            continue
        try:
            points = max(0, int(q.get('points') or 0))
        except (TypeError, ValueError):
            points = 0
        unit = q.get('unit') if q.get('unit') in units else graph.unit_ids()[0]
        candidates.append({
            'key': str(index),
            'number': str(q.get('number') or '').strip()[:30],
            'title': str(q.get('title') or '').strip()[:150],
            'unit': unit,
            'problem_text': str(q.get('problem_text') or '').strip(),
            'correct_answer': str(q.get('correct_answer') or '').strip(),
            'answer_display': str(q.get('answer_display') or '').strip(),
            'answer_format': q.get('answer_format') if q.get('answer_format') in ('answer_only', 'written') else 'answer_only',
            'points': points or None,
            'figure_note': str(q.get('figure_note') or '').strip(),
            'confidence': q.get('confidence') if q.get('confidence') in ('high', 'low') else 'low',
        })
    return candidates


# ---------------------------------------------------------------- 裏処理（読み取り・Chain 1）

_running: set = set()
_lock = threading.Lock()


def _start(app, key: str, target, *args) -> bool:
    with _lock:
        if key in _running:
            return False
        _running.add(key)

    def run():
        try:
            target(app, *args)
        finally:
            with _lock:
                _running.discard(key)

    if app.config.get('LEARNING_JOBS_INLINE'):
        run()
    else:
        threading.Thread(target=run, daemon=True).start()
    return True


def is_running(key: str) -> bool:
    with _lock:
        return key in _running


def start_reading(app, import_id: str) -> bool:
    return _start(app, f'read:{import_id}', _read, import_id)


def _read(app, import_id: str) -> None:
    from app import db
    from app.learning.llm import get_vision_client
    from app.models import ExamImport

    with app.app_context():
        exam = db.session.get(ExamImport, import_id)
        if exam is None:
            return
        try:
            client = get_vision_client(required=True)
            pages = json.loads(exam.pages or '[]')
            images = []
            for page in pages:
                with open(page['path'], 'rb') as f:
                    images.append((f.read(), 'image/jpeg'))
            problem_pages = sum(1 for p in pages if p['kind'] == 'problem')
            out = client.read_images_json(system=READ_SYSTEM, user=read_prompt(problem_pages, len(pages) - problem_pages),
                                          images=images, schema=read_schema(), schema_name='exam_questions')
            exam.candidates = json.dumps(normalize_candidates(out), ensure_ascii=False)
            if out.get('exam_title') and exam.name.endswith('.pdf'):
                exam.name = str(out['exam_title'])[:200]
            exam.model_name = getattr(client, 'model_name', None)
            exam.status = 'ready'
            exam.error = None
        except Exception as exc:  # 失敗しても読み取り直せるようにする
            exam.status = 'error'
            exam.error = f'{exc.__class__.__name__}: {str(exc)[:300]}'
        db.session.commit()


def start_chain1(app, exam_id: str) -> bool:
    return _start(app, f'chain1:{exam_id}', _chain1, exam_id)


def _chain1(app, exam_id: str) -> None:
    """取り込んだ問題を順に Chain 1 で解答ステップに分解する（1問ずつ確定する）。"""
    from app import db
    from app.learning import explanation as chain
    from app.learning.llm import get_llm_client
    from app.models import TestProblem

    with app.app_context():
        llm = get_llm_client(required=False)
        ids = [p.id for p in TestProblem.query.filter_by(exam_id=exam_id, chain1_status='pending')
               .order_by(TestProblem.created_at).all()]
        for problem_id in ids:
            problem = db.session.get(TestProblem, problem_id)
            if problem is None:
                continue
            try:
                if llm is None:
                    raise RuntimeError('GEMINI_API_KEY が設定されていません')
                steps = chain.decompose_problem(llm, problem.unit, problem.problem_text)
                problem.steps = json.dumps(steps, ensure_ascii=False)
                problem.steps_source = 'chain1'
                problem.chain1_status = 'done'
                problem.chain1_error = None
            except Exception as exc:  # 1問の失敗で止めない（画面から分解し直せる）
                problem.chain1_status = 'error'
                problem.chain1_error = f'{exc.__class__.__name__}: {str(exc)[:300]}'
            db.session.commit()


def exam_dir(upload_root: str, import_id: str) -> str:
    return os.path.join(upload_root, 'exams', import_id)


def problem_title(candidate: Dict[str, Any], exam_name: Optional[str] = None) -> str:
    number = candidate.get('number') or ''
    title = candidate.get('title') or ''
    return (f'大問{number} {title}' if number else title).strip()[:200]
