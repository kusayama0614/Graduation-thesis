"""解説生成ジョブの実行（スレッドで非同期実行。テストでは LEARNING_JOBS_INLINE で同期実行）"""
from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

from flask import current_app

from app import db
from app.learning import explanation as chain
from app.learning.llm import LLMClient, get_llm_client
from app.models import GeneratedExplanation, GenerationJob, LearnerProfile, TestProblem


def problem_to_chain_input(problem: TestProblem) -> Dict[str, Any]:
    return {
        'id': problem.id,
        'unit': problem.unit,
        'title': problem.title,
        'problem_text': problem.problem_text,
        'correct_answer': problem.correct_answer,
        'steps': json.loads(problem.steps or '[]'),
        'template': json.loads(problem.template) if problem.template else None,
    }


def _save(teacher_db_id: str, problem_id: str, student_id: Optional[str], result: Dict[str, Any],
          snapshot: Optional[Dict[str, Any]]) -> GeneratedExplanation:
    row = GeneratedExplanation(
        teacher_id=teacher_db_id,
        problem_id=problem_id,
        student_id=student_id,
        kind=result['kind'],
        status=result['status'],
        content=result['content'],
        trace=json.dumps({'steps': result['steps'], 'warnings': result['warnings'],
                          'format': result.get('format', 'markdown')}, ensure_ascii=False),
        profile_snapshot=json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None,
        model_name=result.get('model_name'),
    )
    db.session.add(row)
    db.session.commit()
    return row


def run_job(job_id: str, llm: LLMClient) -> None:
    job = db.session.get(GenerationJob, job_id)
    problem = db.session.get(TestProblem, job.problem_id)
    request = json.loads(job.request or '{}')
    problem_input = problem_to_chain_input(problem)
    explanation_ids: List[str] = []
    errors: List[Dict[str, Any]] = []
    job.status = 'running'
    db.session.commit()

    def progress():
        job.done += 1
        job.explanation_ids = json.dumps(explanation_ids)
        job.errors = json.dumps(errors, ensure_ascii=False)
        db.session.commit()

    master = None
    if request.get('include_master'):
        try:
            master = chain.generate_explanation(llm, problem_input, kind='master')
            explanation_ids.append(_save(job.teacher_id, problem.id, None, master, None).id)
        except Exception as exc:
            db.session.rollback()
            errors.append({'student_id': None, 'kind': 'master', 'error': f'{exc.__class__.__name__}: {exc}'})
        progress()

    for student_id in request.get('student_ids', []):
        profile = LearnerProfile.query.filter_by(teacher_id=job.teacher_id, student_id=student_id).first()
        if profile is None:
            errors.append({'student_id': student_id, 'kind': 'individual', 'error': '学習者プロファイルがありません'})
            progress()
            continue
        node_states = json.loads(profile.node_states or '{}')
        tendencies = json.loads(profile.error_tendencies or '{}')
        snapshot = {'node_states': node_states, 'error_tendencies': tendencies}
        try:
            result = chain.generate_explanation(llm, problem_input, node_states, tendencies)
            explanation_ids.append(_save(job.teacher_id, problem.id, student_id, result, snapshot).id)
        except Exception as exc:
            db.session.rollback()
            errors.append({'student_id': student_id, 'kind': 'individual', 'error': f'{exc.__class__.__name__}: {exc}'})
        progress()

    # 一人でも生成できれば completed（失敗した生徒は errors に残る）。誰も生成できなければ failed
    job.status = 'completed' if explanation_ids or not errors else 'failed'
    job.explanation_ids = json.dumps(explanation_ids)
    job.errors = json.dumps(errors, ensure_ascii=False)
    db.session.commit()


def start_job(job: GenerationJob) -> None:
    app = current_app._get_current_object()
    llm = get_llm_client(required=True)
    if app.config.get('LEARNING_JOBS_INLINE'):
        run_job(job.id, llm)
        return

    def target():
        with app.app_context():
            try:
                run_job(job.id, llm)
            except Exception as exc:
                db.session.rollback()
                failed = db.session.get(GenerationJob, job.id)
                if failed:
                    failed.status = 'failed'
                    failed.errors = json.dumps([{'error': f'{exc.__class__.__name__}: {exc}'}], ensure_ascii=False)
                    db.session.commit()

    threading.Thread(target=target, daemon=True).start()


def job_to_dict(job: GenerationJob) -> Dict[str, Any]:
    return {
        'id': job.id,
        'problem_id': job.problem_id,
        'status': job.status,
        'request': json.loads(job.request or '{}'),
        'total': job.total or 0,
        'done': job.done or 0,
        'explanation_ids': json.loads(job.explanation_ids or '[]'),
        'errors': json.loads(job.errors or '[]'),
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'updated_at': job.updated_at.isoformat() if job.updated_at else None,
    }
