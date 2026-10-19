"""定期試験の全問題 × 生徒の解説をまとめて生成する（卒業論文 3.1節「試験の返却と同時に全員へ解説を配布」）

1問 × 1人ずつ Chain 2〜4 を実行すると，Gemini の呼び出しが試験全体で数千〜数万回になる。そこで，
Chain 2 の判断（ステップごとに含める要素と，添える誤りへの注意）が同じ生徒には，LLM に渡す指示も
同じになることを利用する。問題・ステップ・判断・モデルから「指紋」（signature）を作り，

- 生徒の判断が前回の生成から変わっていなければ，既にある解説をそのまま使う（skipped）
- 同じ指紋の解説が既にあれば，それを使い回す（reused。判断の理由はその生徒自身のものに置き換える）
- どちらもなければ，指紋ごとに1回だけ生成する（generated）

解答ステップがない問題（Chain 1 で分解できなかった問題）は判断ができないため，個別化しない全員共通の解説
（kind='common'）を1問につき1件だけ生成し，全員に配る。

生成は並列数を絞ったスレッドで行い，データベースへの書き込みは1件ずつ確定する。Gemini の利用上限に
達したら一時停止し（paused），再開すると続きから生成する（済んだものは指紋が同じなので省略される）。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from app.learning import explanation as chain

QUOTA_PATTERN = re.compile(r'RESOURCE_EXHAUSTED|\b429\b|quota', re.IGNORECASE)


# ---------------------------------------------------------------- 指紋（同じなら LLM への指示が同じ）

def _decision_key(decision: Dict[str, Any]) -> Dict[str, Any]:
    """LLM への指示に入る部分だけを取り出す（定着度の数値や理由の文は指示に入らない）。"""
    return {
        'elements': list(decision.get('elements', [])),
        'cautions': sorted((c['node'], int(c['error_count'])) for c in decision.get('cautions', [])),
    }


def signature(problem_input: Dict[str, Any], decisions: List[Dict[str, Any]], model_name: Optional[str],
              kind: str = 'individual') -> str:
    payload = {
        'kind': kind,
        'problem_text': problem_input['problem_text'],
        'correct_answer': problem_input.get('correct_answer'),
        'steps': problem_input['steps'],
        'decisions': [_decision_key(d) for d in decisions],
        'model': model_name,
        'max_attempts': chain.MAX_ATTEMPTS,
    }
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def master_signature(problem_input: Dict[str, Any], model_name: Optional[str]) -> str:
    decisions = [{'step_id': s['id'], **chain.master_decision(s)} for s in problem_input['steps']]
    return signature(problem_input, decisions, model_name, kind='master')


def common_signature(problem_input: Dict[str, Any], model_name: Optional[str]) -> str:
    payload = {'kind': 'common', 'problem_text': problem_input['problem_text'],
               'correct_answer': problem_input.get('correct_answer'), 'model': model_name,
               'max_attempts': chain.MAX_ATTEMPTS}
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def with_own_decisions(result: Dict[str, Any], decisions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """使い回す解説の記録の「判断と理由」を，その生徒自身の定着度による理由に置き換える。"""
    result = copy.deepcopy(result)
    by_step = {d['step_id']: d for d in decisions}
    for step in result.get('steps', []):
        if step.get('step_id') in by_step:
            step['decision'] = by_step[step['step_id']]
    return result


# ---------------------------------------------------------------- 見積もり

def student_states(profile) -> Tuple[Dict[str, Any], Dict[str, int]]:
    return json.loads(profile.node_states or '{}'), json.loads(profile.error_tendencies or '{}')


def estimate_calls(steps: List[Dict[str, Any]]) -> int:
    """1件の解説の生成で Gemini を呼ぶ回数の目安（ステップごとに書く1回と抜き出す1回。差し戻しは含めない）。"""
    return 2 * max(1, len(steps))


def number_key(text: Optional[str]) -> Tuple:
    """「3(2)」「12」「1-1-20」などを数の順に並べるための鍵。"""
    return tuple(int(n) for n in re.findall(r'\d+', text or '')) or (10 ** 9,)


def class_of(student_id: str) -> Optional[str]:
    """生徒ID「学年-組-番」の「学年-組」。"""
    parts = student_id.split('-')
    return '-'.join(parts[:2]) if len(parts) >= 3 else None


def plan_common(problem_input: Dict[str, Any], student_count: int, model_name: Optional[str], existing_rows) -> Dict[str, Any]:
    """解答ステップがない問題: 全員共通の解説が（同じ指紋で）既にあれば省略，なければ1件だけ生成する。"""
    latest = _latest(existing_rows).get(('common', None))
    done = latest is not None and latest.signature == common_signature(problem_input, model_name)
    generate = 0 if done or not student_count else 1
    return {'students': student_count, 'distinct_plans': 1, 'generate': generate,
            'reuse': 0 if done else max(0, student_count - generate), 'skip': student_count if done else 0,
            'llm_calls': generate * 2, 'common': True}


def plan_problem(problem_input: Dict[str, Any], profiles: Dict[str, Tuple[Dict, Dict]], model_name: Optional[str],
                 existing_rows, reuse: bool = True) -> Dict[str, Any]:
    """1問について，生成が必要な件数（指紋の種類）・使い回せる件数・省略できる件数を LLM を呼ばずに数える。"""
    latest = _latest(existing_rows)
    known = {r.signature for r in existing_rows
             if r.kind == 'individual' and r.signature and not r.reused_from}
    to_generate, skippable = set(), 0
    groups: Dict[str, int] = {}
    for student_id, (node_states, tendencies) in profiles.items():
        sig = signature(problem_input, chain.plan_explanation(problem_input['steps'], node_states, tendencies),
                        model_name)
        groups[sig] = groups.get(sig, 0) + 1
        current = latest.get(('individual', student_id))
        if current is not None and current.signature == sig:
            skippable += 1
        elif not (reuse and sig in known):
            to_generate.add(sig if reuse else f'{sig}:{student_id}')
    generate = len(to_generate)
    return {'students': len(profiles), 'distinct_plans': len(groups), 'generate': generate,
            'reuse': len(profiles) - skippable - generate, 'skip': skippable,
            'llm_calls': generate * estimate_calls(problem_input['steps'])}


# ---------------------------------------------------------------- 実行

_running: set = set()
_lock = threading.Lock()


def is_running(job_id: str) -> bool:
    with _lock:
        return job_id in _running


def start(app, job_id: str) -> bool:
    with _lock:
        if job_id in _running:
            return False
        _running.add(job_id)

    def run():
        try:
            with app.app_context():
                _run(app, job_id)
        finally:
            with _lock:
                _running.discard(job_id)

    if app.config.get('LEARNING_JOBS_INLINE'):
        run()
    else:
        threading.Thread(target=run, daemon=True).start()
    return True


def _latest(rows):
    latest = {}
    for row in sorted(rows, key=lambda r: r.created_at):
        latest[(row.kind, row.student_id)] = row
    return latest


def _row_result(row) -> Dict[str, Any]:
    trace = json.loads(row.trace or '{}')
    return {'kind': row.kind, 'status': row.status, 'content': row.content, 'steps': trace.get('steps', []),
            'warnings': trace.get('warnings', []), 'model_name': row.model_name}


def _save(job, problem_id: str, student_id: Optional[str], result: Dict[str, Any], snapshot, sig: Optional[str],
          reused_from: Optional[str] = None, extra_trace: Optional[Dict[str, Any]] = None):
    from app import db
    from app.models import GeneratedExplanation

    trace = {'steps': result['steps'], 'warnings': result['warnings'], **(extra_trace or {})}
    row = GeneratedExplanation(
        teacher_id=job.teacher_id, problem_id=problem_id, student_id=student_id, kind=result['kind'],
        status=result['status'], content=result['content'], trace=json.dumps(trace, ensure_ascii=False),
        profile_snapshot=json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None,
        model_name=result.get('model_name'), signature=sig, reused_from=reused_from,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _run(app, job_id: str) -> None:
    from app import db
    from app.learning.jobs import problem_to_chain_input
    from app.learning.llm import get_llm_client
    from app.models import ExamGenerationJob, GeneratedExplanation, LearnerProfile, TestProblem

    job = db.session.get(ExamGenerationJob, job_id)
    if job is None:
        return
    request = json.loads(job.request or '{}')
    errors: List[Dict[str, Any]] = []
    job.status, job.done, job.generated, job.reused, job.skipped, job.message = 'running', 0, 0, 0, 0, None
    db.session.commit()
    try:
        llm = get_llm_client(required=True)
    except Exception as exc:
        job.status, job.message = 'failed', str(exc)
        db.session.commit()
        return
    model_name = getattr(llm, 'model_name', None)
    workers = max(1, int(app.config.get('EXPLANATION_CONCURRENCY') or os.getenv('EXPLANATION_CONCURRENCY', 3)))
    reuse = request.get('reuse', True)

    profiles = {}
    for student_id in request.get('student_ids', []):
        profile = LearnerProfile.query.filter_by(teacher_id=job.teacher_id, student_id=student_id).first()
        if profile is None:
            errors.append({'student_id': student_id, 'error': '学習者プロファイルがありません'})
        else:
            profiles[student_id] = student_states(profile)

    paused = None
    for problem_id in request.get('problem_ids', []):
        if paused:
            break
        problem = db.session.get(TestProblem, problem_id)
        if problem is None:
            continue
        problem_input = problem_to_chain_input(problem)
        label = problem.number or problem.title
        job.current = f'{label}（{problem.title}）'
        db.session.commit()
        latest = _latest(GeneratedExplanation.query.filter_by(problem_id=problem.id).all())

        if not problem_input['steps']:
            # 解答ステップがない問題: 全員共通の解説を1件だけ作る（指紋が同じなら作り直さない）
            if problem.chain1_status == 'pending':
                errors.append({'problem': label, 'error': 'Chain 1（解答ステップへの分解）の実行中です。終わってから生成してください'})
            else:
                sig = common_signature(problem_input, model_name)
                existing = latest.get(('common', None))
                if existing is not None and existing.signature == sig:
                    job.skipped += len(profiles)
                else:
                    try:
                        _save(job, problem.id, None, chain.generate_common_explanation(llm, problem_input), None, sig)
                        job.generated += 1
                        job.reused += max(0, len(profiles) - 1)
                    except Exception as exc:
                        db.session.rollback()
                        if QUOTA_PATTERN.search(str(exc)):
                            paused = str(exc)
                            break
                        errors.append({'problem': label, 'student_id': None,
                                       'error': f'全員共通の解説: {exc.__class__.__name__}: {exc}'})
            job.done += len(profiles)
            job.errors = json.dumps(errors, ensure_ascii=False)
            db.session.commit()
            continue

        # マスター解説（比較用。引き算型の元にもなる）
        master = None
        if request.get('include_master') or request.get('include_subtractive'):
            master_sig = master_signature(problem_input, model_name)
            existing = latest.get(('master', None))
            if existing is not None and existing.signature == master_sig:
                master = existing
            else:
                try:
                    result = chain.generate_explanation(llm, problem_input, kind='master')
                    master = _save(job, problem.id, None, result, None, master_sig)
                    job.generated += 1
                    db.session.commit()
                except Exception as exc:
                    db.session.rollback()
                    if QUOTA_PATTERN.search(str(exc)):
                        paused = str(exc)
                        break
                    errors.append({'problem': label, 'student_id': None, 'error': f'マスター解説: {exc.__class__.__name__}: {exc}'})

        # 生徒ごとの判断と指紋
        plans = {}
        for student_id, (node_states, tendencies) in profiles.items():
            decisions = chain.plan_explanation(problem_input['steps'], node_states, tendencies)
            plans[student_id] = (decisions, signature(problem_input, decisions, model_name),
                                 {'node_states': node_states, 'error_tendencies': tendencies})

        by_signature: Dict[str, Any] = {}
        for row in GeneratedExplanation.query.filter(GeneratedExplanation.problem_id == problem.id,
                                                     GeneratedExplanation.kind == 'individual',
                                                     GeneratedExplanation.signature.isnot(None),
                                                     GeneratedExplanation.reused_from.is_(None)).all():
            by_signature.setdefault(row.signature, row)

        def save_for(student_id, source_row, result, reused):
            decisions, sig, snapshot = plans[student_id]
            own = with_own_decisions(result, decisions)
            row = _save(job, problem.id, student_id, own, snapshot, sig,
                        reused_from=source_row.id if reused and source_row is not None else None)
            if master is not None and request.get('include_subtractive'):
                subtractive = chain.build_subtractive(problem_input, _row_result(master), *profiles[student_id])
                _save(job, problem.id, student_id, subtractive, snapshot, sig, extra_trace={'master_id': master.id})
            return row

        pending: Dict[str, List[str]] = {}
        for student_id, (decisions, sig, snapshot) in plans.items():
            current = latest.get(('individual', student_id))
            if current is not None and current.signature == sig:
                job.skipped += 1
                job.done += 1
                continue
            if reuse and sig in by_signature:
                source = by_signature[sig]
                save_for(student_id, source, _row_result(source), reused=True)
                job.reused += 1
                job.done += 1
                db.session.commit()
                continue
            if reuse:
                pending.setdefault(sig, []).append(student_id)
            else:
                pending.setdefault(f'{sig}:{student_id}', []).append(student_id)
        db.session.commit()

        def work(student_id):
            node_states, tendencies = profiles[student_id]
            return chain.generate_explanation(llm, problem_input, node_states, tendencies)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(work, students[0]): students for students in pending.values()}
            for future in as_completed(futures):
                students = futures[future]
                if future.cancelled():
                    continue
                try:
                    result = future.result()
                except Exception as exc:
                    db.session.rollback()
                    if QUOTA_PATTERN.search(str(exc)):
                        paused = str(exc)
                        for other in futures:
                            other.cancel()
                        continue
                    for student_id in students:
                        errors.append({'problem': label, 'student_id': student_id, 'error': f'{exc.__class__.__name__}: {exc}'})
                        job.done += 1
                    db.session.commit()
                    continue
                first = save_for(students[0], None, result, reused=False)
                job.generated += 1
                job.done += 1
                for student_id in students[1:]:
                    save_for(student_id, first, result, reused=True)
                    job.reused += 1
                    job.done += 1
                job.errors = json.dumps(errors, ensure_ascii=False)
                db.session.commit()

    job.errors = json.dumps(errors, ensure_ascii=False)
    job.current = None
    if paused:
        job.status = 'paused'
        wait = re.search(r'retry in ([0-9hms.]+)', paused)
        job.message = ('Gemini の利用上限に達したため一時停止しました。' + (f'{wait.group(1)} 後を目安に' if wait else '時間をおいて')
                       + '「続きから生成」を押してください（済んだ解説は作り直しません）。')
    else:
        job.status = 'completed'
    db.session.commit()


def job_to_dict(job) -> Dict[str, Any]:
    return {
        'id': job.id, 'exam_id': job.exam_id, 'status': job.status, 'running': is_running(job.id),
        'request': json.loads(job.request or '{}'), 'total': job.total or 0, 'done': job.done or 0,
        'generated': job.generated or 0, 'reused': job.reused or 0, 'skipped': job.skipped or 0,
        'current': job.current, 'message': job.message, 'errors': json.loads(job.errors or '[]'),
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'updated_at': job.updated_at.isoformat() if job.updated_at else None,
    }
