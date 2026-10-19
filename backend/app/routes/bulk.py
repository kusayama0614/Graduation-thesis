"""演習プリントの一括取り込み API（/api/bulk）

取り込み（PDF・画像をページに分ける）→ 見出しの読み取り（Gemini）→ 確認・修正 → 答案として登録，の順に使う。
処理の中身は app/utils/bulk_import.py を参照。
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import date

from flask import Blueprint, current_app, request, send_file, session
from werkzeug.utils import secure_filename

from app import db
from app.learning.llm import LLMUnavailable, get_vision_client
from app.models import AnswerSheet, BulkImportBatch, ScannedPage, Teacher
from app.routes.auth import login_required
from app.utils import bulk_import as bulk
from app.utils.answer_key_parser import parse_answer_key_file
from app.utils.answer_sheet_processor import start_answer_sheet_processing
from app.utils.api_response import make_error_response, make_success_response
from app.utils.firebase_service import get_firebase_service

bulk_bp = Blueprint('bulk', __name__)

MAX_PAGES_PER_FILE = 500


def _teacher_ids():
    teacher_id = session.get('teacher_id')
    teacher_db_id = session.get('teacher_db_id')
    if not teacher_db_id and teacher_id:
        teacher = Teacher.query.filter_by(teacher_id=teacher_id).first()
        teacher_db_id = teacher.id if teacher else None
    return teacher_id, teacher_db_id


def _get_batch(batch_id):
    _, teacher_db_id = _teacher_ids()
    if not teacher_db_id:
        return None
    return BulkImportBatch.query.filter_by(id=batch_id, teacher_id=teacher_db_id).first()


def _get_page(page_id):
    page = db.session.get(ScannedPage, page_id)
    if page is None or _get_batch(page.batch_id) is None:
        return None
    return page


def _files(batch):
    try:
        return json.loads(batch.files or '[]')
    except ValueError:
        return []


def _upload_root():
    return current_app.config.get('UPLOAD_FOLDER') or os.getenv('UPLOAD_FOLDER', 'uploads')


def _page_dict(page):
    reading = json.loads(page.reading) if page.reading else {}
    return {
        'id': page.id, 'file_id': page.file_id, 'page_index': page.page_index, 'status': page.status,
        'kind': page.kind, 'print_no': page.print_no, 'print_title': page.print_title, 'grade': page.grade,
        'class_no': page.class_no, 'student_no': page.student_no, 'confidence': page.confidence,
        'note': reading.get('note') or None, 'error': page.error, 'answer_sheet_id': page.answer_sheet_id,
    }


def _batch_summary(batch):
    counts = dict(db.session.query(ScannedPage.status, db.func.count(ScannedPage.id))
                  .filter_by(batch_id=batch.id).group_by(ScannedPage.status).all())
    total = sum(counts.values())
    return {
        'id': batch.id, 'name': batch.name, 'subject': batch.subject, 'status': batch.status,
        'reading': bulk.is_reading(batch.id), 'files': _files(batch),
        'page_count': total, 'pages_read': total - counts.get('pending', 0) - counts.get('error', 0),
        'pages_error': counts.get('error', 0),
        'created_at': batch.created_at.isoformat() if batch.created_at else None,
    }


# ---------------------------------------------------------------- 取り込み

@bulk_bp.route('/batches', methods=['GET'])
@login_required
def list_batches():
    _, teacher_db_id = _teacher_ids()
    batches = (BulkImportBatch.query.filter_by(teacher_id=teacher_db_id)
               .order_by(BulkImportBatch.created_at.desc()).all()) if teacher_db_id else []
    return make_success_response({'batches': [_batch_summary(b) for b in batches]})


@bulk_bp.route('/batches', methods=['POST'])
@login_required
def create_batch():
    _, teacher_db_id = _teacher_ids()
    if not teacher_db_id:
        return make_error_response('AUTH_TEACHER_NOT_FOUND', 'Teacher account was not found for this session', 401)
    data = request.get_json(silent=True) or {}
    name = str(data.get('name') or '').strip()
    if not name:
        return make_error_response('BULK_NAME_REQUIRED', '取り込みの名前を入力してください', 400)
    batch = BulkImportBatch(teacher_id=teacher_db_id, name=name[:200],
                            subject=str(data.get('subject') or '数学').strip()[:50], files='[]')
    db.session.add(batch)
    db.session.commit()
    return make_success_response({'batch': _batch_summary(batch)}, 201)


@bulk_bp.route('/batches/<batch_id>', methods=['DELETE'])
@login_required
def delete_batch(batch_id):
    batch = _get_batch(batch_id)
    if batch is None:
        return make_error_response('BULK_BATCH_NOT_FOUND', 'Batch not found', 404)
    if bulk.is_reading(batch.id):
        return make_error_response('BULK_BUSY', '読み取り中は削除できません', 409)
    directory = bulk.batch_dir(_upload_root(), batch.id)
    db.session.delete(batch)
    db.session.commit()
    shutil.rmtree(directory, ignore_errors=True)  # 登録済みの答案（AnswerSheet）とその PDF は残る
    return make_success_response({'deleted': batch_id})


@bulk_bp.route('/parse-file-name', methods=['GET'])
@login_required
def parse_file_name():
    return make_success_response({'parsed': bulk.parse_file_name(request.args.get('name', ''))})


@bulk_bp.route('/batches/<batch_id>/files', methods=['POST'])
@login_required
def upload_file(batch_id):
    """1ファイルずつ受け取り，ページに分けて保存する（大量のファイルは画面から順に送る）。"""
    batch = _get_batch(batch_id)
    if batch is None:
        return make_error_response('BULK_BATCH_NOT_FOUND', 'Batch not found', 404)
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return make_error_response('UPLOAD_FILES_MISSING', 'No file provided', 400)
    original_name = upload.filename
    extension = os.path.splitext(original_name)[1].lower()
    if extension not in bulk.SUPPORTED_EXTENSIONS:
        return make_error_response('BULK_UNSUPPORTED_FILE', f'対応していないファイル形式です（{extension or "拡張子なし"}）', 400)

    guessed = bulk.parse_file_name(original_name)

    def int_field(name, fallback):
        value = (request.form.get(name) or '').strip()
        return int(value) if value.isdigit() and int(value) > 0 else fallback

    file_id = str(uuid.uuid4())
    directory = bulk.batch_dir(_upload_root(), batch.id)
    source_dir = os.path.join(directory, 'source')
    os.makedirs(source_dir, exist_ok=True)
    source_path = os.path.join(source_dir, f'{file_id}{extension}')
    upload.save(source_path)
    try:
        image_paths = bulk.split_into_pages(source_path, os.path.join(directory, 'pages'), file_id)
    except Exception as exc:
        os.remove(source_path)
        return make_error_response('BULK_SPLIT_FAILED', f'ファイルをページに分けられませんでした（{exc.__class__.__name__}）', 400)
    if len(image_paths) > MAX_PAGES_PER_FILE:
        for path in image_paths:
            os.remove(path)
        return make_error_response('BULK_TOO_MANY_PAGES', f'1ファイルのページ数は {MAX_PAGES_PER_FILE} までです', 400)

    info = {
        'id': file_id, 'name': original_name, 'pages': len(image_paths),
        'grade': int_field('grade', guessed['grade']), 'class_no': int_field('class_no', guessed['class_no']),
        'date': (bulk.parse_iso_date(request.form.get('date')) or bulk.parse_iso_date(guessed['date']) or None),
    }
    info['date'] = info['date'].isoformat() if info['date'] else None
    for index, path in enumerate(image_paths):
        db.session.add(ScannedPage(batch_id=batch.id, file_id=file_id, file_name=original_name,
                                   page_index=index, image_path=path, status='pending'))
    files = _files(batch)
    files.append(info)
    batch.files = json.dumps(files, ensure_ascii=False)
    db.session.commit()
    return make_success_response({'file': info, 'batch': _batch_summary(batch)}, 201)


# ---------------------------------------------------------------- 読み取り

@bulk_bp.route('/batches/<batch_id>/read', methods=['POST'])
@login_required
def start_read(batch_id):
    batch = _get_batch(batch_id)
    if batch is None:
        return make_error_response('BULK_BATCH_NOT_FOUND', 'Batch not found', 404)
    try:
        get_vision_client(required=True)
    except LLMUnavailable as exc:
        return make_error_response('LLM_UNAVAILABLE', str(exc), 503)
    started = bulk.start_reading(current_app._get_current_object(), batch.id)
    db.session.expire_all()
    return make_success_response({'started': started, 'batch': _batch_summary(_get_batch(batch_id))}, 202)


@bulk_bp.route('/batches/<batch_id>', methods=['GET'])
@login_required
def batch_detail(batch_id):
    batch = _get_batch(batch_id)
    if batch is None:
        return make_error_response('BULK_BATCH_NOT_FOUND', 'Batch not found', 404)
    pages = ScannedPage.query.filter_by(batch_id=batch.id).all()
    grouped = bulk.build_groups(pages, _files(batch))
    return make_success_response({
        'batch': _batch_summary(batch),
        'summary': bulk.summarize(grouped['groups']),
        'groups': grouped['groups'],
        'unresolved': grouped['unresolved'],
        'pages': {p.id: _page_dict(p) for p in pages},
        'issue_labels': bulk.ISSUE_LABELS,
    })


@bulk_bp.route('/pages/<page_id>/image', methods=['GET'])
@login_required
def page_image(page_id):
    page = _get_page(page_id)
    if page is None or not os.path.exists(page.image_path):
        return make_error_response('BULK_PAGE_NOT_FOUND', 'Page not found', 404)
    return send_file(os.path.abspath(page.image_path), mimetype='image/jpeg', max_age=3600)


@bulk_bp.route('/pages/<page_id>', methods=['PATCH'])
@login_required
def update_page(page_id):
    """読み取り結果を直す（直したページは確認済みになる）。"""
    page = _get_page(page_id)
    if page is None:
        return make_error_response('BULK_PAGE_NOT_FOUND', 'Page not found', 404)
    if page.answer_sheet_id:
        return make_error_response('BULK_ALREADY_REGISTERED', '登録済みのページは変更できません', 409)
    data = request.get_json(silent=True) or {}
    if 'kind' in data:
        if data['kind'] not in ('front', 'back', 'excluded'):
            return make_error_response('BULK_INVALID_KIND', 'kind は front / back / excluded のいずれかです', 400)
        page.kind = data['kind']
    for field in ('print_no', 'grade', 'class_no', 'student_no'):
        if field in data:
            value = data[field]
            if value in (None, ''):
                setattr(page, field, None)
            elif isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
                number = int(value)
                setattr(page, field, number if number > 0 else None)
            else:
                return make_error_response('BULK_INVALID_NUMBER', f'{field} は数字で入力してください', 400)
    if 'print_title' in data:
        page.print_title = str(data['print_title'] or '').strip()[:300] or None
    page.status = 'confirmed'
    db.session.commit()
    return make_success_response({'page': _page_dict(page)})


@bulk_bp.route('/batches/<batch_id>/confirm', methods=['POST'])
@login_required
def confirm_pages(batch_id):
    """画像を見て読み取りが正しいと確かめたページを，まとめて確認済みにする。"""
    batch = _get_batch(batch_id)
    if batch is None:
        return make_error_response('BULK_BATCH_NOT_FOUND', 'Batch not found', 404)
    ids = (request.get_json(silent=True) or {}).get('page_ids') or []
    pages = ScannedPage.query.filter(ScannedPage.batch_id == batch.id, ScannedPage.id.in_(ids),
                                     ScannedPage.kind.isnot(None)).all()
    for page in pages:
        page.status = 'confirmed'
    db.session.commit()
    return make_success_response({'confirmed': len(pages)})


# ---------------------------------------------------------------- 登録

@bulk_bp.route('/answer-key/parse', methods=['POST'])
@login_required
def parse_answer_key():
    """解答キーのファイル（CSV・Excel など）を，登録で使う形に変換して返す（保存はしない）。"""
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return make_error_response('UPLOAD_FILES_MISSING', 'No file provided', 400)
    directory = os.path.join(_upload_root(), 'bulk', '_keys')
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f'{uuid.uuid4()}_{secure_filename(upload.filename) or "key"}')
    upload.save(path)
    try:
        parsed = parse_answer_key_file(path, upload.filename)
    finally:
        os.remove(path)
    if not parsed.get('success') or not parsed.get('candidates'):
        return make_error_response('BULK_KEY_PARSE_FAILED', '解答キーを読み取れませんでした', 400)
    answers = {str(c.get('question')): str(c.get('answer')) for c in parsed['candidates']}
    key = {'total_questions': len(answers), 'answers': answers}
    nodes = {str(c.get('question')): c['nodes'] for c in parsed['candidates'] if c.get('nodes')}
    if nodes:
        key['nodes'] = nodes
    return make_success_response({'answer_key': key})


@bulk_bp.route('/batches/<batch_id>/register', methods=['POST'])
@login_required
def register(batch_id):
    """確認の済んだ答案を，プリントごとの設定（テスト名・単元・解答キー）で AnswerSheet として登録する。"""
    batch = _get_batch(batch_id)
    if batch is None:
        return make_error_response('BULK_BATCH_NOT_FOUND', 'Batch not found', 404)
    if bulk.is_reading(batch.id):
        return make_error_response('BULK_BUSY', '読み取りが終わってから登録してください', 409)
    data = request.get_json(silent=True) or {}
    prints = {str(k): v for k, v in (data.get('prints') or {}).items() if isinstance(v, dict)}
    if not prints:
        return make_error_response('BULK_NO_PRINTS', '登録するプリントを選んでください', 400)
    run_ocr = bool(data.get('run_ocr'))
    teacher_id, teacher_db_id = _teacher_ids()
    files = {f['id']: f for f in _files(batch)}
    pages = ScannedPage.query.filter_by(batch_id=batch.id).all()
    page_map = {p.id: p for p in pages}
    grouped = bulk.build_groups(pages, list(files.values()))
    firebase_service = get_firebase_service()

    created, skipped = [], []
    for group in grouped['groups']:
        setting = prints.get(str(group['print_no'])) if group['print_no'] else None
        if setting is None:
            continue
        if group['registered']:
            skipped.append({'student_id': group['student_id'], 'print_no': group['print_no'], 'reason': '登録済み'})
            continue
        if not group['ok']:
            skipped.append({'student_id': group['student_id'], 'print_no': group['print_no'],
                            'reason': '・'.join(group['issue_labels'])})
            continue
        file_info = files.get(group['file_id'], {})
        exam_date = (bulk.parse_iso_date(setting.get('exam_date')) or bulk.parse_iso_date(file_info.get('date'))
                     or date.today())
        test_name = (str(setting.get('test_name') or '').strip()
                     or f'No.{group["print_no"]} {group["print_title"] or ""}'.strip())[:200]
        answer_key = setting.get('answer_key') if isinstance(setting.get('answer_key'), dict) else None
        unit = str(setting.get('unit') or '').strip()
        if answer_key is not None:
            answer_key = dict(answer_key)
            if 'answers' not in answer_key:
                answer_key = {'answers': answer_key}
            if unit:
                answer_key['unit'] = unit
        elif unit:
            answer_key = {'answers': {}, 'unit': unit}

        sheet_name = f'No{group["print_no"]}_{group["student_id"]}.pdf'
        pdf_path = bulk.pages_to_pdf([page_map[pid].image_path for pid in group['page_ids']],
                                     os.path.join(bulk.batch_dir(_upload_root(), batch.id), 'sheets',
                                                  f'{uuid.uuid4()}_{sheet_name}'))
        notes = f'一括取り込み「{batch.name}」（{file_info.get("name", "")}）'
        processing_options = {'auto_score': run_ocr and bool(answer_key and answer_key.get('answers')),
                              'ocr_process': run_ocr, 'generate_report': False}
        common = dict(test_name=test_name, subject=batch.subject or '数学',
                      student_grade=str(group['grade']), student_class=str(group['class_no']),
                      student_id=group['student_id'], notes=notes, file_path=pdf_path, file_name=sheet_name,
                      file_size=os.path.getsize(pdf_path))
        job_id = str(uuid.uuid4())
        status = 'processing' if run_ocr else 'completed'
        if firebase_service.enabled:
            record = firebase_service.save_answer_sheet(
                teacher_id=teacher_id, exam_date=exam_date.isoformat(), status=status,
                processing_options=processing_options, processing_stage='queued' if run_ocr else 'uploaded',
                current_step='queued' if run_ocr else 'completed', completed_steps=[],
                progress_percent=0 if run_ocr else 100,
                processing_message='一括取り込みから登録しました', **common)
            sheet_id = record.get('id')
            updates = {'processing_job_id': job_id}
            if answer_key:
                updates['answer_key'] = answer_key
            firebase_service.update_answer_sheet(sheet_id, updates)
        else:
            sheet = AnswerSheet(
                teacher_id=teacher_db_id, exam_date=exam_date, status=status,
                processing_options=json.dumps(processing_options, ensure_ascii=False),
                processing_stage='queued' if run_ocr else 'uploaded', processing_job_id=job_id,
                current_step='queued' if run_ocr else 'completed', completed_steps='[]',
                progress_percent=0 if run_ocr else 100, processing_message='一括取り込みから登録しました',
                answer_key=json.dumps(answer_key, ensure_ascii=False) if answer_key else None, **common)
            db.session.add(sheet)
            db.session.flush()
            sheet_id = sheet.id
        for pid in group['page_ids']:
            page_map[pid].answer_sheet_id = sheet_id
        db.session.commit()
        created.append({'answer_sheet_id': sheet_id, 'student_id': group['student_id'],
                        'print_no': group['print_no'], 'test_name': test_name})
        if run_ocr:
            start_answer_sheet_processing(current_app._get_current_object(), sheet_id, teacher_id, {
                'teacher_id': teacher_id, 'student_grade': common['student_grade'],
                'student_class': common['student_class'], 'student_id': group['student_id'],
                'test_name': test_name, 'subject': common['subject'], 'exam_date': exam_date.isoformat(),
                'notes': notes, 'processing_options': processing_options, 'processing_job_id': job_id,
                'answer_key': answer_key,
            })

    if created:
        batch.status = 'registered'
        db.session.commit()
    return make_success_response({'created': created, 'skipped': skipped})
