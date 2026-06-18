# ==================== アップロードルート ====================
"""
ファイルアップロード関連のAPI エンドポイント
"""
from flask import Blueprint, request, jsonify, session, current_app
from app import db
from app.models import LearningData, AnswerSheet, Teacher
from app.routes.auth import login_required
from app.utils.answer_sheet_processor import start_answer_sheet_processing
from app.utils.firebase_service import get_firebase_service
from app.utils.ocr import get_ocr_status
from app.utils.answer_key_parser import parse_answer_key_file
import os
from datetime import datetime
import json

upload_bp = Blueprint('upload', __name__)


@upload_bp.route('/ocr-status', methods=['GET'])
def ocr_status():
    """OCR の利用可否を返す"""
    return jsonify(get_ocr_status()), 200


def _save_upload_file(file_storage, prefix: str, teacher_id: str) -> str:
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{prefix}_{teacher_id}_{timestamp}_{file_storage.filename}"
    filepath = os.path.join(os.getenv('UPLOAD_FOLDER', 'uploads'), filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    file_storage.save(filepath)
    return filepath


def _get_teacher_db_id(teacher_id: str) -> str | None:
    teacher_db_id = session.get('teacher_db_id')
    if teacher_db_id:
        return teacher_db_id

    teacher = Teacher.query.filter_by(teacher_id=teacher_id).first()
    if teacher:
        return teacher.id

    return None


@upload_bp.route('/learning-data', methods=['POST'])
@login_required
def upload_learning_data():
    """学習データをアップロード"""
    teacher_id = session.get('teacher_id') or request.form.get('teacher_id', 'guest')
    teacher_db_id = _get_teacher_db_id(teacher_id)

    if not teacher_db_id:
        return jsonify({'error': 'Teacher account was not found for this session'}), 401

    if 'files' not in request.files or len(request.files.getlist('files')) == 0:
        return jsonify({'error': 'No files provided'}), 400

    files = request.files.getlist('files')
    title = request.form.get('title', '').strip()
    category = request.form.get('category', 'other')
    description = request.form.get('description', '')

    if not title:
        return jsonify({'error': 'Title is required'}), 400

    upload_results = []
    firebase_service = get_firebase_service()

    for file in files:
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"learning_{teacher_id}_{timestamp}_{file.filename}"
            filepath = os.path.join(os.getenv('UPLOAD_FOLDER', 'uploads'), filename)

            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            file.save(filepath)

            file_size = os.path.getsize(filepath)
            file_type = file.filename.split('.')[-1].lower() if '.' in file.filename else ''

            local_learning_data = LearningData(
                teacher_id=teacher_db_id,
                title=title,
                category=category,
                description=description,
                file_path=filepath,
                file_name=file.filename,
                file_size=file_size,
                file_type=file_type,
            )
            db.session.add(local_learning_data)
            db.session.commit()

            firebase_record = None
            firebase_error = None
            if firebase_service.enabled:
                try:
                    firebase_record = firebase_service.save_learning_data(
                        teacher_id=teacher_id,
                        title=title,
                        category=category,
                        description=description,
                        file_path=filepath,
                        file_name=file.filename,
                        file_size=file_size,
                        file_type=file_type,
                    )
                except Exception as exc:
                    firebase_error = str(exc)

            upload_results.append({
                'filename': file.filename,
                'size': file_size,
                'status': 'success',
                'record_id': local_learning_data.id,
                'firebase_record_id': firebase_record.get('id') if firebase_record else None,
                'warning': firebase_error,
            })

        except Exception as exc:
            db.session.rollback()
            upload_results.append({
                'filename': file.filename,
                'error': str(exc),
                'status': 'failed'
            })

    return jsonify({
        'success': True,
        'message': f'Uploaded {len(upload_results)} files',
        'results': upload_results
    }), 200


# ==================== 解答用紙アップロード ====================
@upload_bp.route('/answer-sheet', methods=['POST'])
def upload_answer_sheet():
    """解答用紙をアップロード"""
    teacher_id = session.get('teacher_id') or request.form.get('teacher_id', 'guest')
    
    # ファイルの確認
    if 'files' not in request.files or len(request.files.getlist('files')) == 0:
        return jsonify({'error': 'No files provided'}), 400
    
    files = request.files.getlist('files')
    test_name = request.form.get('test_name', '').strip()
    subject = request.form.get('subject', '').strip()
    exam_date = request.form.get('exam_date', '').strip()
    student_grade = request.form.get('student_grade', '').strip() or None
    student_class = request.form.get('student_class', '').strip() or None
    student_id = request.form.get('student_id', '').strip() or None
    notes = request.form.get('notes', '').strip() or None
    auto_score = request.form.get('auto_score', 'true').lower() == 'true'
    ocr_process = request.form.get('ocr_process', 'true').lower() == 'true'
    generate_report = request.form.get('generate_report', 'false').lower() == 'true'
    # 解答キー（JSON文字列）またはファイルで受け取る
    answer_key = None
    raw_key = request.form.get('answer_key')
    if raw_key:
        try:
            answer_key = json.loads(raw_key)
        except Exception:
            answer_key = None
    if not answer_key and 'answer_key_file' in request.files:
        try:
            key_file = request.files.get('answer_key_file')
            # Save temporary key file and attempt to parse structured formats
            keypath = _save_upload_file(key_file, 'answer_key_uploaded', teacher_id)
            parsed = parse_answer_key_file(keypath, key_file.filename)
            if parsed.get('success'):
                # convert candidates list to internal answer_key dict
                answers = {str(item.get('question')): str(item.get('answer')) for item in parsed.get('candidates', [])}
                answer_key = {
                    'total_questions': parsed.get('total_candidates', len(answers)),
                    'answers': answers
                }
            else:
                # try JSON fallback
                try:
                    key_file.stream.seek(0)
                    key_text = key_file.read().decode('utf-8')
                    answer_key = json.loads(key_text)
                except Exception:
                    answer_key = None
        except Exception:
            answer_key = None
    
    # バリデーション
    if not test_name or not subject or not exam_date:
        return jsonify({
            'error': 'test_name, subject, and exam_date are required'
        }), 400
    
    upload_results = []
    firebase_service = get_firebase_service()
    processing_options = {
        'auto_score': auto_score,
        'ocr_process': ocr_process,
        'generate_report': generate_report,
    }
    processing_steps = []
    if ocr_process:
        processing_steps.append('ocr')
    if auto_score:
        processing_steps.append('auto_score')
    if generate_report:
        processing_steps.append('generate_report')

    processing_stage = 'queued' if processing_steps else 'uploaded'
    
    for file in files:
        try:
            # ファイル名の生成
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"answer_{teacher_id}_{timestamp}_{file.filename}"
            filepath = os.path.join(os.getenv('UPLOAD_FOLDER', 'uploads'), filename)
            
            # ファイル保存
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            file.save(filepath)
            
            # ==================== デモ用: DBに保存（実装予定） ====================
            saved_record = None
            if firebase_service.enabled:
                saved_record = firebase_service.save_answer_sheet(
                    teacher_id=teacher_id,
                    test_name=test_name,
                    subject=subject,
                    exam_date=exam_date,
                    file_path=filepath,
                    file_name=file.filename,
                    file_size=os.path.getsize(filepath),
                    status='processing',
                    student_grade=student_grade,
                    student_class=student_class,
                    student_id=student_id,
                    notes=notes,
                    processing_options=processing_options,
                    processing_stage=processing_stage,
                    current_step='queued',
                    completed_steps=[],
                    progress_percent=0,
                    processing_message='キューに登録されました',
                )
                # 解答キーがあれば後で使えるよう保存
                if answer_key:
                    try:
                        firebase_service.update_answer_sheet(saved_record.get('id'), {'answer_key': answer_key})
                    except Exception:
                        pass
            else:
                answer_sheet = AnswerSheet(
                    teacher_id=teacher_id,
                    test_name=test_name,
                    subject=subject,
                    exam_date=exam_date,
                    file_path=filepath,
                    file_name=file.filename,
                    file_size=os.path.getsize(filepath),
                    status='processing'
                )
                db.session.add(answer_sheet)
                db.session.commit()
                saved_record = {'id': answer_sheet.id}
                # ローカルDB を使う場合は answer_key を保存するカラムがなければスキップ
                if answer_key:
                    try:
                        # attempt to save to a JSON file next to upload as fallback
                        ak_path = filepath + '.answer_key.json'
                        with open(ak_path, 'w', encoding='utf-8') as akf:
                            json.dump(answer_key, akf, ensure_ascii=False, indent=2)
                    except Exception:
                        pass
            
            upload_results.append({
                'filename': file.filename,
                'size': file.content_length,
                'status': 'success',
                'record_id': saved_record.get('id') if saved_record else None,
                'student_id': student_id,
                'processing_stage': processing_stage,
                'processing_options': processing_options,
                'next_steps': processing_steps,
            })

            start_answer_sheet_processing(
                current_app._get_current_object(),
                saved_record.get('id') if saved_record else None,
                teacher_id,
                {
                    'teacher_id': teacher_id,
                    'student_grade': student_grade,
                    'student_class': student_class,
                    'student_id': student_id,
                    'test_name': test_name,
                    'subject': subject,
                    'exam_date': exam_date,
                    'notes': notes,
                    'processing_options': processing_options,
                    'answer_key': answer_key,
                }
            )
        
        except Exception as e:
            upload_results.append({
                'filename': file.filename,
                'error': str(e),
                'status': 'failed'
            })
    
    return jsonify({
        'success': True,
        'message': f'Uploaded {len(upload_results)} files',
        'results': upload_results
        ,
        'workflow': {
            'student_grade': student_grade,
            'student_class': student_class,
            'student_id': student_id,
            'processing_options': processing_options,
            'processing_steps': processing_steps,
            'processing_stage': processing_stage,
        }
    }), 200


# ==================== アップロード済みファイル一覧 ====================
@upload_bp.route('/list', methods=['GET'])
def list_uploads():
    """アップロード済みファイルの一覧を取得"""
    upload_folder = os.getenv('UPLOAD_FOLDER', 'uploads')
    
    if not os.path.exists(upload_folder):
        return jsonify({'files': []}), 200
    
    files = []
    for filename in os.listdir(upload_folder):
        filepath = os.path.join(upload_folder, filename)
        if os.path.isfile(filepath):
            files.append({
                'name': filename,
                'size': os.path.getsize(filepath),
                'created': datetime.fromtimestamp(
                    os.path.getctime(filepath)
                ).isoformat()
            })
    
    return jsonify({'files': files}), 200
