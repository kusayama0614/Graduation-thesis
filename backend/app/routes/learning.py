# ==================== 個別最適化解説生成ルート ====================
"""
学習者プロファイル（フェーズA）と個別化した解説の生成（フェーズB）の API（卒業論文 3章）
"""
import json
import re
import os
import shutil
import time
from datetime import date, datetime

from sqlalchemy.exc import OperationalError
from flask import Blueprint, current_app, request, send_file

from app import db
from app.learning import exam_generation, exam_import, pdf_export, tex_template
from app.learning import explanation as chain
from app.learning import graph
from app.learning.jobs import job_to_dict, problem_to_chain_input, start_job
from app.learning.llm import LLMUnavailable, get_embedding_client, get_llm_client, get_vision_client
from app.learning.profile import (
    ExerciseError, apply_exercise, key_answers, key_nodes, profile_to_dict, rebuild_profile,
)
from app.models import (
    AnswerSheet, ExamGenerationJob, ExamImport, PdfExportJob, ExerciseRecord, ExplanationEmbedding, GeneratedExplanation,
    GenerationJob, LearnerProfile, TestProblem,
)
from app.routes.analysis import _resolve_teacher_ids
from app.routes.auth import login_required
from app.utils.api_response import make_error_response, make_success_response
from app.utils.bulk_import import split_into_pages
from app.utils.firebase_service import get_firebase_service
from app.utils.gemini import gemini_model_name
from app.utils.math_verification import judge_answer

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
        'has_template': bool(problem.template),
        'answer_check': _answer_check(problem),
        'created_at': problem.created_at.isoformat() if problem.created_at else None,
        'updated_at': problem.updated_at.isoformat() if problem.updated_at else None,
    }
    if include_counts:
        payload['explanation_count'] = GeneratedExplanation.query.filter_by(problem_id=problem.id).count()
    return payload


def _answer_check(problem):
    """模範解答と，Chain 1 が問題文から導いた最後のステップの結果が一致するかを sympy で確かめる（表6 層1の考え方）。

    略解をそのまま信じず，食い違えば模範解答の転記・読み取りの誤りか分解の誤りを教員に確かめてもらう。
    """
    steps = json.loads(problem.steps or '[]')
    if problem.template:
        return {'status': 'template', 'detail': '答はマスター解説のテンプレートの「答」を使います'}
    if not problem.correct_answer or not steps or not steps[-1].get('result'):
        return {'status': 'unchecked', 'detail': '模範解答または解答ステップがありません'}
    result = steps[-1]['result']
    candidates = [result] + ([result.split('=', 1)[1]] if result.count('=') == 1 else [])
    try:
        for candidate in candidates:
            verdict = judge_answer(problem.correct_answer, candidate)
            if verdict['is_correct']:
                return {'status': 'match', 'detail': f'最後のステップの結果「{result}」と一致します'}
    except Exception:
        return {'status': 'unchecked', 'detail': '式として比べられませんでした'}
    if verdict['method'] in ('string_mismatch', 'empty', 'type_mismatch'):
        return {'status': 'unchecked', 'detail': f'式として比べられませんでした（最後のステップの結果「{result}」）'}
    return {'status': 'mismatch', 'detail': f'最後のステップの結果「{result}」と模範解答が一致しません'}


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

    body: {student_ids?: [...]（省略時はプロファイルのある全生徒）, include_master?: bool}
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
    if not student_ids and not include_master:
        return make_error_response('LEARNING_INVALID_STUDENTS', '生成の対象となる生徒がいません', 400)

    try:
        get_llm_client(required=True)
    except LLMUnavailable as exc:
        return _llm_unavailable(exc)

    job = GenerationJob(
        teacher_id=teacher_db_id, problem_id=problem.id, status='queued',
        request=json.dumps({'student_ids': student_ids, 'include_master': include_master}, ensure_ascii=False),
        total=len(student_ids) + (1 if include_master else 0),
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
        'reused_from': row.reused_from,
        'format': json.loads(row.trace or '{}').get('format', 'markdown'),
        'reviewed': bool(row.reviewed),
        'edited': row.original_content is not None,
        'created_at': row.created_at.isoformat() if row.created_at else None,
    }
    if include_trace:
        payload['original_content'] = row.original_content
        payload['trace'] = json.loads(row.trace or '{}')
        payload['format'] = payload['trace'].get('format', 'markdown')
        payload['profile_snapshot'] = json.loads(row.profile_snapshot) if row.profile_snapshot else None
    return payload


@learning_bp.route('/problems/<problem_id>/explanations', methods=['GET'])
@login_required
def list_explanations(problem_id):
    problem = _get_problem(problem_id)
    if not problem:
        return make_error_response('LEARNING_PROBLEM_NOT_FOUND', 'Problem not found', 404)
    # 以前作った引き算型（比較の対象から外した）は一覧に出さない
    query = GeneratedExplanation.query.filter(GeneratedExplanation.problem_id == problem.id,
                                              GeneratedExplanation.kind != 'subtractive')
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


@learning_bp.route('/explanations/<explanation_id>', methods=['PUT'])
@login_required
def review_explanation(explanation_id):
    """教員が解説を確かめる（直す）。要確認の解説は，ここで確認済みにしてから配布する（3.5節）。

    body: {content?: 直した本文, reviewed?: bool（既定 true）, apply_to_group?: bool（既定 true。
           同じ判断で使い回した生徒の解説にも同じ修正を反映する）}
    """
    row = GeneratedExplanation.query.filter_by(id=explanation_id, teacher_id=_teacher_db_id()).first()
    if not row:
        return make_error_response('LEARNING_EXPLANATION_NOT_FOUND', 'Explanation not found', 404)
    payload = request.get_json(silent=True) or {}
    content = payload.get('content')
    if content is not None and (not isinstance(content, str) or not content.strip()):
        return make_error_response('LEARNING_INVALID_CONTENT', '本文が空です', 400)
    reviewed = bool(payload.get('reviewed', True))
    rows = [row]
    if payload.get('apply_to_group', True) and row.kind == 'individual':
        root = row.reused_from or row.id
        rows = GeneratedExplanation.query.filter(
            GeneratedExplanation.teacher_id == row.teacher_id,
            db.or_(GeneratedExplanation.id == root, GeneratedExplanation.reused_from == root)).all()
        # 生徒ごとに直した解説（本文が元と異なるもの）は上書きしない
        rows = [r for r in rows if r.id == row.id or r.content == row.content]
    for target in rows:
        if content is not None and content != target.content:
            if target.original_content is None:
                target.original_content = target.content
            target.content = content
        target.reviewed = reviewed
    db.session.commit()
    return make_success_response({'explanation': _explanation_to_dict(row), 'updated': len(rows)})


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
    """マスター解説と積み上げ型（個別化した解説）を表10の指標で比較する（各生徒の最新のものを使う）。

    類似度は文字列の一致度（difflib）と，Gemini の埋め込みによる意味の類似度（コサイン類似度）の両方を返す。
    ?semantic=false で意味の類似度を省略できる。
    """
    problem = _get_problem(problem_id)
    if not problem:
        return make_error_response('LEARNING_PROBLEM_NOT_FOUND', 'Problem not found', 404)
    rows = (GeneratedExplanation.query.filter(GeneratedExplanation.problem_id == problem.id,
                                              GeneratedExplanation.kind.in_(('master', 'individual')))
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
        snapshot_row = latest.get(('individual', sid))
        node_states = (json.loads(snapshot_row.profile_snapshot or '{}').get('node_states', {})
                       if snapshot_row else {})
        entry = {'student_id': sid}
        for kind in ('individual',):
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
    for kind in ('individual',):
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
        'template': {'name': exam.template_name, 'problems': sorted((p.number for p in problems if p.template),
                                                                    key=exam_generation.number_key)}
        if exam.template_tex else None,
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


# ==================== 定期試験の解説をまとめて生成し，生徒ごとに配布する ====================

def _exam_problems(exam_id, problem_ids=None):
    problems = TestProblem.query.filter_by(exam_id=exam_id, teacher_id=_teacher_db_id()).all()
    if problem_ids is not None:
        wanted = set(problem_ids)
        problems = [p for p in problems if p.id in wanted]
    return sorted(problems, key=lambda p: (exam_generation.number_key(p.number), p.created_at))


def _select_students(source):
    """student_ids（生徒IDの配列）か classes（「1-1」などの配列）で対象の生徒を選ぶ。どちらもなければ全員。"""
    profiles = LearnerProfile.query.filter_by(teacher_id=_teacher_db_id()).all()
    known = {p.student_id: p for p in profiles}
    student_ids = source.get('student_ids')
    classes = source.get('classes')
    if isinstance(classes, str):
        classes = [c for c in classes.split(',') if c.strip()]
    if isinstance(student_ids, str):
        student_ids = [s for s in student_ids.split(',') if s.strip()]
    if student_ids:
        if not isinstance(student_ids, list) or not all(isinstance(s, str) for s in student_ids):
            raise ValueError('student_ids は生徒IDの配列です')
        selected = [s.strip() for s in student_ids]
    elif classes:
        wanted = {c.strip() for c in classes}
        selected = [sid for sid in known if exam_generation.class_of(sid) in wanted]
    else:
        selected = list(known)
    selected = sorted(dict.fromkeys(selected), key=exam_generation.number_key)
    return selected, known


def _current_model_name():
    llm = get_llm_client()
    return getattr(llm, 'model_name', None) or gemini_model_name()


def _exam_job_options(payload):
    return {
        'include_master': bool(payload.get('include_master', False)),
        'reuse': bool(payload.get('reuse', True)),
    }


@learning_bp.route('/exams/<exam_id>/generation-plan', methods=['GET'])
@login_required
def exam_generation_plan(exam_id):
    """生成する前の見積もり（LLM は呼ばない）: 問題ごとに，生成・使い回し・省略の件数と Gemini の呼び出し回数の目安。"""
    exam = _get_exam(exam_id)
    if exam is None:
        return make_error_response('LEARNING_EXAM_NOT_FOUND', 'Exam not found', 404)
    try:
        student_ids, known = _select_students(request.args)
    except ValueError as exc:
        return make_error_response('LEARNING_INVALID_STUDENTS', str(exc), 400)
    reuse = request.args.get('reuse', 'true').lower() != 'false'
    profiles = {sid: exam_generation.student_states(known[sid]) for sid in student_ids if sid in known}
    model_name = _current_model_name()
    problems = []
    totals = {'generate': 0, 'reuse': 0, 'skip': 0, 'llm_calls': 0}
    for problem in _exam_problems(exam.id):
        problem_input = problem_to_chain_input(problem)
        entry = {'id': problem.id, 'number': problem.number, 'title': problem.title,
                 'step_count': len(problem_input['steps']), 'chain1_status': problem.chain1_status}
        if problem_input['steps']:
            rows = GeneratedExplanation.query.filter_by(problem_id=problem.id, kind='individual').all()
            entry.update(exam_generation.plan_problem(problem_input, profiles, model_name, rows, reuse))
        elif problem.chain1_status == 'pending':
            entry['pending'] = True
            problems.append(entry)
            continue
        else:
            rows = GeneratedExplanation.query.filter_by(problem_id=problem.id, kind='common').all()
            entry.update(exam_generation.plan_common(problem_input, len(profiles), model_name, rows))
        for key in totals:
            totals[key] += entry[key]
        problems.append(entry)
    return make_success_response({
        'exam_id': exam.id, 'model_name': model_name, 'student_count': len(profiles),
        'missing_profiles': [sid for sid in student_ids if sid not in known],
        'problems': problems, 'totals': totals,
    })


@learning_bp.route('/exams/<exam_id>/generation-jobs', methods=['POST'])
@login_required
def start_exam_generation(exam_id):
    """試験の全問題（または選んだ問題）× 生徒の解説をまとめて生成する。

    body: {student_ids? | classes?, problem_ids?, include_master?, reuse?（既定 true）}
    """
    exam = _get_exam(exam_id)
    if exam is None:
        return make_error_response('LEARNING_EXAM_NOT_FOUND', 'Exam not found', 404)
    payload = request.get_json(silent=True) or {}
    try:
        student_ids, known = _select_students(payload)
    except ValueError as exc:
        return make_error_response('LEARNING_INVALID_STUDENTS', str(exc), 400)
    student_ids = [sid for sid in student_ids if sid in known]
    if not student_ids:
        return make_error_response('LEARNING_INVALID_STUDENTS', '学習者プロファイルのある生徒が選ばれていません', 400)
    # 解答ステップがない問題も対象にする（全員共通の解説を作る）
    problems = _exam_problems(exam.id, payload.get('problem_ids'))
    if not problems:
        return make_error_response('LEARNING_PROBLEM_NOT_FOUND', '対象の問題がありません', 400)
    running = [j for j in ExamGenerationJob.query.filter_by(exam_id=exam.id, teacher_id=_teacher_db_id()).all()
               if exam_generation.is_running(j.id)]
    if running:
        return make_error_response('LEARNING_JOB_RUNNING', 'この試験の解説を生成中です。終わってから実行してください', 409)
    try:
        get_llm_client(required=True)
    except LLMUnavailable as exc:
        return _llm_unavailable(exc)

    job = ExamGenerationJob(
        teacher_id=_teacher_db_id(), exam_id=exam.id, status='queued',
        request=json.dumps({'student_ids': student_ids, 'problem_ids': [p.id for p in problems],
                            **_exam_job_options(payload)}, ensure_ascii=False),
        total=len(student_ids) * len(problems), done=0, errors='[]',
    )
    db.session.add(job)
    db.session.commit()
    exam_generation.start(current_app._get_current_object(), job.id)
    db.session.expire_all()
    return make_success_response({'job': exam_generation.job_to_dict(db.session.get(ExamGenerationJob, job.id))}, 202)


@learning_bp.route('/exams/<exam_id>/generation-jobs', methods=['GET'])
@login_required
def list_exam_generation_jobs(exam_id):
    jobs = (ExamGenerationJob.query.filter_by(exam_id=exam_id, teacher_id=_teacher_db_id())
            .order_by(ExamGenerationJob.created_at.desc()).limit(10).all())
    return make_success_response({'jobs': [exam_generation.job_to_dict(j) for j in jobs]})


def _get_exam_job(job_id):
    return ExamGenerationJob.query.filter_by(id=job_id, teacher_id=_teacher_db_id()).first()


@learning_bp.route('/exam-generation-jobs/<job_id>', methods=['GET'])
@login_required
def get_exam_generation_job(job_id):
    job = _get_exam_job(job_id)
    if job is None:
        return make_error_response('LEARNING_JOB_NOT_FOUND', 'Job not found', 404)
    return make_success_response({'job': exam_generation.job_to_dict(job)})


@learning_bp.route('/exam-generation-jobs/<job_id>/resume', methods=['POST'])
@login_required
def resume_exam_generation_job(job_id):
    """一時停止・失敗したジョブを続きから実行する（済んだ解説は指紋が同じなので作り直さない）。"""
    job = _get_exam_job(job_id)
    if job is None:
        return make_error_response('LEARNING_JOB_NOT_FOUND', 'Job not found', 404)
    if exam_generation.is_running(job.id):
        return make_error_response('LEARNING_JOB_RUNNING', 'このジョブは実行中です', 409)
    try:
        get_llm_client(required=True)
    except LLMUnavailable as exc:
        return _llm_unavailable(exc)
    job.status = 'queued'
    db.session.commit()
    exam_generation.start(current_app._get_current_object(), job.id)
    db.session.expire_all()
    return make_success_response({'job': exam_generation.job_to_dict(_get_exam_job(job_id))}, 202)


def _latest_explanations(problem_ids, kind='individual'):
    """{(problem_id, student_id): 最新の解説}（kind='common' のときは student_id が None）"""
    latest = {}
    if not problem_ids:
        return latest
    rows = (GeneratedExplanation.query.filter(GeneratedExplanation.problem_id.in_(problem_ids),
                                              GeneratedExplanation.kind == kind)
            .order_by(GeneratedExplanation.created_at.asc()).all())
    for row in rows:
        latest[(row.problem_id, row.student_id)] = row
    return latest


@learning_bp.route('/exams/<exam_id>/explanations-summary', methods=['GET'])
@login_required
def exam_explanations_summary(exam_id):
    """試験の問題ごとに，解説の揃い具合・要確認の件数・模範解答の照合結果をまとめる。

    ?classes=1-1,1-2 / ?student_ids=... で対象の生徒を絞れる（省略時はプロファイルのある全員）。
    review には，要確認でまだ教員が確かめていない解説を，使い回しの元ごとに1件ずつ並べる。
    """
    exam = _get_exam(exam_id)
    if exam is None:
        return make_error_response('LEARNING_EXAM_NOT_FOUND', 'Exam not found', 404)
    try:
        student_ids, known = _select_students(request.args)
    except ValueError as exc:
        return make_error_response('LEARNING_INVALID_STUDENTS', str(exc), 400)
    student_ids = [sid for sid in student_ids if sid in known]
    problems = _exam_problems(exam.id)
    latest = _latest_explanations([p.id for p in problems])
    common = _latest_explanations([p.id for p in problems], kind='common')
    summary, review = [], []
    for problem in problems:
        rows = [latest[(problem.id, sid)] for sid in student_ids if (problem.id, sid) in latest]
        common_row = common.get((problem.id, None))
        if not json.loads(problem.steps or '[]'):
            # 解答ステップがない問題は全員共通の解説を配る
            pending = bool(common_row and common_row.status == 'needs_review' and not common_row.reviewed)
            if pending:
                review.append({'problem_id': problem.id, 'number': problem.number, 'title': problem.title,
                               'explanation_id': common_row.id, 'student_ids': [], 'common': True,
                               'warnings': json.loads(common_row.trace or '{}').get('warnings', [])})
            covered = len(student_ids) if common_row else 0
            summary.append({
                'id': problem.id, 'number': problem.number, 'title': problem.title, 'points': problem.points,
                'step_count': 0, 'chain1_status': problem.chain1_status, 'answer_check': _answer_check(problem),
                'common': True, 'students': len(student_ids), 'with_explanation': covered,
                'distinct_explanations': 1 if common_row else 0,
                'needs_review': covered if common_row and common_row.status == 'needs_review' else 0,
                'pending_review': covered if pending else 0,
                'reviewed': covered if common_row and common_row.reviewed else 0,
            })
            continue
        groups = {}
        for row in rows:
            groups.setdefault(row.reused_from or row.id, []).append(row)
        pending_review = 0
        for root, members in groups.items():
            first = members[0]
            if first.status == 'needs_review' and not first.reviewed:
                pending_review += len(members)
                review.append({'problem_id': problem.id, 'number': problem.number, 'title': problem.title,
                               'explanation_id': first.id, 'student_ids': [m.student_id for m in members],
                               'warnings': json.loads(first.trace or '{}').get('warnings', [])})
        summary.append({
            'id': problem.id, 'number': problem.number, 'title': problem.title, 'points': problem.points,
            'step_count': len(json.loads(problem.steps or '[]')), 'chain1_status': problem.chain1_status,
            'answer_check': _answer_check(problem), 'common': False,
            'students': len(student_ids), 'with_explanation': len(rows), 'distinct_explanations': len(groups),
            'needs_review': sum(1 for r in rows if r.status == 'needs_review'),
            'pending_review': pending_review, 'reviewed': sum(1 for r in rows if r.reviewed),
        })
    return make_success_response({'exam': {'id': exam.id, 'name': exam.name}, 'student_count': len(student_ids),
                                  'problems': summary, 'review': review})


@learning_bp.route('/exams/<exam_id>/booklet', methods=['GET'])
@login_required
def exam_booklet(exam_id):
    """生徒ごとの解説冊子（印刷用）。問題番号の順に，その生徒の最新の積み上げ型の解説を並べる。

    ?classes=1-1 / ?student_ids=... で対象を選ぶ。要確認で未確認の解説は ready=false として返す。
    解答ステップがない問題（Chain 1 で分解できなかった問題）は，全員共通の解説（common=true）を載せる。
    """
    exam = _get_exam(exam_id)
    if exam is None:
        return make_error_response('LEARNING_EXAM_NOT_FOUND', 'Exam not found', 404)
    try:
        student_ids, known = _select_students(request.args)
    except ValueError as exc:
        return make_error_response('LEARNING_INVALID_STUDENTS', str(exc), 400)
    student_ids = [sid for sid in student_ids if sid in known]
    problems = _exam_problems(exam.id)
    latest = _latest_explanations([p.id for p in problems])
    common = _latest_explanations([p.id for p in problems], kind='common')
    students = []
    for sid in student_ids:
        items = []
        for problem in problems:
            row = latest.get((problem.id, sid))
            is_common = row is None and (problem.id, None) in common
            if is_common:
                row = common[(problem.id, None)]
            items.append({
                'problem_id': problem.id, 'number': problem.number, 'title': problem.title,
                'problem_text': problem.problem_text, 'points': problem.points, 'common': is_common,
                'format': json.loads(row.trace or '{}').get('format', 'markdown') if row else None,
                'explanation_id': row.id if row else None, 'content': row.content if row else None,
                'status': row.status if row else None, 'reviewed': bool(row.reviewed) if row else False,
                'ready': bool(row) and (row.status == 'ok' or bool(row.reviewed)),
            })
        traces = [json.loads(latest[(p.id, sid)].trace or '{}') for p in problems if (p.id, sid) in latest]
        evidence = pdf_export.personalization_evidence(traces, *_evidence_settings())
        students.append({'student_id': sid, 'items': items, 'insufficient': evidence['insufficient'],
                         'evidence': evidence,
                         'ready': all(i['ready'] for i in items),
                         'missing': sum(1 for i in items if not i['explanation_id'])})
    return make_success_response({'exam': {'id': exam.id, 'name': exam.name}, 'students': students})


# ==================== マスター解説の TeX（個別の解説のテンプレート） ====================

MAX_TEMPLATE_BYTES = 2 * 1024 * 1024


@learning_bp.route('/exam-imports/<exam_id>/template', methods=['POST'])
@login_required
def upload_exam_template(exam_id):
    """教員が作ったマスター解説の TeX を読み込み，問題番号ごとにテンプレートとして登録する。

    multipart の file か，JSON の {"tex": "...", "name": "..."}。対応した問題は，テンプレートの「手順」を解答ステップに
    置き換え，裏で前提知識ノードだけを付ける（Chain 1 の分解の代わり）。個別の解説は，この体裁のまま各手順の本文だけを
    生徒に合わせて書き直す。
    """
    exam = _get_exam(exam_id)
    if exam is None:
        return make_error_response('LEARNING_EXAM_NOT_FOUND', 'Exam not found', 404)
    upload = request.files.get('file')
    if upload is not None and upload.filename:
        raw = upload.read(MAX_TEMPLATE_BYTES + 1)
        name = upload.filename
    else:
        payload = request.get_json(silent=True) or {}
        raw = str(payload.get('tex') or '').encode('utf-8')
        name = str(payload.get('name') or 'template.tex')
    if not raw:
        return make_error_response('LEARNING_TEMPLATE_MISSING', 'TeX ファイルを選んでください', 400)
    if len(raw) > MAX_TEMPLATE_BYTES:
        return make_error_response('LEARNING_TEMPLATE_TOO_LARGE', 'TeX ファイルが大きすぎます（2MB まで）', 400)
    try:
        tex = raw.decode('utf-8-sig')
        parsed = tex_template.parse(tex)
        sections = tex_template.sections(parsed)
    except UnicodeDecodeError:
        return make_error_response('LEARNING_TEMPLATE_INVALID', 'TeX ファイルは UTF-8 で保存してください', 400)
    except tex_template.TemplateError as exc:
        return make_error_response('LEARNING_TEMPLATE_INVALID', str(exc), 400)

    problems = _exam_problems(exam.id)
    matched, without = [], []
    for problem in problems:
        section = sections.get(tex_template.normalize_number(problem.number or ''))
        if section is None:
            if problem.template:
                problem.template = None
                if problem.steps_source == 'template':  # テンプレートから外れた問題は問題文から分解し直す
                    problem.steps, problem.chain1_status = '[]', 'pending'
            without.append(problem.number)
            continue
        problem.template = json.dumps(section, ensure_ascii=False)
        steps = tex_template.template_steps(section)
        problem.steps = json.dumps(steps, ensure_ascii=False)
        problem.steps_source = 'template'
        problem.chain1_status, problem.chain1_error = ('pending' if steps else 'done'), None
        matched.append({'number': problem.number, 'steps': len(steps)})
    exam.template_tex = tex
    exam.template_name = name[:200]
    db.session.commit()
    exam_import.start_chain1(current_app._get_current_object(), exam.id)
    db.session.expire_all()
    used = {tex_template.normalize_number(p.number or '') for p in problems}
    return make_success_response({
        'exam': _exam_to_dict(_get_exam(exam_id)), 'matched': matched, 'problems_without_template': without,
        'unused_sections': [n for n in sections if n not in used],
    })


@learning_bp.route('/exam-imports/<exam_id>/template', methods=['DELETE'])
@login_required
def delete_exam_template(exam_id):
    """テンプレートを外す（テンプレートから作った解答ステップは，問題文から Chain 1 で分解し直す）。"""
    exam = _get_exam(exam_id)
    if exam is None:
        return make_error_response('LEARNING_EXAM_NOT_FOUND', 'Exam not found', 404)
    for problem in _exam_problems(exam.id):
        if problem.template or problem.steps_source == 'template':
            problem.template = None
            problem.steps, problem.chain1_status, problem.chain1_error = '[]', 'pending', None
    exam.template_tex = exam.template_name = None
    db.session.commit()
    exam_import.start_chain1(current_app._get_current_object(), exam.id)
    db.session.expire_all()
    return make_success_response({'exam': _exam_to_dict(_get_exam(exam_id))})


@learning_bp.route('/exams/<exam_id>/booklet.tex', methods=['GET'])
@login_required
def exam_booklet_tex(exam_id):
    """生徒ごとの個別解説を，テンプレートと同じ体裁の TeX 文書にする（?classes=1-1 / ?student_ids=...）。

    複数の生徒は改ページして1つの文書に続ける。個別の解説がない小問はテンプレートのまま載せる。
    """
    exam = _get_exam(exam_id)
    if exam is None:
        return make_error_response('LEARNING_EXAM_NOT_FOUND', 'Exam not found', 404)
    if not exam.template_tex:
        return make_error_response('LEARNING_TEMPLATE_MISSING', 'マスター解説の TeX を先に読み込んでください', 400)
    try:
        student_ids, known = _select_students(request.args)
    except ValueError as exc:
        return make_error_response('LEARNING_INVALID_STUDENTS', str(exc), 400)
    student_ids = [sid for sid in student_ids if sid in known]
    if not student_ids:
        return make_error_response('LEARNING_INVALID_STUDENTS', '対象の生徒がいません', 400)
    parsed = tex_template.parse(exam.template_tex)
    problems = [p for p in _exam_problems(exam.id) if p.template]
    latest = _latest_explanations([p.id for p in problems])
    common = _latest_explanations([p.id for p in problems], kind='common')

    def latex_of(row):
        return row.content if row and json.loads(row.trace or '{}').get('format') == 'latex' else None

    students = []
    for sid in student_ids:
        contents = {}
        for problem in problems:
            content = latex_of(latest.get((problem.id, sid))) or latex_of(common.get((problem.id, None)))
            if content:
                contents[tex_template.normalize_number(problem.number or '')] = content
        students.append((sid, contents))
    document = tex_template.build_document(parsed, students, label=pdf_export.KIND_LABELS['A'])
    label = request.args.get('classes') or (student_ids[0] if len(student_ids) == 1 else 'students')
    filename = f'個別解説_{label}.tex'
    response = current_app.response_class(document, mimetype='application/x-tex; charset=utf-8')
    from urllib.parse import quote
    response.headers['Content-Disposition'] = (f'attachment; filename="kobetsu_kaisetsu.tex"; '
                                               f"filename*=UTF-8''{quote(filename)}")
    return response


# ==================== 生徒ごとの PDF（解答解説A: 個別化，解答解説B: マスター） ====================

def _evidence_settings():
    return (int(current_app.config.get('PERSONALIZATION_MIN_EVIDENCE') or pdf_export.MIN_EVIDENCE),
            float(current_app.config.get('PERSONALIZATION_MIN_RATIO') or pdf_export.MIN_EVIDENCED_RATIO))


@learning_bp.route('/exams/<exam_id>/pdf-exports', methods=['POST'])
@login_required
def start_pdf_export(exam_id):
    """選んだ生徒（クラス）の解答解説A・B を PDF にする。body: {classes?: [...], student_ids?: [...]}

    ファイル名は「1年1組20番_解答解説A.pdf」（個別化），「…_解答解説B.pdf」（マスター）。根拠となる演習の記録が
    少なく個別化しているとは言い難い生徒の A は「…_解答解説A※.pdf」。
    """
    exam = _get_exam(exam_id)
    if exam is None:
        return make_error_response('LEARNING_EXAM_NOT_FOUND', 'Exam not found', 404)
    if not exam.template_tex:
        return make_error_response('LEARNING_TEMPLATE_MISSING', 'マスター解説の TeX（テンプレート）を先に読み込んでください', 400)
    payload = request.get_json(silent=True) or {}
    try:
        student_ids, known = _select_students(payload)
    except ValueError as exc:
        return make_error_response('LEARNING_INVALID_STUDENTS', str(exc), 400)
    student_ids = [sid for sid in student_ids if sid in known]
    if not student_ids:
        return make_error_response('LEARNING_INVALID_STUDENTS', '対象の生徒がいません', 400)
    running = [j for j in PdfExportJob.query.filter_by(exam_id=exam.id, teacher_id=_teacher_db_id()).all()
               if pdf_export.is_running(j.id)]
    if running:
        return make_error_response('LEARNING_JOB_RUNNING', 'PDF を作成中です。終わってから実行してください', 409)
    classes = payload.get('classes') if isinstance(payload.get('classes'), list) else []
    job = PdfExportJob(teacher_id=_teacher_db_id(), exam_id=exam.id, status='queued',
                       request=json.dumps({'student_ids': student_ids, 'classes': classes}, ensure_ascii=False),
                       total=len(student_ids) * 2, done=0, files='[]', errors='[]')
    db.session.add(job)
    db.session.commit()
    pdf_export.start(current_app._get_current_object(), job.id)
    db.session.expire_all()
    return make_success_response({'job': pdf_export.job_to_dict(db.session.get(PdfExportJob, job.id))}, 202)


@learning_bp.route('/exams/<exam_id>/pdf-exports', methods=['GET'])
@login_required
def list_pdf_exports(exam_id):
    jobs = (PdfExportJob.query.filter_by(exam_id=exam_id, teacher_id=_teacher_db_id())
            .order_by(PdfExportJob.created_at.desc()).limit(5).all())
    return make_success_response({'jobs': [pdf_export.job_to_dict(j) for j in jobs]})


def _get_pdf_job(job_id):
    return PdfExportJob.query.filter_by(id=job_id, teacher_id=_teacher_db_id()).first()


@learning_bp.route('/pdf-exports/<job_id>', methods=['GET'])
@login_required
def get_pdf_export(job_id):
    job = _get_pdf_job(job_id)
    if job is None:
        return make_error_response('LEARNING_JOB_NOT_FOUND', 'Job not found', 404)
    return make_success_response({'job': pdf_export.job_to_dict(job)})


def _attachment(path, filename, mimetype, ascii_name):
    from urllib.parse import quote
    response = send_file(path, mimetype=mimetype)
    response.headers['Content-Disposition'] = (f'attachment; filename="{ascii_name}"; '
                                               f"filename*=UTF-8''{quote(filename)}")
    return response


@learning_bp.route('/pdf-exports/<job_id>/files/<int:index>', methods=['GET'])
@login_required
def download_pdf_file(job_id, index):
    job = _get_pdf_job(job_id)
    files = json.loads(job.files or '[]') if job else []
    if job is None or not 0 <= index < len(files) or not files[index].get('path') \
            or not os.path.exists(files[index]['path']):
        return make_error_response('LEARNING_FILE_NOT_FOUND', 'File not found', 404)
    entry = files[index]
    ascii_name = f'{entry["student_id"]}_{entry["kind"]}{"_low" if entry.get("insufficient") else ""}.pdf'
    return _attachment(entry['path'], entry['filename'], 'application/pdf', re.sub(r'[^A-Za-z0-9._-]', '_', ascii_name))


@learning_bp.route('/pdf-exports/<job_id>/zip', methods=['GET'])
@login_required
def download_pdf_zip(job_id):
    """書き出した PDF をクラス（選んだ生徒）ごとに1つの ZIP でダウンロードする。?kind=A / B で片方だけにできる。"""
    job = _get_pdf_job(job_id)
    if job is None:
        return make_error_response('LEARNING_JOB_NOT_FOUND', 'Job not found', 404)
    files = [f for f in json.loads(job.files or '[]') if f.get('path')]
    kind = request.args.get('kind')
    if kind in ('A', 'B'):
        files = [f for f in files if f['kind'] == kind]
    if not files:
        return make_error_response('LEARNING_FILE_NOT_FOUND', 'ダウンロードできる PDF がありません', 404)
    directory = pdf_export.export_dir(current_app.config.get('UPLOAD_FOLDER') or 'uploads', job.exam_id)
    os.makedirs(directory, exist_ok=True)
    target = pdf_export.zip_files(files, os.path.join(directory, f'{job.id}{"_" + kind if kind else ""}.zip'))
    classes = json.loads(job.request or '{}').get('classes') or []
    label = '・'.join(f'{c.split("-")[0]}年{c.split("-")[1]}組' for c in classes if re.match(r'^\d+-\d+$', c)) or '解答解説'
    return _attachment(target, f'{label}_解答解説{kind or ""}.zip', 'application/zip',
                       f'{"_".join(classes) or "students"}_kaisetsu{kind or ""}.zip')


# ==================== 要確認の解説だけを直す ====================

@learning_bp.route('/exams/<exam_id>/repair-plan', methods=['GET'])
@login_required
def exam_repair_plan(exam_id):
    """要確認の解説を直す前の見積もり（LLM は呼ばない）: 再検算だけで直る件数と，書き直しが必要なステップ数。"""
    exam = _get_exam(exam_id)
    if exam is None:
        return make_error_response('LEARNING_EXAM_NOT_FOUND', 'Exam not found', 404)
    try:
        student_ids, known = _select_students(request.args)
    except ValueError as exc:
        return make_error_response('LEARNING_INVALID_STUDENTS', str(exc), 400)
    student_ids = [sid for sid in student_ids if sid in known]
    return make_success_response({'plan': exam_generation.repair_plan(_exam_problems(exam.id), student_ids)})


@learning_bp.route('/exams/<exam_id>/repair-jobs', methods=['POST'])
@login_required
def start_exam_repair(exam_id):
    """要確認の解説だけを直す。body: {classes?: [...], student_ids?: [...]}

    まず記録に残っている計算を現在の検算でもう一度確かめ（LLM を呼ばない），それでも通らないステップだけを書き直す。
    同じ解説を使い回した生徒はまとめて1回だけ直す。教員が確認済みにした解説は対象外。
    """
    exam = _get_exam(exam_id)
    if exam is None:
        return make_error_response('LEARNING_EXAM_NOT_FOUND', 'Exam not found', 404)
    payload = request.get_json(silent=True) or {}
    try:
        student_ids, known = _select_students(payload)
    except ValueError as exc:
        return make_error_response('LEARNING_INVALID_STUDENTS', str(exc), 400)
    student_ids = [sid for sid in student_ids if sid in known]
    problems = _exam_problems(exam.id)
    groups = exam_generation.review_groups(problems, student_ids)
    if not groups:
        return make_error_response('LEARNING_NOTHING_TO_REPAIR', '要確認で未確認の解説はありません', 400)
    running = [j for j in ExamGenerationJob.query.filter_by(exam_id=exam.id, teacher_id=_teacher_db_id()).all()
               if exam_generation.is_running(j.id)]
    if running:
        return make_error_response('LEARNING_JOB_RUNNING', 'この試験の解説を生成中です。終わってから実行してください', 409)
    try:
        get_llm_client(required=True)
    except LLMUnavailable as exc:
        return _llm_unavailable(exc)
    job = ExamGenerationJob(
        teacher_id=_teacher_db_id(), exam_id=exam.id, status='queued',
        request=json.dumps({'mode': 'repair', 'student_ids': student_ids, 'problem_ids': [p.id for p in problems]},
                           ensure_ascii=False),
        total=len(groups), done=0, errors='[]',
    )
    db.session.add(job)
    db.session.commit()
    exam_generation.start(current_app._get_current_object(), job.id)
    db.session.expire_all()
    return make_success_response({'job': exam_generation.job_to_dict(db.session.get(ExamGenerationJob, job.id))}, 202)
