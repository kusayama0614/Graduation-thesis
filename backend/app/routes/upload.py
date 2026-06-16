# ==================== アップロードルート ====================
"""
ファイルアップロード関連のAPI エンドポイント
"""
from flask import Blueprint, request, jsonify, session
from app import db
from app.models import LearningData, AnswerSheet
from app.utils.firebase_service import get_firebase_service
import os
from datetime import datetime

upload_bp = Blueprint('upload', __name__)


# ==================== 学習データアップロード ====================
@upload_bp.route('/learning-data', methods=['POST'])
def upload_learning_data():
    """学習データをアップロード"""
    teacher_id = session.get('teacher_id') or request.form.get('teacher_id', 'guest')
    
    # ファイルの確認
    if 'files' not in request.files or len(request.files.getlist('files')) == 0:
        return jsonify({'error': 'No files provided'}), 400
    
    files = request.files.getlist('files')
    title = request.form.get('title', '').strip()
    category = request.form.get('category', 'other')
    description = request.form.get('description', '')
    
    # バリデーション
    if not title:
        return jsonify({'error': 'Title is required'}), 400
    
    upload_results = []
    firebase_service = get_firebase_service()
    
    for file in files:
        try:
            # ファイル名の生成
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"learning_{teacher_id}_{timestamp}_{file.filename}"
            filepath = os.path.join(os.getenv('UPLOAD_FOLDER', 'uploads'), filename)
            
            # ファイル保存
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            file.save(filepath)
            
            # ==================== デモ用: DBに保存（実装予定） ====================
            saved_record = None
            if firebase_service.enabled:
                saved_record = firebase_service.save_learning_data(
                    teacher_id=teacher_id,
                    title=title,
                    category=category,
                    description=description,
                    file_path=filepath,
                    file_name=file.filename,
                    file_size=os.path.getsize(filepath),
                    file_type=file.filename.split('.')[-1]
                )
            else:
                learning_data = LearningData(
                    teacher_id=teacher_id,
                    title=title,
                    category=category,
                    description=description,
                    file_path=filepath,
                    file_name=file.filename,
                    file_size=os.path.getsize(filepath),
                    file_type=file.filename.split('.')[-1]
                )
                db.session.add(learning_data)
                db.session.commit()
                saved_record = {'id': learning_data.id}
            
            upload_results.append({
                'filename': file.filename,
                'size': file.content_length,
                'status': 'success',
                'record_id': saved_record.get('id') if saved_record else None
            })
        
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
    auto_score = request.form.get('auto_score', 'true').lower() == 'true'
    ocr_process = request.form.get('ocr_process', 'true').lower() == 'true'
    generate_report = request.form.get('generate_report', 'false').lower() == 'true'
    
    # バリデーション
    if not test_name or not subject or not exam_date:
        return jsonify({
            'error': 'test_name, subject, and exam_date are required'
        }), 400
    
    upload_results = []
    firebase_service = get_firebase_service()
    
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
                    status='processing'
                )
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
            
            upload_results.append({
                'filename': file.filename,
                'size': file.content_length,
                'status': 'success',
                'record_id': saved_record.get('id') if saved_record else None,
                'processing_options': {
                    'auto_score': auto_score,
                    'ocr_process': ocr_process,
                    'generate_report': generate_report
                }
            })
        
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
