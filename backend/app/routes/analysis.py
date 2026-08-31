# ==================== 分析ルート ====================
"""
データ分析関連のAPI エンドポイント（Langchain統合）
"""
from flask import Blueprint, request, jsonify, session
from app import db
from app.models import AnswerSheet, AnalysisResult, Teacher
from app.routes.auth import login_required
from app.utils.langchain_analyzer import get_report_generator
from app.utils.firebase_service import get_firebase_service
import json
import traceback
from datetime import datetime
import io
import csv

analysis_bp = Blueprint('analysis', __name__)


def _resolve_teacher_ids():
    teacher_id = session.get('teacher_id')
    teacher_db_id = session.get('teacher_db_id')

    if not teacher_db_id and teacher_id:
        teacher = Teacher.query.filter_by(teacher_id=teacher_id).first()
        if teacher:
            teacher_db_id = teacher.id

    return teacher_id, teacher_db_id


def _deserialize_error_patterns(raw_value):
    if isinstance(raw_value, list):
        return raw_value
    if isinstance(raw_value, str) and raw_value.strip():
        try:
            parsed = json.loads(raw_value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _status_defaults(status):
    status_value = status or 'pending'
    progress_map = {
        'completed': 100,
        'failed': 100,
        'processing': 50,
        'pending': 0,
        'queued': 0,
    }
    message_map = {
        'completed': '処理が完了しました',
        'failed': '処理に失敗しました',
        'processing': '処理中です',
        'pending': '処理待ちです',
        'queued': 'キュー登録済みです',
    }
    return {
        'processing_stage': status_value,
        'current_step': status_value,
        'completed_steps': ['completed'] if status_value == 'completed' else [],
        'progress_percent': progress_map.get(status_value, 0),
        'processing_message': message_map.get(status_value, '処理中です'),
    }


def _latest_analysis_by_answer_sheet(answer_sheet_ids):
    if not answer_sheet_ids:
        return {}

    results = AnalysisResult.query.filter(
        AnalysisResult.answer_sheet_id.in_(answer_sheet_ids)
    ).order_by(AnalysisResult.created_at.desc()).all()

    latest = {}
    for result in results:
        if result.answer_sheet_id not in latest:
            latest[result.answer_sheet_id] = result
    return latest


def _sheet_to_response_payload(sheet, analysis_result=None):
    status_defaults = _status_defaults(sheet.status)
    error_patterns = _deserialize_error_patterns(analysis_result.error_patterns) if analysis_result else []
    processing_time = analysis_result.processing_time if analysis_result and analysis_result.processing_time else '---'
    upload_date = sheet.created_at.date().isoformat() if sheet.created_at else None

    payload = {
        'id': sheet.id,
        'test_name': sheet.test_name,
        'subject': sheet.subject,
        'student_id': analysis_result.student_name if analysis_result and analysis_result.student_name else None,
        'student_name': analysis_result.student_name if analysis_result and analysis_result.student_name else None,
        'score': analysis_result.score if analysis_result else None,
        'status': sheet.status,
        'upload_date': upload_date,
        'processing_time': processing_time,
        'questions': analysis_result.total_questions if analysis_result else None,
        'correct_answers': analysis_result.correct_count if analysis_result else None,
        'error_patterns': error_patterns,
        'analysis': analysis_result.analysis_text if analysis_result else None,
        'study_plan': analysis_result.study_plan if analysis_result else None,
        'analysis_result_id': analysis_result.id if analysis_result else None,
        'exam_date': sheet.exam_date.isoformat() if sheet.exam_date else None,
        'file_name': sheet.file_name,
        'file_size': sheet.file_size,
        'processing_options': {},
    }
    payload.update(status_defaults)
    return payload


def _apply_row_filters(rows, test_name='', subject='', status='', student_id='', upload_date='', data_id=''):
    filtered = rows

    if data_id and data_id != 'all':
        filtered = [row for row in filtered if str(row.get('id')) == str(data_id)]

    if test_name:
        needle = test_name.lower()
        filtered = [row for row in filtered if needle in (row.get('test_name', '').lower())]

    if subject:
        filtered = [row for row in filtered if row.get('subject') == subject]

    if status:
        filtered = [row for row in filtered if row.get('status') == status]

    if student_id:
        needle = student_id.lower()
        filtered = [
            row for row in filtered
            if needle in str(row.get('student_id') or '').lower() or needle in str(row.get('student_name') or '').lower()
        ]

    if upload_date:
        filtered = [row for row in filtered if str(row.get('upload_date') or '').startswith(upload_date)]

    return filtered


def _rows_to_csv_text(rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'id',
        'test_name',
        'subject',
        'student_id',
        'student_name',
        'status',
        'score',
        'correct_answers',
        'questions',
        'upload_date',
        'processing_stage',
        'current_step',
        'progress_percent',
        'processing_message',
    ])

    for row in rows:
        writer.writerow([
            row.get('id'),
            row.get('test_name'),
            row.get('subject'),
            row.get('student_id'),
            row.get('student_name'),
            row.get('status'),
            row.get('score'),
            row.get('correct_answers'),
            row.get('questions'),
            row.get('upload_date'),
            row.get('processing_stage'),
            row.get('current_step'),
            row.get('progress_percent'),
            row.get('processing_message'),
        ])

    return output.getvalue()


# ==================== 解答データ一覧取得 ====================
@analysis_bp.route('/answer-data', methods=['GET'])
@login_required
def get_answer_data():
    """解答データの一覧を取得"""
    teacher_id, teacher_db_id = _resolve_teacher_ids()
    
    # フィルターパラメータ
    test_name = request.args.get('test_name', '').lower()
    subject = request.args.get('subject', '')
    status = request.args.get('status', '')
    student_id = request.args.get('student_id', '').strip()
    upload_date = request.args.get('upload_date', '').strip()
    
    firebase_service = get_firebase_service()

    if firebase_service.enabled:
        firebase_rows = firebase_service.list_answer_sheets(teacher_id=teacher_id)
        filtered_data = _apply_row_filters(
            firebase_rows,
            test_name=test_name,
            subject=subject,
            status=status,
            student_id=student_id,
            upload_date=upload_date,
        )

        return jsonify({
            'total': len(firebase_rows),
            'filtered': len(filtered_data),
            'data': filtered_data
        }), 200

    if not teacher_db_id:
        return jsonify({'total': 0, 'filtered': 0, 'data': []}), 200

    local_rows = AnswerSheet.query.filter_by(teacher_id=teacher_db_id).order_by(AnswerSheet.created_at.desc()).all()
    latest_analysis = _latest_analysis_by_answer_sheet([row.id for row in local_rows])
    rows = [_sheet_to_response_payload(row, latest_analysis.get(row.id)) for row in local_rows]
    filtered_data = _apply_row_filters(
        rows,
        test_name=test_name,
        subject=subject,
        status=status,
        student_id=student_id,
        upload_date=upload_date,
    )

    return jsonify({
        'total': len(rows),
        'filtered': len(filtered_data),
        'data': filtered_data
    }), 200


# ==================== 解答データ詳細取得 ====================
@analysis_bp.route('/answer-data/<data_id>', methods=['GET'])
@login_required
def get_answer_data_detail(data_id):
    """解答データの詳細を取得"""
    teacher_id, teacher_db_id = _resolve_teacher_ids()
    firebase_service = get_firebase_service()

    if firebase_service.enabled:
        answer_sheet = firebase_service.get_answer_sheet(data_id)
        if answer_sheet and answer_sheet.get('teacher_id') == teacher_id:
            analysis_result = firebase_service.get_analysis_result_by_answer_sheet_id(data_id)
            if analysis_result:
                answer_sheet = answer_sheet | {
                    'analysis': analysis_result.get('analysis_text'),
                    'study_plan': analysis_result.get('study_plan'),
                    'score': analysis_result.get('score'),
                    'correct_answers': analysis_result.get('correct_count'),
                    'questions': analysis_result.get('total_questions'),
                    'error_patterns': analysis_result.get('error_patterns'),
                    'analysis_result_id': analysis_result.get('id'),
                }

            return jsonify(answer_sheet), 200

    if not teacher_db_id:
        return jsonify({'error': 'Data not found'}), 404

    answer_sheet = AnswerSheet.query.filter_by(id=data_id, teacher_id=teacher_db_id).first()
    if not answer_sheet:
        return jsonify({'error': 'Data not found'}), 404

    analysis_result = AnalysisResult.query.filter_by(answer_sheet_id=data_id).order_by(AnalysisResult.created_at.desc()).first()
    return jsonify(_sheet_to_response_payload(answer_sheet, analysis_result)), 200
    
    return jsonify({'error': 'Data not found'}), 404


# ==================== 統計情報取得 ====================
@analysis_bp.route('/statistics', methods=['GET'])
@login_required
def get_statistics():
    """統計情報を取得"""
    teacher_id, teacher_db_id = _resolve_teacher_ids()
    firebase_service = get_firebase_service()

    if firebase_service.enabled:
        rows = firebase_service.list_answer_sheets(teacher_id=teacher_id)
        status_counts = {
            'completed': 0,
            'processing': 0,
            'failed': 0,
            'pending': 0,
        }
        subjects = {}
        scores = []

        for row in rows:
            row_status = (row.get('status') or 'pending').lower()
            if row_status in status_counts:
                status_counts[row_status] += 1
            else:
                status_counts['pending'] += 1

            subject_key = row.get('subject') or 'unknown'
            subject_stats = subjects.setdefault(subject_key, {'total': 0, 'scores': []})
            subject_stats['total'] += 1

            score = row.get('score')
            if isinstance(score, (int, float)):
                scores.append(score)
                subject_stats['scores'].append(score)

        subject_payload = {}
        for subject_key, subject_stats in subjects.items():
            avg_score = round(sum(subject_stats['scores']) / len(subject_stats['scores']), 1) if subject_stats['scores'] else None
            subject_payload[subject_key] = {
                'total': subject_stats['total'],
                'avg_score': avg_score,
            }

        return jsonify({
            'total_data': len(rows),
            'completed': status_counts['completed'],
            'processing': status_counts['processing'],
            'failed': status_counts['failed'],
            'pending': status_counts['pending'],
            'average_score': round(sum(scores) / len(scores), 1) if scores else None,
            'high_score': max(scores) if scores else None,
            'low_score': min(scores) if scores else None,
            'subjects': subject_payload,
        }), 200

    if not teacher_db_id:
        return jsonify({
            'total_data': 0,
            'completed': 0,
            'processing': 0,
            'failed': 0,
            'pending': 0,
            'average_score': None,
            'high_score': None,
            'low_score': None,
            'subjects': {},
        }), 200

    answer_sheets = AnswerSheet.query.filter_by(teacher_id=teacher_db_id).all()
    status_counts = {
        'completed': 0,
        'processing': 0,
        'failed': 0,
        'pending': 0,
    }

    latest_analysis = _latest_analysis_by_answer_sheet([row.id for row in answer_sheets])
    subject_stats_map = {}
    score_values = []

    for row in answer_sheets:
        row_status = (row.status or 'pending').lower()
        if row_status in status_counts:
            status_counts[row_status] += 1
        else:
            status_counts['pending'] += 1

        subject_key = row.subject or 'unknown'
        subject_stats = subject_stats_map.setdefault(subject_key, {'total': 0, 'scores': []})
        subject_stats['total'] += 1

        analysis_result = latest_analysis.get(row.id)
        if analysis_result and isinstance(analysis_result.score, (int, float)):
            score_values.append(analysis_result.score)
            subject_stats['scores'].append(analysis_result.score)

    subjects = {}
    for subject_key, values in subject_stats_map.items():
        avg_score = round(sum(values['scores']) / len(values['scores']), 1) if values['scores'] else None
        subjects[subject_key] = {
            'total': values['total'],
            'avg_score': avg_score,
        }

    return jsonify({
        'total_data': len(answer_sheets),
        'completed': status_counts['completed'],
        'processing': status_counts['processing'],
        'failed': status_counts['failed'],
        'pending': status_counts['pending'],
        'average_score': round(sum(score_values) / len(score_values), 1) if score_values else None,
        'high_score': max(score_values) if score_values else None,
        'low_score': min(score_values) if score_values else None,
        'subjects': subjects,
    }), 200


# ==================== レポート生成（Langchain統合） ====================
@analysis_bp.route('/generate-report', methods=['POST'])
@login_required
def generate_report():
    """LangChain を使用してレポートを生成"""
    data = request.get_json()
    
    data_id = data.get('data_id')
    report_type = data.get('report_type', 'analysis')  # analysis, study_plan, both
    
    if not data_id:
        return jsonify({'error': 'data_id is required'}), 400
    
    teacher_id, teacher_db_id = _resolve_teacher_ids()
    firebase_service = get_firebase_service()

    if firebase_service.enabled:
        student_data = firebase_service.get_answer_sheet(data_id)
        if student_data and student_data.get('teacher_id') != teacher_id:
            student_data = None
    else:
        if not teacher_db_id:
            student_data = None
        else:
            answer_sheet = AnswerSheet.query.filter_by(id=data_id, teacher_id=teacher_db_id).first()
            if not answer_sheet:
                student_data = None
            else:
                analysis_result = AnalysisResult.query.filter_by(answer_sheet_id=answer_sheet.id).order_by(AnalysisResult.created_at.desc()).first()
                student_data = {
                    'id': answer_sheet.id,
                    'test_name': answer_sheet.test_name,
                    'subject': answer_sheet.subject,
                    'student_name': analysis_result.student_name if analysis_result else '未設定の生徒',
                    'score': analysis_result.score if analysis_result and analysis_result.score is not None else 0,
                    'status': answer_sheet.status,
                    'questions': analysis_result.total_questions if analysis_result and analysis_result.total_questions is not None else 20,
                    'correct_answers': analysis_result.correct_count if analysis_result and analysis_result.correct_count is not None else 0,
                    'error_patterns': _deserialize_error_patterns(analysis_result.error_patterns) if analysis_result else [],
                    'weak_areas': ['基礎分野の再確認'],
                    'processing_time': analysis_result.processing_time if analysis_result else '',
                }
    
    if not student_data:
        return jsonify({'error': 'Data not found', 'data_id': data_id}), 404
    
    try:
        # LangChain レポート生成
        generator = get_report_generator()
        
        result = {
            'data_id': data_id,
            'student_name': student_data.get('student_name'),
            'test_name': student_data.get('test_name'),
            'subject': student_data.get('subject'),
            'score': student_data.get('score'),
            'timestamp': None
        }
        
        if report_type in ['analysis', 'both']:
            result['analysis_report'] = generator.generate_analysis_report(student_data)
        
        if report_type in ['study_plan', 'both']:
            result['study_plan'] = generator.generate_study_plan(student_data)

        if firebase_service.enabled:
            try:
                firebase_service.save_analysis_result(
                    answer_sheet_id=data_id,
                    student_name=student_data.get('student_name', ''),
                    score=student_data.get('score'),
                    correct_count=student_data.get('correct_answers'),
                    total_questions=student_data.get('questions'),
                    error_patterns=student_data.get('error_patterns', []),
                    analysis_text=result.get('analysis_report', ''),
                    study_plan=result.get('study_plan', ''),
                    processing_time=student_data.get('processing_time', '')
                )
            except Exception:
                pass
        else:
            analysis_text = result.get('analysis_report', '') if report_type in ['analysis', 'both'] else ''
            study_plan = result.get('study_plan', '') if report_type in ['study_plan', 'both'] else ''
            error_patterns = student_data.get('error_patterns', [])
            if not isinstance(error_patterns, str):
                error_patterns = json.dumps(error_patterns, ensure_ascii=False)

            db.session.add(AnalysisResult(
                answer_sheet_id=data_id,
                student_name=student_data.get('student_name') or '未設定の生徒',
                score=student_data.get('score'),
                correct_count=student_data.get('correct_answers'),
                total_questions=student_data.get('questions'),
                error_patterns=error_patterns,
                analysis_text=analysis_text,
                study_plan=study_plan,
                processing_time=student_data.get('processing_time') or 'レポート生成',
                status='completed',
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ))
            db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Report generated successfully',
            'report': result
        }), 200
    
    except ValueError as e:
        # API キー未設定エラー
        return jsonify({
            'error': 'API configuration error',
            'details': str(e),
            'hint': 'OPENAI_API_KEY environment variable must be set'
        }), 500

    except Exception as e:
        error_message = str(e)

        if 'API key not valid' in error_message or 'API_KEY_INVALID' in error_message or 'Incorrect API key' in error_message or '401' in error_message:
            return jsonify({
                'error': 'Failed to generate report',
                'details': 'OPENAI_API_KEY is invalid for the OpenAI API.',
                'hint': 'Use a valid API key from OpenAI and replace the current OPENAI_API_KEY value.'
            }), 500
    
        # その他のエラー
        return jsonify({
            'error': 'Failed to generate report',
            'details': error_message,
            'traceback': traceback.format_exc()
        }), 500


# ==================== エクスポート ====================
@analysis_bp.route('/export', methods=['POST'])
@login_required
def export_data():
    """データをエクスポート"""
    data = request.get_json() or {}

    export_format = (data.get('format', 'csv') or 'csv').lower()
    filters = data.get('filters', {}) or {}

    if export_format not in ('csv', 'json'):
        return jsonify({'error': 'Unsupported format. Use csv or json.'}), 400

    teacher_id, teacher_db_id = _resolve_teacher_ids()
    firebase_service = get_firebase_service()

    test_name = str(filters.get('test_name', '')).strip().lower()
    subject = str(filters.get('subject', '')).strip()
    status = str(filters.get('status', '')).strip()
    student_id = str(filters.get('student_id', '')).strip()
    upload_date = str(filters.get('upload_date', '')).strip()
    data_id = str(filters.get('data_id', '')).strip()

    if firebase_service.enabled:
        rows = firebase_service.list_answer_sheets(teacher_id=teacher_id)
    else:
        if not teacher_db_id:
            rows = []
        else:
            local_rows = AnswerSheet.query.filter_by(teacher_id=teacher_db_id).order_by(AnswerSheet.created_at.desc()).all()
            latest_analysis = _latest_analysis_by_answer_sheet([row.id for row in local_rows])
            rows = [_sheet_to_response_payload(row, latest_analysis.get(row.id)) for row in local_rows]

    export_rows = _apply_row_filters(
        rows,
        test_name=test_name,
        subject=subject,
        status=status,
        student_id=student_id,
        upload_date=upload_date,
        data_id=data_id,
    )

    if export_format == 'json':
        return jsonify({
            'success': True,
            'format': 'json',
            'count': len(export_rows),
            'data': export_rows,
            'message': f'{len(export_rows)}件をJSON形式でエクスポートしました',
        }), 200

    csv_text = _rows_to_csv_text(export_rows)
    return jsonify({
        'success': True,
        'format': 'csv',
        'count': len(export_rows),
        'csv': csv_text,
        'message': f'{len(export_rows)}件をCSV形式でエクスポートしました',
    }), 200
