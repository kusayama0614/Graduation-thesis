"""生徒ごとの解答解説を PDF に書き出す（個別化した解説を「解答解説A」，マスター解説を「解答解説B」とする）。

- 1人につき2つの PDF（A: その生徒の個別化した解説，B: マスター解説）を，テンプレートの TeX と同じ体裁で作る。
  表題の枠・ファイル名は「解説A」「解説B」だけにし，どちらが個別化したものかは分からないようにする。
- ファイル名は「1年1組20番_解答解説A.pdf」。学習者プロファイルの根拠（演習で出題された数）が少なく，
  個別化しているとは言い難い生徒の A には，名前の最後に「※」を付ける（personalization_evidence）。
- コンパイルは xelatex など（設定 PDF_LATEX_ENGINE。既定は xelatex を探す）。同じ TeX の PDF は保存しておき作り直さない。
  クラス分は時間がかかるので，並列数を絞ったスレッドで実行し，1件ずつ進み具合を記録する。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.learning import tex_template

# 「データ数が少なく個別化されているとは言い難い」とみなす基準（設定で変えられる）
MIN_EVIDENCE = 3          # 前提知識ノードの根拠（演習で出題された小問の数）がこれ未満のノードは「根拠が少ない」
MIN_EVIDENCED_RATIO = 0.5  # 根拠の十分なステップ（依存する全ノードの根拠が MIN_EVIDENCE 以上）の割合がこれ未満なら ※

KIND_LABELS = {'A': '解説A', 'B': '解説B'}


class PdfError(RuntimeError):
    pass


# ---------------------------------------------------------------- ファイル名と ※ の判定

def file_stem(student_id: str, kind: str) -> str:
    """「1-1-20」→「1年1組20番_解答解説A」。この形でない生徒IDはそのまま使う。"""
    match = re.match(r'^(\d+)-(\d+)-(\d+)$', student_id or '')
    who = f'{match.group(1)}年{match.group(2)}組{match.group(3)}番' if match else re.sub(r'[\\/:*?"<>|]', '_', student_id)
    return f'{who}_解答解説{kind}'


def personalization_evidence(traces: List[Dict[str, Any]], min_evidence: int = MIN_EVIDENCE,
                             min_ratio: float = MIN_EVIDENCED_RATIO) -> Dict[str, Any]:
    """生徒の個別化した解説（試験の全問題）の判断が，どれだけ演習の記録に裏付けられているか。

    ステップごとに Chain 2 の判断に記録した各ノードの根拠（出題数）を見て，依存する全ノードの根拠が
    min_evidence 以上のステップを「根拠の十分なステップ」とする。その割合が min_ratio 未満なら insufficient。
    （記録のないノードは定着度の初期値 0.5 で判断されるため，個別化しているとは言い難い）
    """
    steps = evidenced = 0
    weak_nodes: Dict[str, int] = {}
    for trace in traces:
        for step in trace.get('steps', []):
            confidences = (step.get('decision') or {}).get('node_confidences') or {}
            if not confidences:
                continue
            steps += 1
            counts = {node: int(value.get('evidence_count') or 0) for node, value in confidences.items()}
            if all(count >= min_evidence for count in counts.values()):
                evidenced += 1
            for node, count in counts.items():
                if count < min_evidence:
                    weak_nodes[node] = count
    ratio = round(evidenced / steps, 3) if steps else None
    return {
        'steps': steps, 'evidenced_steps': evidenced, 'ratio': ratio,
        'insufficient': ratio is None or ratio < min_ratio,
        'low_evidence_nodes': weak_nodes,
        'rule': f'依存する前提知識の根拠（出題数）が {min_evidence} 問以上のステップが {int(min_ratio * 100)}% 未満なら ※',
    }


# ---------------------------------------------------------------- コンパイル

def find_engine(configured: Optional[str] = None) -> Optional[str]:
    """xelatex を探す（Mac の MacTeX は PATH に入っていないことがあるので，よくある場所も見る）。"""
    if configured:
        return configured if os.path.exists(configured) or shutil.which(configured) else None
    found = shutil.which('xelatex')
    if found:
        return found
    candidates = ['/Library/TeX/texbin/xelatex', '/usr/local/bin/xelatex', '/opt/homebrew/bin/xelatex']
    candidates += sorted(glob.glob('/usr/local/texlive/*/bin/*/xelatex'), reverse=True)
    return next((c for c in candidates if os.path.exists(c)), None)


def tex_error_summary(log: str) -> str:
    """TeX のログから，エラーの行（! で始まる）と，その続きの説明の行（fontspec の「見つからないフォント名」など）を取り出す。"""
    lines = log.splitlines()
    parts = []
    for index, line in enumerate(lines):
        if not line.startswith('!'):
            continue
        message = [line.strip()]
        for follow in lines[index + 1:index + 6]:
            text = follow.strip()
            if not text or text.startswith('See the') or text.startswith('Type') or text.startswith('l.'):
                break
            message.append(re.sub(r'^\(\w+\)\s*', '', text))
        parts.append(' '.join(message))
        if len(parts) >= 2:
            break
    summary = ' / '.join(parts)
    missing = re.search(r'The font "([^"]+)" cannot be\s*(?:\(\w+\)\s*)?found', log)
    if missing:
        summary += (f'（フォント「{missing.group(1)}」が Mac に入っていません。フォントをインストールしてから，'
                    'もう一度「PDF を作る」を押してください）')
    return summary


def compile_tex(tex: str, engine: str, timeout: int = 180) -> bytes:
    """TeX を一時フォルダでコンパイルして PDF のバイト列を返す。失敗したらログの末尾を添えて PdfError。"""
    with tempfile.TemporaryDirectory(prefix='kaisetsu_') as work:
        source = os.path.join(work, 'doc.tex')
        with open(source, 'w', encoding='utf-8') as f:
            f.write(tex)
        command = [engine, '-interaction=nonstopmode', '-halt-on-error', '-no-shell-escape', 'doc.tex']
        env = dict(os.environ)
        engine_dir = os.path.dirname(engine)
        if engine_dir:  # xdvipdfmx など同じ場所のコマンドを見つけられるように
            env['PATH'] = engine_dir + os.pathsep + env.get('PATH', '')
        try:
            result = subprocess.run(command, cwd=work, capture_output=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise PdfError(f'コンパイルが {timeout} 秒で終わりませんでした')
        pdf = os.path.join(work, 'doc.pdf')
        if result.returncode != 0 or not os.path.exists(pdf):
            log = ''
            log_path = os.path.join(work, 'doc.log')
            if os.path.exists(log_path):
                with open(log_path, encoding='utf-8', errors='replace') as f:
                    log = f.read()
            detail = tex_error_summary(log) or log[-600:] or result.stderr.decode('utf-8', 'replace')[-600:]
            raise PdfError(f'TeX のコンパイルに失敗しました: {detail}')
        with open(pdf, 'rb') as f:
            return f.read()


def cached_pdf(tex: str, cache_dir: str, compiler: Callable[[str], bytes]) -> str:
    """同じ TeX の PDF があればそれを，なければコンパイルして保存したパスを返す。"""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, hashlib.sha1(tex.encode('utf-8')).hexdigest() + '.pdf')
    if not os.path.exists(path):
        data = compiler(tex)
        temp = path + '.part'
        with open(temp, 'wb') as f:
            f.write(data)
        os.replace(temp, path)
    return path


# ---------------------------------------------------------------- 書き出しのジョブ

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


def export_dir(upload_root: str, exam_id: str) -> str:
    return os.path.join(upload_root, 'exports', exam_id)


def _compiler(app) -> Callable[[str], bytes]:
    custom = app.config.get('PDF_COMPILER')  # テスト用（TeX を受け取り PDF のバイト列を返す関数）
    if custom is not None:
        return custom
    engine = find_engine(app.config.get('PDF_LATEX_ENGINE') or os.getenv('PDF_LATEX_ENGINE'))
    if engine is None:
        raise PdfError('xelatex が見つかりません。TeX（MacTeX など）を入れるか，.env の PDF_LATEX_ENGINE に xelatex の場所を書いてください')
    return lambda tex: compile_tex(tex, engine)


def plan_documents(exam, problems, latest_individual: Dict[Tuple[str, str], Any], latest_common: Dict[str, Any],
                   student_ids: List[str], settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    """生徒ごとの A（個別化）と B（マスター）の TeX・ファイル名・※ の判定を作る（LLM もコンパイルも行わない）。"""
    parsed = tex_template.parse(exam.template_tex)
    templated = [p for p in problems if p.template]
    documents = []
    for sid in student_ids:
        contents: Dict[str, str] = {}
        traces = []
        missing = []
        for problem in templated:
            number = tex_template.normalize_number(problem.number or '')
            row = latest_individual.get((problem.id, sid))
            if row is not None and json.loads(row.trace or '{}').get('format') == 'latex':
                contents[number] = row.content
                traces.append(json.loads(row.trace or '{}'))
                continue
            common = latest_common.get(problem.id)
            if common is not None and json.loads(common.trace or '{}').get('format') == 'latex':
                contents[number] = common.content
            elif json.loads(problem.steps or '[]'):
                missing.append(problem.number)
        evidence = personalization_evidence(traces, settings['min_evidence'], settings['min_ratio'])
        stem_a = file_stem(sid, 'A') + ('※' if evidence['insufficient'] else '')
        if missing and not traces:
            # 個別化した解説が1問もない生徒は A（個別化）を作らず，B（マスター）だけを作る
            documents.append({'student_id': sid, 'kind': 'A', 'filename': f'{file_stem(sid, "A")}.pdf',
                              'insufficient': False, 'evidence': evidence, 'missing': missing, 'tex': None,
                              'skipped': True,
                              'reason': '個別化した解説が1問も生成されていないため，A（個別化した解説）は作らず，'
                                        'B（マスター解説）だけを作りました'})
        else:
            # 一部の問題だけ解説がないときは，その問題をテンプレートのまま載せて A を作る（注意として知らせる）
            documents.append({'student_id': sid, 'kind': 'A', 'filename': f'{stem_a}.pdf',
                              'insufficient': evidence['insufficient'], 'evidence': evidence, 'missing': missing,
                              'tex': tex_template.build_document(parsed, [(sid, contents)], label=KIND_LABELS['A'])})
        documents.append({'student_id': sid, 'kind': 'B', 'filename': f'{file_stem(sid, "B")}.pdf', 'insufficient': False,
                          'evidence': None, 'missing': [],
                          'tex': tex_template.build_document(parsed, [(sid, {})], label=KIND_LABELS['B'])})
    return documents


def _run(app, job_id: str) -> None:
    from app import db
    from app.models import ExamImport, GeneratedExplanation, PdfExportJob, TestProblem

    job = db.session.get(PdfExportJob, job_id)
    if job is None:
        return
    request = json.loads(job.request or '{}')
    job.status, job.done, job.message = 'running', 0, None
    db.session.commit()
    try:
        compiler = _compiler(app)
        exam = db.session.get(ExamImport, job.exam_id)
        if exam is None or not exam.template_tex:
            raise PdfError('マスター解説のテンプレート（TeX）がありません')
        problems = TestProblem.query.filter_by(exam_id=exam.id).all()
        ids = [p.id for p in problems]
        latest_individual: Dict[Tuple[str, str], Any] = {}
        latest_common: Dict[str, Any] = {}
        for row in (GeneratedExplanation.query.filter(GeneratedExplanation.problem_id.in_(ids),
                                                      GeneratedExplanation.kind.in_(('individual', 'common')))
                    .order_by(GeneratedExplanation.created_at.asc()).all()):
            if row.kind == 'individual':
                latest_individual[(row.problem_id, row.student_id)] = row
            else:
                latest_common[row.problem_id] = row
        settings = {'min_evidence': int(app.config.get('PERSONALIZATION_MIN_EVIDENCE') or MIN_EVIDENCE),
                    'min_ratio': float(app.config.get('PERSONALIZATION_MIN_RATIO') or MIN_EVIDENCED_RATIO)}
        from app.learning import exclusions  # 研究協力不可の生徒の PDF は作らない
        documents = plan_documents(exam, problems, latest_individual, latest_common,
                                   exclusions.without_excluded(job.teacher_id, request.get('student_ids', [])), settings)
    except Exception as exc:
        db.session.rollback()
        job.status, job.message = 'failed', str(exc)
        db.session.commit()
        return

    cache_dir = os.path.abspath(os.path.join(export_dir(app.config.get('UPLOAD_FOLDER') or 'uploads', job.exam_id), 'cache'))
    files: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    results: Dict[int, Dict[str, Any]] = {}
    for document in documents:
        if document['missing'] and not document.get('skipped'):
            errors.append({'student_id': document['student_id'], 'file': document['filename'], 'warning': True,
                           'error': f'個別化した解説がない問題があります（{", ".join(document["missing"])}）。'
                                    'その問題はテンプレートのまま載せました'})
        if document.get('skipped'):
            errors.append({'student_id': document['student_id'], 'file': document['filename'], 'warning': True,
                           'skipped': True, 'error': document['reason']})
            results[id(document)] = {**{k: document[k] for k in ('student_id', 'kind', 'filename', 'insufficient', 'evidence')},
                                     'path': None, 'skipped': True, 'reason': document['reason']}
    to_compile = [d for d in documents if not d.get('skipped')]
    job.total = len(to_compile)
    files = [results[id(d)] for d in documents if id(d) in results]
    job.files = json.dumps(files, ensure_ascii=False)
    job.errors = json.dumps(errors, ensure_ascii=False)
    db.session.commit()

    workers = max(1, int(app.config.get('PDF_EXPORT_CONCURRENCY') or os.getenv('PDF_EXPORT_CONCURRENCY', 2)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(cached_pdf, d['tex'], cache_dir, compiler): d for d in to_compile}
        for future in as_completed(futures):
            document = futures[future]
            entry = {k: document[k] for k in ('student_id', 'kind', 'filename', 'insufficient', 'evidence')}
            try:
                entry['path'] = future.result()
            except Exception as exc:
                entry['path'] = None
                errors.append({'student_id': document['student_id'], 'file': document['filename'], 'error': str(exc)[:800]})
            results[id(document)] = entry
            job.done += 1
            files = [results[id(d)] for d in documents if id(d) in results]
            job.files = json.dumps(files, ensure_ascii=False)
            job.errors = json.dumps(errors, ensure_ascii=False)
            db.session.commit()
    job.status = 'completed' if any(f.get('path') for f in files) else 'failed'
    if job.status == 'failed' and not job.message:
        failures = [e for e in errors if not e.get('warning')]
        job.message = failures[0]['error'] if failures else 'PDF を作れませんでした'
    db.session.commit()


SKIPPED_NOTE_NAME = '教員用_解答解説Aを作らなかった生徒.txt'


def skipped_note(files: List[Dict[str, Any]]) -> Optional[str]:
    """A を作らなかった生徒の一覧（教員用。ZIP に入れて，B だけの生徒がいることを明記する）。"""
    skipped = [f for f in files if f.get('skipped')]
    if not skipped:
        return None
    lines = ['次の生徒は，個別化した解説が1問も生成されていないため，解答解説A（個別化した解説）を作らず，',
             '解答解説B（マスター解説）だけを作りました。', '']
    lines += [f'{file_stem(f["student_id"], "A").split("_")[0]}（{f["student_id"]}）: {f.get("reason", "")}' for f in skipped]
    return '\n'.join(lines) + '\n'


def zip_files(files: List[Dict[str, Any]], target: str, note: Optional[str] = None) -> str:
    """書き出した PDF を1つの ZIP にまとめる（ファイル名は日本語のまま）。note があれば教員用の説明を入れる。"""
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for entry in files:
            if entry.get('path') and os.path.exists(entry['path']):
                archive.write(entry['path'], arcname=entry['filename'])
        if note:
            archive.writestr(SKIPPED_NOTE_NAME, note)
    return target


def job_to_dict(job) -> Dict[str, Any]:
    files = json.loads(job.files or '[]')
    return {
        'id': job.id, 'exam_id': job.exam_id, 'status': job.status, 'running': is_running(job.id),
        'request': json.loads(job.request or '{}'), 'total': job.total or 0, 'done': job.done or 0,
        'message': job.message, 'errors': json.loads(job.errors or '[]'),
        'files': [{'index': i, 'student_id': f['student_id'], 'kind': f['kind'], 'filename': f['filename'],
                   'insufficient': f['insufficient'], 'evidence': f.get('evidence'), 'ready': bool(f.get('path')),
                   'skipped': bool(f.get('skipped')), 'reason': f.get('reason')}
                  for i, f in enumerate(files)],
        'created_at': job.created_at.isoformat() if job.created_at else None,
    }
