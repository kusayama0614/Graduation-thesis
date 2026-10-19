# ==================== 個別最適化解説生成ルート ====================
"""
学習者プロファイル（フェーズA）と個別化した解説の生成（フェーズB）の API（卒業論文 3章）
"""
import json
import os
import shutil
import time
from datetime import date, datetime

from sqlalchemy.exc import OperationalError
from flask import Blueprint, current_app, request, send_file

from app import db
from app.learning import exam_import
from app.learning import explanation as chain
from app.learning import graph
from app.learning.jobs import job_to_dict, problem_to_chain_input, start_job
from app.learning.llm import LLMUnavailable, get_embedding_client, get_llm_client, get_vision_client
from app.learning.profile import (
    ExerciseError, apply_exercise, key_answers, key_nodes, profile_to_dict, rebuild_profile,
)
from app.models import (
    AnswerSheet, ExamImport, ExerciseRecord, ExplanationEmbedding, GeneratedExplanation, GenerationJob,
    LearnerProfile, TestProblem,
)
from app.routes.analysis import _resolve_teacher_ids
from app.routes.auth import login_required
from app.utils.api_response import make_error_response, make_success_response
from app.utils.bulk_import import split_into_pages
from app.utils.firebase_service import get_firebase_service

learning_bp = Blueprint('learning', __name__)

MAX_STUDENTS_PER_JOB = 200


def _teacher_db_id():
    return _resolve_teacher_ids()[1]


def _parse_json(raw, default):
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default
    return default


def _as_date(value):
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.strptime(value[:10], '%Y-%m-%d').date()
        except ValueError:
            return None
    return None


def _load_sheet(data_id):
    """解答用紙を (dict, error_response) で返す。Firebase 有効時はそちらから読む。"""
    teacher_id, teacher_db_id = _resolve_teacher_ids()
    firebase_service = get_firebase_service()
    if firebase_service.enabled:
        sheet = firebase_service.get_answer_sheet(data_id)
        if not sheet or sheet.get('teacher_id') != teacher_id:
            return None, make_error_response('ANALYSIS_NOT_FOUND', 'Data not found', 404)
        return {
            'id': data_id,
            'student_id': sheet.get('student_id'),
            'test_name': sheet.get('test_name'),
            'exam_date': _as_date(sheet.get('exam_date')),
            'answer_key': _parse_json(sheet.get('answer_key'), {}),
            'question_results': _parse_json(sheet.get('question_results'), []),
            'ocr_text': sheet.get('ocr_text') or '',
        }, None
    sheet = AnswerSheet.query.filter_by(id=data_id, teacher_id=teacher_db_id).first() if teacher_db_id else None
    if not sheet:
        return None, make_error_response('ANALYSIS_NOT_FOUND', 'Data not found', 404)
    return {
        'id': sheet.id,
        'student_id': sheet.student_id,
        'test_name': sheet.test_name,
        'exam_date': sheet.exam_date,
        'answer_key': _parse_json(sheet.answer_key, {}),
        'question_results': _parse_json(sheet.question_results, []),
        'ocr_text': sheet.ocr_text or '',
    }, None


# ==================== 前提知識依存グラフ ====================

@learning_bp.route('/graph', methods=['GET'])
@login_required
def get_graph():
    data = graph.load_graph()
    return make_success_response({
        'graph': data,
        'decision_rules': {
            'alpha': 0.3,
            'initial_confidence': 0.5,
            'elements': [{'element': e, 'label': chain.ELEMENT_LABELS[e], 'condition': cond}
                         for e, cond in chain.ELEMENT_RULES],
            'max_attempts': chain.MAX_ATTEMPTS,
        },
    })


# ==================== フェーズA: 演習 → 学習者プロファイル ====================

@learning_bp.route('/answer-data/<data_id>/node-tags', methods=['GET'])
@login_required
def get_node_tags(data_id):
    """解答用紙の小問の一覧と，付与済みの単元・前提知識ノード，プロファイルへの反映状況を返す。"""
    sheet, error = _load_sheet(data_id)
    if error:
        return error
    answer_key = sheet['answer_key'] if isinstance(sheet['answer_key'], dict) else {}
    answers = key_answers(answer_key)
    nodes = key_nodes(answer_key)
    questions = list(answers.keys())
    for result in sheet['question_results']:
        q = str(result.get('question'))
        if q not in questions:
            questions.append(q)
    for q in nodes:
        if q not in questions:
            questions.append(q)
    questions.sort(key=lambda q: (0, int(q)) if q.isdigit() else (1, q))
    records = ExerciseRecord.query.filter_by(teacher_id=_teacher_db_id(), answer_sheet_id=data_id).count()
    return make_success_response({
        'data_id': data_id,
        'student_id': sheet['student_id'],
        'test_name': sheet['test_name'],
        'unit': answer_key.get('unit'),
        'questions': [{'question': q, 'expected': answers.get(q), 'nodes': nodes.get(q, [])} for q in questions],
        'has_results': bool(sheet['question_results']) or bool(answers and sheet['ocr_text']),
        'applied': records > 0,
        'record_count': records,
    })


@learning_bp.route('/applied-sheets', methods=['GET'])
@login_required
def applied_sheets():
    """プロファイルに反映済みの解答用紙ID の一覧（演習の反映画面の表示用）。"""
    rows = (db.session.query(ExerciseRecord.answer_sheet_id, db.func.count(ExerciseRecord.id))
            .filter_by(teacher_id=_teacher_db_id()).group_by(ExerciseRecord.answer_sheet_id).all())
    return make_success_response({'sheets': {sheet_id: count for sheet_id, count in rows}})


def _normalize_node_tags(unit, nodes):
    """(正規化した {問番号: [ノード]}, エラー応答) を返す。"""
    if unit not in graph.unit_ids():
        return None, make_error_response('LEARNING_INVALID_UNIT', f'unit は {", ".join(graph.unit_ids())} のいずれかです', 400)
    if not isinstance(nodes, dict):
        return None, make_error_response('LEARNING_INVALID_NODES', 'nodes は {問番号: [ノード名]} の形式です', 400)
    normalized = {}
    for question, ids in nodes.items():
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            return None, make_error_response('LEARNING_INVALID_NODES', f'小問 {question} のノードは文字列の配列です', 400)
        unknown = graph.validate_node_ids(ids, unit)
        if unknown:
            return None, make_error_response('LEARNING_UNKNOWN_NODE', f'小問 {question}: 未知のノード {", ".join(unknown)}', 400)
        normalized[str(question).strip()] = list(dict.fromkeys(ids))
    return normalized, None


def _save_node_tags(data_id, sheet, unit, normalized):
    """解答キーに単元と前提知識ノードを書き込み，更新後の解答キーを返す。"""
    answer_key = sheet['answer_key'] if isinstance(sheet['answer_key'], dict) else {}
    if 'answers' not in answer_key:
        answer_key = {'answers': answer_key} if answer_key else {'answers': {}}
    answer_key['unit'] = unit
    answer_key['nodes'] = normalized
    firebase_service = get_firebase_service()
    if firebase_service.enabled:
        firebase_service.update_answer_sheet(data_id, {'answer_key': answer_key})
    else:
        row = AnswerSheet.query.filter_by(id=data_id, teacher_id=_teacher_db_id()).first()
        row.answer_key = json.dumps(answer_key, ensure_ascii=False)
        db.session.commit()
    sheet['answer_key'] = answer_key
    return answer_key


@learning_bp.route('/answer-data/<data_id>/node-tags', methods=['PUT'])
@login_required
def set_node_tags(data_id):
    """解答キーの各小問に前提知識ノードを付ける。body: {unit, nodes: {問番号: [node_id, ...]}}"""
    payload = request.get_json(silent=True) or {}
    normalized, error = _normalize_node_tags(payload.get('unit'), payload.get('nodes'))
    if error:
        return error
    sheet, error = _load_sheet(data_id)
    if error:
        return error
    answer_key = _save_node_tags(data_id, sheet, payload.get('unit'), normalized)
    return make_success_response({'data_id': data_id, 'answer_key': answer_key})


@learning_bp.route('/answer-data/<data_id>/apply', methods=['POST'])
@login_required
def apply_to_profile(data_id):
    """演習プリントの判定結果を学習者プロファイルに反映する（同じプリントの再反映は置き換え）。"""
    sheet, error = _load_sheet(data_id)
    if error:
        return error
    try:
        result = apply_exercise(_teacher_db_id(), sheet, get_llm_client())
    except ExerciseError as exc:
        db.session.rollback()
        return make_error_response('LEARNING_EXERCISE_INVALID', str(exc), 400)
    return make_success_response({'data_id': data_id, **result})


MAX_SHEETS_PER_BULK_APPLY = 100


@learning_bp.route('/answer-data/apply-bulk', methods=['POST'])
@login_required
def apply_bulk():
    """複数の解答用紙をまとめてプロファイルに反映する。

    body: {data_ids: [...], unit?, nodes?: {問番号: [ノード]}}。unit と nodes を指定すると，反映の前に
    全ての解答用紙へ同じ前提知識ノードを付ける（同じプリントをまとめて反映するとき）。
    1枚ずつ結果を返し，反映できなかったもの（採点結果がない等）があっても残りは続ける。
    """
    payload = request.get_json(silent=True) or {}
    data_ids = payload.get('data_ids')
    if not isinstance(data_ids, list) or not data_ids or not all(isinstance(i, str) for i in data_ids):
        return make_error_response('LEARNING_INVALID_IDS', 'data_ids は解答用紙IDの配列です', 400)
    if len(data_ids) > MAX_SHEETS_PER_BULK_APPLY:
        return make_error_response('LEARNING_TOO_MANY', f'一度に反映できるのは {MAX_SHEETS_PER_BULK_APPLY} 枚までです', 400)
    normalized = None
    if payload.get('nodes') is not None:
        normalized, error = _normalize_node_tags(payload.get('unit'), payload.get('nodes'))
        if error:
            return error

    llm = get_llm_client()
    results = []
    for data_id in dict.fromkeys(data_ids):
        sheet, error = _load_sheet(data_id)
        if error:
            results.append({'data_id': data_id, 'status': 'error', 'message': '解答用紙が見つかりません'})
            continue
        try:
            if normalized is not None:
                _save_node_tags(data_id, sheet, payload['unit'], normalized)
            try:
                result = apply_exercise(_teacher_db_id(), sheet, llm)
            except OperationalError as exc:
                if 'locked' not in str(exc):
                    raise
                # 裏で読み取り・書き起こしが書き込んでいる間に重なったときは，少し待って一度だけやり直す
                db.session.rollback()
                time.sleep(2)
                result = apply_exercise(_teacher_db_id(), sheet, llm)
        except ExerciseError as exc:
            db.session.rollback()
            results.append({'data_id': data_id, 'student_id': sheet['student_id'], 'status': 'skipped', 'message': str(exc)})
            continue
        except Exception as exc:  # LLM の一時的な失敗などで1枚が止まっても残りは続ける
            db.session.rollback()
            results.append({'data_id': data_id, 'student_id': sheet['student_id'], 'status': 'error',
                            'message': f'{exc.__class__.__name__}: {str(exc)[:200]}'})
            continue
        results.append({'data_id': data_id, 'student_id': result.get('student_id'), 'status': 'applied',
                        'record_count': len(result.get('items') or []), 'warnings': result.get('warnings') or []})
    counts = {status: sum(1 for r in results if r['status'] == status) for status in ('applied', 'skipped', 'error')}
    return make_success_response({'results': results, 'counts': counts})


@learning_bp.route('/profiles', methods=['GET'])
@login_required
def list_profiles():
    profiles = (LearnerProfile.query.filter_by(teacher_id=_teacher_db_id())
                .order_by(LearnerProfile.student_id.asc()).all())
    return make_success_response({'profiles': [profile_to_dict(p) for p in profiles], 'total': len(profiles)})


@learning_bp.route('/profiles/<student_id>', methods=['GET'])
@login_required
def get_profile(student_id):
    profile = LearnerProfile.query.filter_by(teacher_id=_teacher_db_id(), student_id=student_id).first()
    if not profile:
        return make_error_response('LEARNING_PROFILE_NOT_FOUND', 'Profile not found', 404)
    include_records = request.args.get('include_records', 'true').lower() != 'false'
    return make_success_response({'profile': profile_to_dict(profile, include_records=include_records)})


@learning_bp.route('/profiles/<student_id>/rebuild', methods=['POST'])
@login_required
def rebuild_student_profile(student_id):
    teacher_db_id = _teacher_db_id()
    if not ExerciseRecord.query.filter_by(teacher_id=teacher_db_id, student_id=student_id).first():
        return make_error_response('LEARNING_PROFILE_NOT_FOUND', 'No exercise records for this student', 404)
    profile = rebuild_profile(teacher_db_id, student_id)
    db.session.commit()
    return make_success_response({'profile': profile_to_dict(profile)})


# ==================== フェーズB: 定期テスト問題 → 個別化した解説 ====================

def _problem_to_dict(problem, include_counts=True):
    payload = {
        'id': problem.id,
        'unit': problem.unit,
        'title': problem.title,
        'problem_text': problem.problem_text,
        'correct_answer': problem.correct_answer,
        'steps': json.loads(problem.steps or '[]'),
        'steps_source': problem.steps_source,
        'exam_id': problem.exam_id,
        'number': problem.number,
        'points': problem.points,
        'answer_format': problem.answer_format,
        'chain1_status': problem.chain1_status,
        'chain1_error': problem.chain1_error,
        'created_at': problem.created_at.isoformat() if problem.created_at else None,
        'updated_at': problem.updated_at.isoformat() if problem.updated_at else None,
    }
    if include_counts:
        payload['explanation_count'] = GeneratedExplanation.query.filter_by(problem_id=problem.id).count()
    return payload


def _get_problem(problem_id):
    return TestProblem.query.filter_by(id=problem_id, teacher_id=_teacher_db_id()).first()


def _llm_unavailable(exc):
    return make_error_response('LEARNING_LLM_UNAVAILABLE', str(exc), 503)


def _run_chain1(problem):
    llm = get_llm_client(required=True)
    steps = chain.decompose_problem(llm, problem.unit, problem.problem_text)
    problem.steps = json.dumps(steps, ensure_ascii=False)
    problem.steps_source = 'chain1'
    if problem.chain1_status:  # PDF から取り込んだ問題の「作成中・失敗」の印を外す
        problem.chain1_status = 'done'
        problem.chain1_error = None


@learning_bp.route('/problems', methods=['POST'])
@login_required
def create_problem():
    """定期テストの問題を登録する。steps を省略すると Chain 1（LLM）で解答ステップに分解する。"""
    payload = request.get_json(silent=True) or {}
    unit = payload.get('unit')
    title = str(payload.get('title', '')).strip()
    problem_text = str(payload.get('problem_text', '')).strip()
    if unit not in graph.unit_ids():
        return make_error_response('LEARNING_INVALID_UNIT', f'unit は {", ".join(graph.unit_ids())} のいずれかです', 400)
    if not title or not problem_text:
        return make_error_response('LEARNING_INVALID_PROBLEM', 'title と problem_text は必須です', 400)

    problem = TestProblem(
        teacher_id=_teacher_db_id(), unit=unit, title=title[:200], problem_text=problem_text,
        correct_answer=(str(payload['correct_answer']).strip() or None) if payload.get('correct_answer') else None,
    )
    try:
        if payload.get('steps') is not None:
            problem.steps = json.dumps(chain.validate_steps(payload['steps'], unit), ensure_ascii=False)
            problem.steps_source = 'teacher'
        else:
            _run_chain1(problem)
    except chain.ExplanationError as exc:
        return make_error_response('LEARNING_INVALID_STEPS', str(exc), 400)
    except LLMUnavailable as exc:
        return _llm_unavailable(exc)
    db.session.add(problem)
    db.session.commit()
    return make_success_response({'problem': _problem_to_dict(problem)}, 201)


@learning_bp.route('/problems', methods=['GET'])
@login_required
def list_problems():
    problems = (TestProblem.query.filter_by(teacher_id=_teacher_db_id())
                .order_by(TestProblem.created_at.desc()).all())
    return make_success_response({'problems': [_problem_to_dict(p) for p in problems], 'total': len(problems)})


@learning_bp.route('/problems/<problem_id>', methods=['GET'])
@login_required
def get_problem(problem_id):
    problem = _get_problem(problem_id)
    if not problem:
        return make_error_response('LEARNING_PROBLEM_NOT_FOUND', 'Problem not found', 404)
    return make_success_response({'problem': _problem_to_dict(problem)})


@learning_bp.route('/problems/<problem_id>', methods=['PUT'])
@login_required
def update_problem(problem_id):
    """問題文・正しい答え・解答ステップ（Chain 1 の結果の教員による修正）を更新する。"""
    problem = _get_problem(problem_id)
    if not problem:
        return make_error_response('LEARNING_PROBLEM_NOT_FOUND', 'Problem not found', 404)
    payload = request.get_json(silent=True) or {}
    if 'title' in payload:
        title = str(payload['title']).strip()
        if not title:
            return make_error_response('LEARNING_INVALID_PROBLEM', 'title は空にできません', 400)
        problem.title = title[:200]
    if 'problem_text' in payload:
        text = str(payload['problem_text']).strip()
        if not text:
            return make_error_response('LEARNING_INVALID_PROBLEM', 'problem_text は空にできません', 400)
        problem.problem_text = text
    if 'correct_answer' in payload:
        problem.correct_answer = str(payload['correct_answer']).strip() or None if payload['correct_answer'] else None
    if 'steps' in payload:
        try:
            problem.steps = json.dumps(chain.validate_steps(payload['steps'], problem.unit), ensure_ascii=False)
        except chain.ExplanationError as exc:
            return make_error_response('LEARNING_INVALID_STEPS', str(exc), 400)
        problem.steps_source = 'teacher'
    db.session.commit()
    return make_success_response({'problem': _problem_to_dict(problem)})


@learning_bp.route('/problems/<problem_id>', methods=['DELETE'])
@login_required
def delete_problem(problem_id):
    problem = _get_problem(problem_id)
    if not problem:
        return make_error_response('LEARNING_PROBLEM_NOT_FOUND', 'Problem not found', 404)
    GenerationJob.query.filter_by(problem_id=problem.id).delete()
    db.session.delete(problem)
    db.session.commit()
    return make_success_response({'deleted': problem_id})


@learning_bp.route('/problems/<problem_id>/decompose', methods=['POST'])
@login_required
def decompose(problem_id):
    """Chain 1 をやり直す（問題文を変更したときなど）。"""
    problem = _get_problem(problem_id)
    if not problem:
        return make_error_response('LEARNING_PROBLEM_NOT_FOUND', 'Problem not found', 404)
    try:
        _run_chain1(problem)
    except LLMUnavailable as exc:
        return _llm_unavailable(exc)
    except chain.ExplanationError as exc:
        return make_error_response('LEARNING_INVALID_STEPS', str(exc), 502)
    db.session.commit()
    return make_success_response({'problem': _problem_to_dict(problem)})


@learning_bp.route('/problems/<problem_id>/plan', methods=['GET'])
@login_required
def plan(problem_id):
    """Chain 2 の判断（どのステップに何を含めるか）だけを，LLM を呼ばずに返す。"""
    problem = _get_problem(problem_id)
    if not problem:
        return make_error_response('LEARNING_PROBLEM_NOT_FOUND', 'Problem not found', 404)
    student_id = request.args.get('student_id', '').strip()
    if not student_id:
        return make_error_response('LEARNING_STUDENT_REQUIRED', 'student_id is required', 400)
    profile = LearnerProfile.query.filter_by(teacher_id=_teacher_db_id(), student_id=student_id).first()
    if not profile:
        return make_error_response('LEARNING_PROFILE_NOT_FOUND', 'Profile not found', 404)
    decisions = chain.plan_explanation(json.loads(problem.steps or '[]'),
                                       json.loads(profile.node_states or '{}'),
                                       json.loads(profile.error_tendencies or '{}'))
    return make_success_response({'problem_id': problem_id, 'student_id': student_id, 'plan': decisions})


@learning_bp.route('/problems/<problem_id>/explanations', methods=['POST'])
@login_required
def generate(problem_id):
    """個別化した解説を生成する（ジョブとして実行）。

    body: {student_ids?: [...]（省略時はプロファイルのある全生徒）, include_master?: bool, include_subtractive?: bool}
    """
    problem = _get_problem(problem_id)
    if not problem:
        return make_error_response('LEARNING_PROBLEM_NOT_FOUND', 'Problem not found', 404)
    if not json.loads(problem.steps or '[]'):
        return make_error_response('LEARNING_INVALID_STEPS', '解答ステップがありません', 400)
    payload = request.get_json(silent=True) or {}
    teacher_db_id = _teacher_db_id()
    student_ids = payload.get('student_ids')
    if student_ids is None:
        student_ids = [p.student_id for p in LearnerProfile.query.filter_by(teacher_id=teacher_db_id)
                       .order_by(LearnerProfile.student_id.asc()).all()]
    if not isinstance(student_ids, list) or not all(isinstance(s, str) and s.strip() for s in student_ids):
        return make_error_response('LEARNING_INVALID_STUDENTS', 'student_ids は生徒IDの配列です', 400)
    student_ids = list(dict.fromkeys(s.strip() for s in student_ids))
    if len(student_ids) > MAX_STUDENTS_PER_JOB:
        return make_error_response('LEARNING_INVALID_STUDENTS', f'一度に生成できるのは {MAX_STUDENTS_PER_JOB} 人までです', 400)
    include_master = bool(payload.get('include_master', False))
    include_subtractive = bool(payload.get('include_subtractive', False))
    if not student_ids and not include_master:
        return make_error_response('LEARNING_INVALID_STUDENTS', '生成の対象となる生徒がいません', 400)

    try:
        get_llm_client(required=True)
    except LLMUnavailable as exc:
        return _llm_unavailable(exc)

    job = GenerationJob(
        teacher_id=teacher_db_id, problem_id=problem.id, status='queued',
        request=json.dumps({'student_ids': student_ids, 'include_master': include_master,
                            'include_subtractive': include_subtractive}, ensure_ascii=False),
        total=len(student_ids) + (1 if include_master or include_subtractive else 0),
        done=0, explanation_ids='[]', errors='[]',
    )
    db.session.add(job)
    db.session.commit()
    start_job(job)
    db.session.refresh(job)
    return make_success_response({'job': job_to_dict(job)}, 202)


@learning_bp.route('/jobs/<job_id>', methods=['GET'])
@login_required
def get_job(job_id):
    job = GenerationJob.query.filter_by(id=job_id, teacher_id=_teacher_db_id()).first()
    if not job:
        return make_error_response('LEARNING_JOB_NOT_FOUND', 'Job not found', 404)
    return make_success_response({'job': job_to_dict(job)})


def _explanation_to_dict(row, include_trace=True):
    payload = {
        'id': row.id,
        'problem_id': row.problem_id,
        'student_id': row.student_id,
        'kind': row.kind,
        'status': row.status,
        'content': row.content,
        'model_name': row.model_name,
        'created_at': row.created_at.isoformat() if row.created_at else None,
    }
    if include_trace:
        payload['trace'] = json.loads(row.trace or '{}')
        payload['profile_snapshot'] = json.loads(row.profile_snapshot) if row.profile_snapshot else None
    return payload


@learning_bp.route('/problems/<problem_id>/explanations', methods=['GET'])
@login_required
def list_explanations(problem_id):
    problem = _get_problem(problem_id)
    if not problem:
        return make_error_response('LEARNING_PROBLEM_NOT_FOUND', 'Problem not found', 404)
    query = GeneratedExplanation.query.filter_by(problem_id=problem.id)
    if request.args.get('student_id'):
        query = query.filter_by(student_id=request.args['student_id'])
    if request.args.get('kind'):
        query = query.filter_by(kind=request.args['kind'])
    rows = query.order_by(GeneratedExplanation.created_at.desc()).all()
    return make_success_response({'explanations': [_explanation_to_dict(r, include_trace=False) for r in rows],
                                  'total': len(rows)})


@learning_bp.route('/explanations/<explanation_id>', methods=['GET'])
@login_required
def get_explanation(explanation_id):
    row = GeneratedExplanation.query.filter_by(id=explanation_id, teacher_id=_teacher_db_id()).first()
    if not row:
        return make_error_response('LEARNING_EXPLANATION_NOT_FOUND', 'Explanation not found', 404)
    return make_success_response({'explanation': _explanation_to_dict(row)})


def _embedding_vectors(rows, client):
    """解説の埋め込みベクトルを返す（保存済みはそれを使い，無いものだけまとめて計算して保存する）。"""
    ids = [r.id for r in rows]
    cached = {e.explanation_id: json.loads(e.vector) for e in ExplanationEmbedding.query.filter(
        ExplanationEmbedding.explanation_id.in_(ids), ExplanationEmbedding.model_name == client.model_name).all()}
    missing = [r for r in rows if r.id not in cached]
    if missing:
        vectors = client.embed([r.content or '' for r in missing])
        for row, vector in zip(missing, vectors):
            vector = [float(v) for v in vector]
            cached[row.id] = vector
            db.session.add(ExplanationEmbedding(explanation_id=row.id, model_name=client.model_name,
                                                vector=json.dumps(vector)))
        db.session.commit()
    return cached


@learning_bp.route('/problems/<problem_id>/comparison', methods=['GET'])
@login_required
def comparison(problem_id):
    """マスター解説 (A)・引き算型 (B)・積み上げ型 (C) を表10の指標で比較する（各生徒の最新のものを使う）。

    類似度は文字列の一致度（difflib）と，Gemini の埋め込みによる意味の類似度（コサイン類似度）の両方を返す。
    ?semantic=false で意味の類似度を省略できる。
    """
    problem = _get_problem(problem_id)
    if not problem:
        return make_error_response('LEARNING_PROBLEM_NOT_FOUND', 'Problem not found', 404)
    rows = (GeneratedExplanation.query.filter_by(problem_id=problem.id)
            .order_by(GeneratedExplanation.created_at.desc()).all())
    latest = {}
    for row in rows:
        latest.setdefault((row.kind, row.student_id), row)
    master = latest.get(('master', None))

    semantic = {'available': False, 'model': None, 'reason': None}
    vectors = {}
    if request.args.get('semantic', 'true').lower() != 'false' and latest:
        client = get_embedding_client()
        if client is None:
            semantic['reason'] = 'GEMINI_API_KEY が設定されていないため，意味の類似度は計算していません'
        else:
            try:
                vectors = _embedding_vectors(list(latest.values()), client)
                semantic.update(available=True, model=client.model_name)
            except Exception as exc:
                db.session.rollback()
                semantic['reason'] = f'埋め込みの計算に失敗しました（{exc.__class__.__name__}: {exc}）'
    elif latest:
        semantic['reason'] = 'semantic=false が指定されました'

    def semantic_sim(a, b):
        if a.id in vectors and b.id in vectors:
            return chain.cosine_similarity(vectors[a.id], vectors[b.id])
        return None

    students = sorted({sid for (kind, sid) in latest if sid})
    per_student = []
    for sid in students:
        snapshot_row = latest.get(('individual', sid)) or latest.get(('subtractive', sid))
        node_states = (json.loads(snapshot_row.profile_snapshot or '{}').get('node_states', {})
                       if snapshot_row else {})
        entry = {'student_id': sid}
        for kind in ('individual', 'subtractive'):
            row = latest.get((kind, sid))
            if row is None:
                continue
            trace = json.loads(row.trace or '{}')
            entry[kind] = {
                'explanation_id': row.id,
                'metrics': chain.explanation_metrics(trace, node_states),
                'similarity_to_master': chain.text_similarity(row.content or '', master.content or '') if master else None,
                'semantic_similarity_to_master': semantic_sim(row, master) if master else None,
            }
        if master:
            entry['master_metrics'] = chain.explanation_metrics(json.loads(master.trace or '{}'), node_states)
        per_student.append(entry)

    def mean(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 3) if values else None

    between_students = {}
    for kind in ('individual', 'subtractive'):
        kind_rows = [latest[(kind, sid)] for sid in students if (kind, sid) in latest]
        pairs = [(a, b) for i, a in enumerate(kind_rows) for b in kind_rows[i + 1:]]
        between_students[kind] = {
            'pair_count': len(pairs),
            'mean_similarity': mean([chain.text_similarity(a.content or '', b.content or '') for a, b in pairs]),
            'mean_semantic_similarity': mean([semantic_sim(a, b) for a, b in pairs]),
        }

    return make_success_response({
        'problem_id': problem_id,
        'master_explanation_id': master.id if master else None,
        'semantic_similarity': semantic,
        'students': per_student,
        'between_students': between_students,
    })


# ==================== 定期試験の PDF からの取り込み ====================

MAX_EXAM_PAGES = 30


def _exam_to_dict(exam):
    pages = json.loads(exam.pages or '[]')
    problems = TestProblem.query.filter_by(exam_id=exam.id, teacher_id=_teacher_db_id()).all()
    statuses = [p.chain1_status for p in problems]
    return {
        'id': exam.id, 'name': exam.name, 'status': exam.status, 'error': exam.error, 'model_name': exam.model_name,
        'reading': exam_import.is_running(f'read:{exam.id}'),
        'pages': [{'index': i, 'kind': page['kind']} for i, page in enumerate(pages)],
        'candidates': json.loads(exam.candidates or '[]'),
        'problem_count': len(problems),
        'chain1': {s: statuses.count(s) for s in ('pending', 'done', 'error')},
        'chain1_running': exam_import.is_running(f'chain1:{exam.id}'),
        'created_at': exam.created_at.isoformat() if exam.created_at else None,
    }


def _get_exam(exam_id):
    return ExamImport.query.filter_by(id=exam_id, teacher_id=_teacher_db_id()).first()


@learning_bp.route('/exam-imports', methods=['POST'])
@login_required
def import_exam_pdf():
    """問題用紙（必須）と模範解答（任意）の PDF を受け取り，Gemini で問題を読み取り始める。"""
    problem_pdf = request.files.get('problem_pdf')
    answer_pdf = request.files.get('answer_pdf')
    if problem_pdf is None or not problem_pdf.filename:
        return make_error_response('LEARNING_EXAM_PDF_MISSING', '問題用紙の PDF を選んでください', 400)
    try:
        get_vision_client(required=True)
    except LLMUnavailable as exc:
        return _llm_unavailable(exc)
    name = (request.form.get('name') or '').strip() or problem_pdf.filename
    exam = ExamImport(teacher_id=_teacher_db_id(), name=name[:200], status='reading')
    db.session.add(exam)
    db.session.flush()
    directory = exam_import.exam_dir(current_app.config.get('UPLOAD_FOLDER') or 'uploads', exam.id)
    os.makedirs(directory, exist_ok=True)
    pages = []
    try:
        for kind, upload in (('problem', problem_pdf), ('answer', answer_pdf)):
            if upload is None or not upload.filename:
                continue
            source = os.path.join(directory, f'{kind}{os.path.splitext(upload.filename)[1].lower() or ".pdf"}')
            upload.save(source)
            for path in split_into_pages(source, os.path.join(directory, 'pages'), kind, dpi=exam_import.EXAM_DPI):
                pages.append({'kind': kind, 'path': path})
    except Exception as exc:
        db.session.rollback()
        shutil.rmtree(directory, ignore_errors=True)
        return make_error_response('LEARNING_EXAM_PDF_INVALID', f'PDF をページに分けられませんでした（{exc.__class__.__name__}）', 400)
    if len(pages) > MAX_EXAM_PAGES:
        db.session.rollback()
        shutil.rmtree(directory, ignore_errors=True)
        return make_error_response('LEARNING_EXAM_TOO_MANY_PAGES', f'ページ数は合わせて {MAX_EXAM_PAGES} までです', 400)
    exam.pages = json.dumps(pages, ensure_ascii=False)
    db.session.commit()
    exam_import.start_reading(current_app._get_current_object(), exam.id)
    db.session.expire_all()
    return make_success_response({'exam': _exam_to_dict(_get_exam(exam.id))}, 201)


@learning_bp.route('/exam-imports', methods=['GET'])
@login_required
def list_exam_imports():
    exams = ExamImport.query.filter_by(teacher_id=_teacher_db_id()).order_by(ExamImport.created_at.desc()).all()
    return make_success_response({'exams': [_exam_to_dict(e) for e in exams]})


@learning_bp.route('/exam-imports/<exam_id>', methods=['GET'])
@login_required
def get_exam_import(exam_id):
    exam = _get_exam(exam_id)
    if exam is None:
        return make_error_response('LEARNING_EXAM_NOT_FOUND', 'Exam not found', 404)
    return make_success_response({'exam': _exam_to_dict(exam)})


@learning_bp.route('/exam-imports/<exam_id>/pages/<int:index>/image', methods=['GET'])
@login_required
def exam_page_image(exam_id, index):
    exam = _get_exam(exam_id)
    pages = json.loads(exam.pages or '[]') if exam else []
    if exam is None or not 0 <= index < len(pages) or not os.path.exists(pages[index]['path']):
        return make_error_response('LEARNING_EXAM_NOT_FOUND', 'Page not found', 404)
    return send_file(os.path.abspath(pages[index]['path']), mimetype='image/jpeg', max_age=3600)


@learning_bp.route('/exam-imports/<exam_id>/read', methods=['POST'])
@login_required
def reread_exam(exam_id):
    """読み取りに失敗したとき・読み取り直したいときに，もう一度 Gemini で読み取る。"""
    exam = _get_exam(exam_id)
    if exam is None:
        return make_error_response('LEARNING_EXAM_NOT_FOUND', 'Exam not found', 404)
    exam.status = 'reading'
    db.session.commit()
    if not exam_import.start_reading(current_app._get_current_object(), exam.id):
        return make_error_response('LEARNING_BUSY', '読み取りの途中です', 409)
    db.session.expire_all()
    return make_success_response({'exam': _exam_to_dict(_get_exam(exam_id))}, 202)


@learning_bp.route('/exam-imports/<exam_id>/save', methods=['POST'])
@login_required
def save_exam_import(exam_id):
    """教員が確かめた候補を定期テストの問題として登録し，Chain 1 を裏で順に実行する。"""
    exam = _get_exam(exam_id)
    if exam is None:
        return make_error_response('LEARNING_EXAM_NOT_FOUND', 'Exam not found', 404)
    questions = (request.get_json(silent=True) or {}).get('questions')
    if not isinstance(questions, list) or not questions:
        return make_error_response('LEARNING_INVALID_PROBLEM', '登録する問題を選んでください', 400)
    units = set(graph.unit_ids())
    created = []
    for q in questions:
        problem_text = str(q.get('problem_text') or '').strip()
        if not problem_text or q.get('unit') not in units:
            return make_error_response('LEARNING_INVALID_PROBLEM',
                                       f'問題 {q.get("number") or ""} の問題文と単元を確かめてください', 400)
        try:
            points = int(q['points']) if q.get('points') not in (None, '') else None
        except (TypeError, ValueError):
            points = None
        problem = TestProblem(
            teacher_id=_teacher_db_id(), unit=q['unit'], title=exam_import.problem_title(q) or '（無題）',
            problem_text=problem_text, correct_answer=str(q.get('correct_answer') or '').strip() or None,
            exam_id=exam.id, number=str(q.get('number') or '').strip()[:30] or None, points=points,
            answer_format=q.get('answer_format') if q.get('answer_format') in ('answer_only', 'written') else None,
            chain1_status='pending', steps='[]',
        )
        db.session.add(problem)
        created.append(problem)
    exam.status = 'saved'
    db.session.commit()
    created_ids = [p.id for p in created]
    exam_import.start_chain1(current_app._get_current_object(), exam.id)
    db.session.expire_all()
    problems = [db.session.get(TestProblem, pid) for pid in created_ids]
    return make_success_response({'problems': [_problem_to_dict(p, include_counts=False) for p in problems],
                                  'exam': _exam_to_dict(_get_exam(exam_id))}, 201)


@learning_bp.route('/exam-imports/<exam_id>/chain1', methods=['POST'])
@login_required
def retry_exam_chain1(exam_id):
    """分解に失敗した問題の Chain 1 をやり直す。"""
    exam = _get_exam(exam_id)
    if exam is None:
        return make_error_response('LEARNING_EXAM_NOT_FOUND', 'Exam not found', 404)
    for problem in TestProblem.query.filter_by(exam_id=exam.id, chain1_status='error').all():
        problem.chain1_status = 'pending'
    db.session.commit()
    exam_import.start_chain1(current_app._get_current_object(), exam.id)
    db.session.expire_all()
    return make_success_response({'exam': _exam_to_dict(_get_exam(exam_id))}, 202)
