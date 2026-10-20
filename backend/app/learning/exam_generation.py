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
from app.learning import tex_template
from app.utils import diagnostics

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
        'template': tex_template.fingerprint(problem_input.get('template')),
    }
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def master_signature(problem_input: Dict[str, Any], model_name: Optional[str]) -> str:
    decisions = [{'step_id': s['id'], **chain.master_decision(s)} for s in problem_input['steps']]
    return signature(problem_input, decisions, model_name, kind='master')


def common_signature(problem_input: Dict[str, Any], model_name: Optional[str]) -> str:
    payload = {'kind': 'common', 'problem_text': problem_input['problem_text'],
               'correct_answer': problem_input.get('correct_answer'), 'model': model_name,
               'max_attempts': chain.MAX_ATTEMPTS, 'style': chain.COMMON_STYLE_VERSION,
               'template': tex_template.fingerprint(problem_input.get('template'))}
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
            'llm_calls': generate * (0 if problem_input.get('template') else 2), 'common': True}


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
        except Exception as exc:  # 予期しない例外も端末に残す（黙って止まらないように）
            diagnostics.log(f'ジョブ {job_id} が例外で終了しました: {exc.__class__.__name__}: {exc}')
            raise
        finally:
            with _lock:
                _running.discard(job_id)

    if app.config.get('LEARNING_JOBS_INLINE'):
        run()
    else:
        threading.Thread(target=run, daemon=True, name=f'job-{job_id[:8]}').start()
        diagnostics.watch_job(lambda: _progress(app, job_id), lambda: is_running(job_id))
    return True


def _progress(app, job_id: str):
    from app import db
    from app.models import ExamGenerationJob

    with app.app_context():
        job = db.session.get(ExamGenerationJob, job_id)
        result = (job.done, job.current) if job else None
        db.session.remove()
        return result


def _latest(rows):
    latest = {}
    for row in sorted(rows, key=lambda r: r.created_at):
        latest[(row.kind, row.student_id)] = row
    return latest


def _row_result(row) -> Dict[str, Any]:
    trace = json.loads(row.trace or '{}')
    return {'kind': row.kind, 'status': row.status, 'content': row.content, 'steps': trace.get('steps', []),
            'warnings': trace.get('warnings', []), 'model_name': row.model_name,
            'format': trace.get('format', 'markdown')}


def _save(job, problem_id: str, student_id: Optional[str], result: Dict[str, Any], snapshot, sig: Optional[str],
          reused_from: Optional[str] = None, reviewed: bool = False):
    from app import db
    from app.models import GeneratedExplanation

    trace = {'steps': result['steps'], 'warnings': result['warnings'], 'format': result.get('format', 'markdown')}
    row = GeneratedExplanation(
        teacher_id=job.teacher_id, problem_id=problem_id, student_id=student_id, kind=result['kind'],
        status=result['status'], content=result['content'], trace=json.dumps(trace, ensure_ascii=False),
        profile_snapshot=json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None,
        model_name=result.get('model_name'), signature=sig, reused_from=reused_from, reviewed=reviewed,
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
    if request.get('mode') == 'repair':
        _run_repair(job, request, llm, errors)
        return
    if request.get('mode') == 'polish':
        _run_polish(job, request, llm, errors)
        return

    profiles = {}
    for student_id in request.get('student_ids', []):
        profile = LearnerProfile.query.filter_by(teacher_id=job.teacher_id, student_id=student_id).first()
        if profile is None:
            errors.append({'student_id': student_id, 'error': '学習者プロファイルがありません'})
        else:
            profiles[student_id] = student_states(profile)

    paused = None
    tasks: List[Dict[str, Any]] = []
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
                        common = (chain.template_common(problem_input) if problem_input.get('template')
                                  else chain.generate_common_explanation(llm, problem_input))
                        _save(job, problem.id, None, common, None, sig)
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

        # マスター解説（比較用）
        if request.get('include_master'):
            master_sig = master_signature(problem_input, model_name)
            existing = latest.get(('master', None))
            if existing is None or existing.signature != master_sig:
                try:
                    result = chain.generate_explanation(llm, problem_input, kind='master')
                    _save(job, problem.id, None, result, None, master_sig)
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

        # 後でまとめて並行に生成するので，この問題の plans と problem_id を引数の既定値で固定する
        def save_for(student_id, source_row, result, reused, plans=plans, problem_id=problem.id):
            decisions, sig, snapshot = plans[student_id]
            own = with_own_decisions(result, decisions)
            row = _save(job, problem_id, student_id, own, snapshot, sig,
                        reused_from=source_row.id if reused and source_row is not None else None)
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

        for students in pending.values():
            tasks.append({'problem': problem, 'label': label, 'input': problem_input, 'students': students,
                          'save_for': save_for})

    # 生成が必要な解説を，問題をまたいで並行に作る（1問に生成が1〜2件しかなくても，同時に動ける数を使い切る）
    if tasks and not paused:
        paused = _generate_tasks(job, tasks, profiles, llm, workers, errors)
    _finish(job, errors, paused)


def _generate_tasks(job, tasks: List[Dict[str, Any]], profiles, llm, workers: int,
                    errors: List[Dict[str, Any]]) -> Optional[str]:
    """使い回せない解説（指紋ごとに1件）を並行に生成して保存する。利用上限に達したら残りを取り消し，その理由を返す。"""
    from app import db

    in_flight: Dict[Any, Dict[str, Any]] = {}
    paused = None

    def work(task):
        student_id = task['students'][0]
        diagnostics.label(f"{task['label']} {student_id}")
        try:
            node_states, tendencies = profiles[student_id]
            with diagnostics.timed('解説の生成（1件）', warn_after=600):
                return chain.generate_explanation(llm, task['input'], node_states, tendencies)
        finally:
            diagnostics.label(None)

    def show_current():
        labels = sorted({t['label'] for t in in_flight.values()}, key=number_key)
        if labels:
            job.current = '，'.join(labels[:4]) + (' ほか' if len(labels) > 4 else '')
        db.session.commit()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for task in tasks:
            future = pool.submit(work, task)
            futures[future] = task
        in_flight.update(futures)
        show_current()
        for future in as_completed(futures):
            task = in_flight.pop(future)
            students, label = task['students'], task['label']
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
                job.errors = json.dumps(errors, ensure_ascii=False)
                show_current()
                continue
            first = task['save_for'](students[0], None, result, False)
            job.generated += 1
            job.done += 1
            for student_id in students[1:]:
                task['save_for'](student_id, first, result, True)
                job.reused += 1
                job.done += 1
            job.errors = json.dumps(errors, ensure_ascii=False)
            show_current()
    return paused


# ---------------------------------------------------------------- 要確認の解説だけを直す

def review_groups(problems, student_ids: List[str]) -> List[Dict[str, Any]]:
    """要確認でまだ教員が確かめていない解説を，使い回しの元ごとにまとめる（同じ解説は1回だけ直す）。"""
    from app.models import GeneratedExplanation

    wanted = set(student_ids)
    groups: List[Dict[str, Any]] = []
    for problem in problems:
        rows = (GeneratedExplanation.query.filter(GeneratedExplanation.problem_id == problem.id,
                                                  GeneratedExplanation.kind.in_(('individual', 'common')))
                .order_by(GeneratedExplanation.created_at.asc()).all())
        latest: Dict[Any, Any] = {}
        for row in rows:
            latest[(row.kind, row.student_id)] = row
        common = latest.get(('common', None))
        if common is not None and common.status == 'needs_review' and not common.reviewed:
            groups.append({'problem': problem, 'source': common, 'members': [common]})
        by_root: Dict[str, List[Any]] = {}
        for (kind, sid), row in latest.items():
            if kind == 'individual' and sid in wanted and row.status == 'needs_review' and not row.reviewed:
                by_root.setdefault(row.reused_from or row.id, []).append(row)
        for root, members in by_root.items():
            members.sort(key=lambda r: number_key(r.student_id))
            source = next((m for m in members if m.id == root), members[0])
            groups.append({'problem': problem, 'source': source, 'members': members})
    return groups


def repair_plan(problems, student_ids: List[str]) -> Dict[str, Any]:
    """LLM を呼ばずに，再検算だけで直る解説と，書き直しが必要なステップを数える（ボタンを押す前の見積もり）。"""
    from app.learning.jobs import problem_to_chain_input

    counts = {'groups': 0, 'explanations': 0, 'fixed_by_recheck': 0, 'rewrite_groups': 0, 'rewrite_steps': 0,
              'cannot_repair': 0}
    for group in review_groups(problems, student_ids):
        counts['groups'] += 1
        counts['explanations'] += len(group['members'])
        problem_input = problem_to_chain_input(group['problem'])
        previous = _row_result(group['source'])
        try:
            if group['source'].kind == 'common':
                plan = chain.repair_common(None, problem_input, previous, dry_run=True)
            else:
                plan = chain.repair_explanation(None, problem_input, previous, dry_run=True)
        except chain.ExplanationError:
            counts['cannot_repair'] += 1
            continue
        if plan['rewrite_steps']:
            counts['rewrite_groups'] += 1
            counts['rewrite_steps'] += len(plan['rewrite_steps'])
        else:
            counts['fixed_by_recheck'] += 1
    # 書き直し1回 = 書く1回 + 計算の抜き出し1回（差し戻しが続けば最大 MAX_ATTEMPTS 倍）
    counts['llm_calls'] = 2 * counts['rewrite_steps']
    counts['llm_calls_max'] = 2 * chain.MAX_ATTEMPTS * counts['rewrite_steps']
    return counts


def _run_repair(job, request: Dict[str, Any], llm, errors: List[Dict[str, Any]]) -> None:
    """要確認の解説だけを直す。再検算で通るものは LLM を呼ばず，通らないステップだけを書き直す。

    使い回した解説（同じ判断の生徒）はまとめて1回だけ直し，生徒ごとの記録（判断の理由）はそれぞれのものにする。
    counters: generated = 書き直した解説，skipped = 再検算だけで直った解説，reused = 同じ直しを反映した生徒。
    """
    from app import db
    from app.learning.jobs import problem_to_chain_input
    from app.models import TestProblem

    problems = [p for p in (db.session.get(TestProblem, pid) for pid in request.get('problem_ids', [])) if p]
    groups = review_groups(problems, request.get('student_ids', []))
    job.total = len(groups)
    db.session.commit()
    paused = None
    for group in groups:
        problem, source = group['problem'], group['source']
        label = problem.number or problem.title
        job.current = f'{label}（{problem.title}）'
        db.session.commit()
        problem_input = problem_to_chain_input(problem)
        try:
            if source.kind == 'common':
                repaired = chain.repair_common(llm, problem_input, _row_result(source))
            else:
                repaired = chain.repair_explanation(llm, problem_input, _row_result(source))
        except Exception as exc:
            db.session.rollback()
            if QUOTA_PATTERN.search(str(exc)):
                paused = str(exc)
                break
            errors.append({'problem': label, 'student_id': source.student_id, 'error': f'{exc.__class__.__name__}: {exc}'})
            job.done += 1
            job.errors = json.dumps(errors, ensure_ascii=False)
            db.session.commit()
            continue
        result = repaired['result']
        first = None
        for member in group['members']:
            own = result
            if member.kind == 'individual':
                own_decisions = [s['decision'] for s in json.loads(member.trace or '{}').get('steps', [])]
                own = with_own_decisions(result, own_decisions)
            snapshot = json.loads(member.profile_snapshot) if member.profile_snapshot else None
            row = _save(job, problem.id, member.student_id, own, snapshot, member.signature,
                        reused_from=first.id if first is not None else None)
            first = first or row
        if repaired['rewrite_steps']:
            job.generated += 1
        else:
            job.skipped += 1
        job.reused += len(group['members']) - 1
        job.done += 1
        job.errors = json.dumps(errors, ensure_ascii=False)
        db.session.commit()
    _finish(job, errors, paused)


# ---------------------------------------------------------------- 文章を整える（文体・誤字・注意の重複）

def _earlier_cautions(problem, student_id: Optional[str], latest_by_problem: Dict[str, Dict[str, Any]],
                      siblings: List[Any]) -> List[str]:
    """同じ大問の前の小問で，この生徒の解説に書いた注意（テンプレートの注意を含む）。小問をまたぐ重複を見つけるため。"""
    from app.learning import polish as polish_module

    template = json.loads(problem.template) if problem.template else None
    daimon = (template or {}).get('daimon')
    found: List[str] = []
    for other in siblings:
        if other.id == problem.id:
            break
        other_template = json.loads(other.template) if other.template else None
        if not other_template or other_template.get('daimon') != daimon:
            continue
        found.extend(polish_module.template_cautions(other_template))
        row = latest_by_problem.get(other.id, {}).get(student_id)
        if row is not None:
            found.extend(polish_module.cautions(row.content or ''))
    return found


def polish_groups(problems, student_ids: List[str], dry_run: bool = True) -> List[Dict[str, Any]]:
    """テンプレートで個別化した最新の解説のうち，整える必要があるもの（使い回しの元ごとに1件）。"""
    from app.learning import polish as polish_module
    from app.learning.jobs import problem_to_chain_input
    from app.models import GeneratedExplanation

    wanted = set(student_ids)
    templated = sorted([p for p in problems if p.template], key=lambda p: number_key(p.number))
    latest_by_problem: Dict[str, Dict[str, Any]] = {}
    for problem in templated:
        rows = (GeneratedExplanation.query.filter_by(problem_id=problem.id, kind='individual')
                .order_by(GeneratedExplanation.created_at.asc()).all())
        latest_by_problem[problem.id] = {r.student_id: r for r in rows if r.student_id in wanted}
    groups = []
    for problem in templated:
        by_root: Dict[str, List[Any]] = {}
        for row in latest_by_problem[problem.id].values():
            # 教員が本文を直した解説は変えない（確認済みにしただけの解説は，計算を変えずに文章だけ整える）
            if row.original_content is None:
                by_root.setdefault(row.reused_from or row.id, []).append(row)
        problem_input = problem_to_chain_input(problem)
        for root, members in by_root.items():
            members.sort(key=lambda r: number_key(r.student_id))
            source = next((m for m in members if m.id == root), members[0])
            earlier = _earlier_cautions(problem, source.student_id, latest_by_problem, templated)
            plan = polish_module.polish(None, problem_input, _row_result(source), earlier, dry_run=True)
            if plan['needs_llm'] or plan['fixed_typos']:
                groups.append({'problem': problem, 'source': source, 'members': members, 'earlier': earlier,
                               'needs_llm': plan['needs_llm']})
    return groups


def polish_plan(problems, student_ids: List[str]) -> Dict[str, Any]:
    from app.learning import polish as polish_module

    groups = polish_groups(problems, student_ids)
    llm_groups = sum(1 for g in groups if g['needs_llm'])
    return {'groups': len(groups), 'explanations': sum(len(g['members']) for g in groups),
            'llm_groups': llm_groups, 'typo_only': len(groups) - llm_groups, 'llm_calls': llm_groups,
            'llm_calls_max': llm_groups * polish_module.MAX_POLISH_CALLS}


def _run_polish(job, request: Dict[str, Any], llm, errors: List[Dict[str, Any]]) -> None:
    """文章を整える。誤字・重複した注意だけなら LLM を呼ばずに直し，敬体・長い注意がある解説は1件につき1回（直しきれなければ2回）呼ぶ。

    counters: generated = LLM で整えた解説，skipped = 誤字だけプログラムで直した解説，reused = 同じ直しを反映した生徒。
    """
    from app import db
    from app.learning import polish as polish_module
    from app.learning.jobs import problem_to_chain_input
    from app.models import TestProblem

    problems = [p for p in (db.session.get(TestProblem, pid) for pid in request.get('problem_ids', [])) if p]
    groups = polish_groups(problems, request.get('student_ids', []))
    job.total = len(groups)
    db.session.commit()
    paused = None
    for group in groups:
        problem, source = group['problem'], group['source']
        label = problem.number or problem.title
        job.current = f'{label}（{problem.title}）'
        db.session.commit()
        try:
            polished = polish_module.polish(llm, problem_to_chain_input(problem), _row_result(source), group['earlier'])
        except Exception as exc:
            db.session.rollback()
            if QUOTA_PATTERN.search(str(exc)):
                paused = str(exc)
                break
            errors.append({'problem': label, 'student_id': source.student_id, 'error': f'{exc.__class__.__name__}: {exc}'})
            job.done += 1
            job.errors = json.dumps(errors, ensure_ascii=False)
            db.session.commit()
            continue
        result = polished['result']
        if result is not None:
            first = None
            for member in group['members']:
                own_decisions = [s['decision'] for s in json.loads(member.trace or '{}').get('steps', [])]
                own = with_own_decisions(result, own_decisions)
                snapshot = json.loads(member.profile_snapshot) if member.profile_snapshot else None
                row = _save(job, problem.id, member.student_id, own, snapshot, member.signature,
                            reused_from=first.id if first is not None else None, reviewed=bool(member.reviewed))
                first = first or row
            if polished.get('llm_called'):
                job.generated += 1
            else:
                job.skipped += 1
            job.reused += len(group['members']) - 1
        job.done += 1
        job.errors = json.dumps(errors, ensure_ascii=False)
        db.session.commit()
    _finish(job, errors, paused)


def _finish(job, errors: List[Dict[str, Any]], paused: Optional[str]) -> None:
    from app import db

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
        'inflight': diagnostics.inflight() if is_running(job.id) else [],
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'updated_at': job.updated_at.isoformat() if job.updated_at else None,
    }
