# ==================== 分析ルート ====================
"""
データ分析関連のAPI エンドポイント（Langchain統合）
"""
from flask import Blueprint, request, session, Response, current_app
from app import db
from app.models import AnswerSheet, AnalysisResult, ProcessingLog, Teacher
from app.routes.auth import login_required
from app.utils.api_response import make_error_response, make_success_response
from app.utils.answer_sheet_processor import start_answer_sheet_processing
from app.utils.langchain_analyzer import get_report_generator
from app.utils.firebase_service import get_firebase_service
from app.utils.time_utils import utc_now
import json
import traceback
from datetime import datetime
import io
import csv
from uuid import uuid4

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

    processing_options = {}
    if sheet.processing_options:
        try:
            processing_options = json.loads(sheet.processing_options)
        except Exception:
            processing_options = {}

    completed_steps = []
    if sheet.completed_steps:
        try:
            parsed_steps = json.loads(sheet.completed_steps)
            if isinstance(parsed_steps, list):
                completed_steps = parsed_steps
        except Exception:
            completed_steps = []

    error_patterns = _deserialize_error_patterns(sheet.error_patterns)
    if not error_patterns and analysis_result:
        error_patterns = _deserialize_error_patterns(analysis_result.error_patterns)

    processing_time = '---'
    if analysis_result and analysis_result.processing_time:
        processing_time = analysis_result.processing_time
    upload_date = sheet.created_at.date().isoformat() if sheet.created_at else None

    payload = {
        'id': sheet.id,
        'test_name': sheet.test_name,
        'subject': sheet.subject,
        'student_id': sheet.student_id,
        'student_name': analysis_result.student_name if analysis_result and analysis_result.student_name else None,
        'score': analysis_result.score if analysis_result and analysis_result.score is not None else sheet.score,
        'status': sheet.status,
        'upload_date': upload_date,
        'processing_time': processing_time,
        'questions': analysis_result.total_questions if analysis_result and analysis_result.total_questions is not None else sheet.total_questions,
        'correct_answers': analysis_result.correct_count if analysis_result and analysis_result.correct_count is not None else sheet.correct_count,
        'error_patterns': error_patterns,
        'analysis': analysis_result.analysis_text if analysis_result else None,
        'study_plan': analysis_result.study_plan if analysis_result else None,
        'analysis_result_id': analysis_result.id if analysis_result else None,
        'exam_date': sheet.exam_date.isoformat() if sheet.exam_date else None,
        'file_name': sheet.file_name,
        'file_size': sheet.file_size,
        'processing_options': processing_options,
        'processing_stage': sheet.processing_stage or status_defaults.get('processing_stage'),
        'processing_job_id': sheet.processing_job_id,
        'current_step': sheet.current_step or status_defaults.get('current_step'),
        'completed_steps': completed_steps if completed_steps else status_defaults.get('completed_steps'),
        'progress_percent': sheet.progress_percent if sheet.progress_percent is not None else status_defaults.get('progress_percent'),
        'processing_message': sheet.processing_message or status_defaults.get('processing_message'),
        'last_error': sheet.last_error,
    }
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


def _project_export_rows(rows, profile):
    profile_value = (profile or 'detailed').lower()
    if profile_value == 'minimal':
        keys = [
            'id',
            'test_name',
            'subject',
            'student_id',
            'status',
            'score',
            'upload_date',
        ]
    else:
        keys = [
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
            'processing_job_id',
            'current_step',
            'progress_percent',
            'processing_message',
            'last_error',
        ]

    projected = []
    for row in rows:
        projected.append({key: row.get(key) for key in keys})
    return projected, keys


def _rows_to_csv_text(rows, headers):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)

    for row in rows:
        writer.writerow([row.get(header) for header in headers])

    return output.getvalue()


def _parse_positive_int(raw_value, default_value, min_value=1, max_value=500):
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = default_value
    return max(min_value, min(max_value, value))


def _parse_sortable_datetime(raw_value):
    if not raw_value:
        return None

    if isinstance(raw_value, datetime):
        return raw_value

    if isinstance(raw_value, str):
        text_value = raw_value.strip()
        if not text_value:
            return None
        if text_value.endswith('Z'):
            text_value = text_value[:-1] + '+00:00'
        try:
            return datetime.fromisoformat(text_value)
        except ValueError:
            return None

    return None


def _parse_error_tags(raw_value):
    if isinstance(raw_value, str) and raw_value.strip():
        try:
            raw_value = json.loads(raw_value)
        except Exception:
            return []

    if not isinstance(raw_value, list):
        return []

    tags = []
    for item in raw_value:
        if isinstance(item, dict):
            tag_name = str(item.get('pattern') or item.get('tag') or '').strip()
            if tag_name:
                tag_count = item.get('count', 1)
                try:
                    tag_count = int(tag_count)
                except (TypeError, ValueError):
                    tag_count = 1
                tags.append((tag_name, max(1, tag_count)))
        elif isinstance(item, str):
            tag_name = item.strip()
            if tag_name:
                tags.append((tag_name, 1))

    return tags


def _coerce_score(raw_score):
    if isinstance(raw_score, (int, float)):
        return float(raw_score)
    try:
        if raw_score is None:
            return None
        return float(raw_score)
    except (TypeError, ValueError):
        return None


def _parse_bool(raw_value):
    if isinstance(raw_value, bool):
        return raw_value
    return str(raw_value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _paginate_rows(rows, page, per_page):
    total_items = len(rows)
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    page_value = min(page, total_pages)
    start = (page_value - 1) * per_page
    end = start + per_page
    return rows[start:end], {
        'page': page_value,
        'per_page': per_page,
        'total_pages': total_pages,
        'has_next': page_value < total_pages,
        'has_prev': page_value > 1,
        'offset': start,
    }


def _list_teacher_answer_rows(teacher_id, teacher_db_id, firebase_service):
    if firebase_service.enabled:
        return firebase_service.list_answer_sheets(teacher_id=teacher_id)

    if not teacher_db_id:
        return []

    local_rows = AnswerSheet.query.filter_by(teacher_id=teacher_db_id).order_by(AnswerSheet.created_at.desc()).all()
    latest_analysis = _latest_analysis_by_answer_sheet([row.id for row in local_rows])
    return [_sheet_to_response_payload(row, latest_analysis.get(row.id)) for row in local_rows]


def _build_learning_progress(rows):
    students = {}
    total_records = len(rows)
    scored_records = 0

    for index, row in enumerate(rows):
        student_key = str(row.get('student_id') or row.get('student_name') or 'UNKNOWN').strip() or 'UNKNOWN'
        student = students.setdefault(student_key, {
            'student_id': student_key,
            'student_name': row.get('student_name') or None,
            'subject_counts': {},
            'records': [],
            'tag_counts': {},
        })

        subject_key = str(row.get('subject') or 'unknown')
        student['subject_counts'][subject_key] = student['subject_counts'].get(subject_key, 0) + 1

        score_value = _coerce_score(row.get('score'))
        if score_value is not None:
            scored_records += 1

        record_date = _parse_sortable_datetime(row.get('exam_date')) or _parse_sortable_datetime(row.get('upload_date'))
        record_sort_ts = 0.0
        if record_date is not None:
            try:
                record_sort_ts = float(record_date.timestamp())
            except Exception:
                record_sort_ts = 0.0
        student['records'].append({
            'score': score_value,
            'status': str(row.get('status') or row.get('processing_stage') or 'pending').lower(),
            'record_sort_ts': record_sort_ts,
            'index': index,
        })

        for tag_name, tag_count in _parse_error_tags(row.get('error_patterns')):
            student['tag_counts'][tag_name] = student['tag_counts'].get(tag_name, 0) + tag_count

    student_rows = []
    for student in students.values():
        sorted_records = sorted(student['records'], key=lambda value: (value['record_sort_ts'], value['index']))
        scores = [record['score'] for record in sorted_records if record['score'] is not None]

        latest_record = sorted_records[-1] if sorted_records else None
        latest_score = None
        if latest_record and latest_record.get('score') is not None:
            latest_score = round(float(latest_record['score']), 1)

        average_score = round(sum(scores) / len(scores), 1) if scores else None
        improvement_delta = round(scores[-1] - scores[0], 1) if len(scores) >= 2 else None
        weak_tags = sorted(
            student['tag_counts'].items(),
            key=lambda item: (-item[1], item[0])
        )[:3]

        latest_date = latest_record['record_sort_ts'] if latest_record else 0.0
        latest_status = latest_record['status'] if latest_record else 'pending'
        at_risk = bool((latest_score is not None and latest_score < 60) or latest_status == 'failed')

        student_rows.append({
            'student_id': student['student_id'],
            'student_name': student['student_name'],
            'total_tests': len(sorted_records),
            'scored_tests': len(scores),
            'average_score': average_score,
            'latest_score': latest_score,
            'improvement_delta': improvement_delta,
            'latest_status': latest_status,
            'at_risk': at_risk,
            'subjects': student['subject_counts'],
            'weak_tags': [{'tag': tag, 'count': count} for tag, count in weak_tags],
            '_sort_latest_date': latest_date,
        })

    student_rows.sort(key=lambda row: (row['at_risk'], row['_sort_latest_date']), reverse=True)
    for row in student_rows:
        row.pop('_sort_latest_date', None)

    average_score_all = [row['average_score'] for row in student_rows if row['average_score'] is not None]
    summary = {
        'total_students': len(student_rows),
        'total_records': total_records,
        'scored_records': scored_records,
        'average_score': round(sum(average_score_all) / len(average_score_all), 1) if average_score_all else None,
        'at_risk_students': len([row for row in student_rows if row['at_risk']]),
    }
    return student_rows, summary


def _normalize_processing_options(raw_value):
    defaults = {
        'ocr_process': True,
        'auto_score': True,
        'generate_report': False,
    }

    if isinstance(raw_value, str) and raw_value.strip():
        try:
            raw_value = json.loads(raw_value)
        except Exception:
            raw_value = {}

    if not isinstance(raw_value, dict):
        raw_value = {}

    options = defaults | raw_value
    return {
        'ocr_process': bool(options.get('ocr_process', options.get('ocr', True))),
        'auto_score': bool(options.get('auto_score', True)),
        'generate_report': bool(options.get('generate_report', False)),
    }


def _serialize_processing_log(log_row):
    return {
        'id': log_row.id,
        'answer_sheet_id': log_row.answer_sheet_id,
        'step': log_row.step,
        'status': log_row.status,
        'message': log_row.message,
        'error': log_row.error,
        'created_at': log_row.created_at.isoformat() if log_row.created_at else None,
    }


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
    page = _parse_positive_int(request.args.get('page'), default_value=1, min_value=1, max_value=10000)
    per_page = _parse_positive_int(request.args.get('per_page'), default_value=20, min_value=1, max_value=200)
    
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
        paged_rows, pagination = _paginate_rows(filtered_data, page, per_page)

        return make_success_response({
            'total': len(firebase_rows),
            'filtered': len(filtered_data),
            'data': paged_rows,
            'page': pagination['page'],
            'per_page': pagination['per_page'],
            'total_pages': pagination['total_pages'],
            'has_next': pagination['has_next'],
            'has_prev': pagination['has_prev'],
        }, 200)

    if not teacher_db_id:
        return make_success_response({
            'total': 0,
            'filtered': 0,
            'data': [],
            'page': 1,
            'per_page': per_page,
            'total_pages': 1,
            'has_next': False,
            'has_prev': False,
        }, 200)

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
    paged_rows, pagination = _paginate_rows(filtered_data, page, per_page)

    return make_success_response({
        'total': len(rows),
        'filtered': len(filtered_data),
        'data': paged_rows,
        'page': pagination['page'],
        'per_page': pagination['per_page'],
        'total_pages': pagination['total_pages'],
        'has_next': pagination['has_next'],
        'has_prev': pagination['has_prev'],
    }, 200)


@analysis_bp.route('/learning-progress', methods=['GET'])
@login_required
def get_learning_progress():
    """学習進捗を生徒単位で集計して返す。"""
    teacher_id, teacher_db_id = _resolve_teacher_ids()
    firebase_service = get_firebase_service()

    subject = request.args.get('subject', '').strip()
    student_id = request.args.get('student_id', '').strip()
    at_risk_only = _parse_bool(request.args.get('at_risk_only'))
    page = _parse_positive_int(request.args.get('page'), default_value=1, min_value=1, max_value=10000)
    per_page = _parse_positive_int(request.args.get('per_page'), default_value=20, min_value=1, max_value=200)

    rows = _list_teacher_answer_rows(teacher_id, teacher_db_id, firebase_service)

    filtered_rows = _apply_row_filters(
        rows,
        subject=subject,
        student_id=student_id,
    )
    progress_rows, summary = _build_learning_progress(filtered_rows)

    if at_risk_only:
        progress_rows = [row for row in progress_rows if row.get('at_risk')]
        summary = summary | {
            'total_students': len(progress_rows),
            'at_risk_students': len(progress_rows),
        }

    paged_rows, pagination = _paginate_rows(progress_rows, page, per_page)

    return make_success_response({
        'summary': summary,
        'students': paged_rows,
        'page': pagination['page'],
        'per_page': pagination['per_page'],
        'total_students': len(progress_rows),
        'total_pages': pagination['total_pages'],
        'has_next': pagination['has_next'],
        'has_prev': pagination['has_prev'],
    }, 200)


@analysis_bp.route('/learning-progress/export', methods=['GET'])
@login_required
def export_learning_progress():
    """学習進捗をエクスポートする。"""
    teacher_id, teacher_db_id = _resolve_teacher_ids()
    firebase_service = get_firebase_service()

    export_format = (request.args.get('format', 'csv') or 'csv').lower()
    if export_format not in ('csv', 'json'):
        return make_error_response('ANALYSIS_EXPORT_UNSUPPORTED_FORMAT', 'Unsupported format. Use csv or json.', 400)

    subject = request.args.get('subject', '').strip()
    student_id = request.args.get('student_id', '').strip()
    at_risk_only = _parse_bool(request.args.get('at_risk_only'))

    rows = _list_teacher_answer_rows(teacher_id, teacher_db_id, firebase_service)
    filtered_rows = _apply_row_filters(
        rows,
        subject=subject,
        student_id=student_id,
    )
    progress_rows, summary = _build_learning_progress(filtered_rows)

    if at_risk_only:
        progress_rows = [row for row in progress_rows if row.get('at_risk')]
        summary = summary | {
            'total_students': len(progress_rows),
            'at_risk_students': len(progress_rows),
        }

    timestamp = utc_now().strftime('%Y%m%d_%H%M%S')
    if export_format == 'json':
        body = json.dumps({
            'summary': summary,
            'students': progress_rows,
        }, ensure_ascii=False, indent=2)
        filename = f'learning_progress_{timestamp}.json'
        return Response(
            body,
            status=200,
            mimetype='application/json',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
            },
        )

    csv_rows = []
    for row in progress_rows:
        weak_tags = row.get('weak_tags') or []
        weak_tags_text = ', '.join([
            f"{tag.get('tag')}({tag.get('count')})" for tag in weak_tags if tag.get('tag')
        ])
        subjects = row.get('subjects') or {}
        subjects_text = ', '.join([
            f"{name}:{count}" for name, count in sorted(subjects.items())
        ])
        csv_rows.append({
            'student_id': row.get('student_id'),
            'student_name': row.get('student_name'),
            'total_tests': row.get('total_tests'),
            'scored_tests': row.get('scored_tests'),
            'average_score': row.get('average_score'),
            'latest_score': row.get('latest_score'),
            'improvement_delta': row.get('improvement_delta'),
            'latest_status': row.get('latest_status'),
            'at_risk': row.get('at_risk'),
            'subjects': subjects_text,
            'weak_tags': weak_tags_text,
        })

    headers = [
        'student_id',
        'student_name',
        'total_tests',
        'scored_tests',
        'average_score',
        'latest_score',
        'improvement_delta',
        'latest_status',
        'at_risk',
        'subjects',
        'weak_tags',
    ]
    csv_text = _rows_to_csv_text(csv_rows, headers)
    filename = f'learning_progress_{timestamp}.csv'
    return Response(
        csv_text,
        status=200,
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
        },
    )


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

            return make_success_response(answer_sheet, 200)

    if not teacher_db_id:
        return make_error_response('ANALYSIS_NOT_FOUND', 'Data not found', 404)

    answer_sheet = AnswerSheet.query.filter_by(id=data_id, teacher_id=teacher_db_id).first()
    if not answer_sheet:
        return make_error_response('ANALYSIS_NOT_FOUND', 'Data not found', 404)

    analysis_result = AnalysisResult.query.filter_by(answer_sheet_id=data_id).order_by(AnalysisResult.created_at.desc()).first()
    return make_success_response(_sheet_to_response_payload(answer_sheet, analysis_result), 200)


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

        return make_success_response({
            'total_data': len(rows),
            'completed': status_counts['completed'],
            'processing': status_counts['processing'],
            'failed': status_counts['failed'],
            'pending': status_counts['pending'],
            'average_score': round(sum(scores) / len(scores), 1) if scores else None,
            'high_score': max(scores) if scores else None,
            'low_score': min(scores) if scores else None,
            'subjects': subject_payload,
        }, 200)

    if not teacher_db_id:
        return make_success_response({
            'total_data': 0,
            'completed': 0,
            'processing': 0,
            'failed': 0,
            'pending': 0,
            'average_score': None,
            'high_score': None,
            'low_score': None,
            'subjects': {},
        }, 200)

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
        score_value = None
        if analysis_result and isinstance(analysis_result.score, (int, float)):
            score_value = analysis_result.score
        elif isinstance(row.score, (int, float)):
            score_value = row.score

        if score_value is not None:
            score_values.append(score_value)
            subject_stats['scores'].append(score_value)

    subjects = {}
    for subject_key, values in subject_stats_map.items():
        avg_score = round(sum(values['scores']) / len(values['scores']), 1) if values['scores'] else None
        subjects[subject_key] = {
            'total': values['total'],
            'avg_score': avg_score,
        }

    return make_success_response({
        'total_data': len(answer_sheets),
        'completed': status_counts['completed'],
        'processing': status_counts['processing'],
        'failed': status_counts['failed'],
        'pending': status_counts['pending'],
        'average_score': round(sum(score_values) / len(score_values), 1) if score_values else None,
        'high_score': max(score_values) if score_values else None,
        'low_score': min(score_values) if score_values else None,
        'subjects': subjects,
    }, 200)


# ==================== レポート生成（Langchain統合） ====================
@analysis_bp.route('/generate-report', methods=['POST'])
@login_required
def generate_report():
    """LangChain を使用してレポートを生成"""
    data = request.get_json()
    
    data_id = data.get('data_id')
    report_type = data.get('report_type', 'analysis')  # analysis, study_plan, both
    
    if not data_id:
        return make_error_response('ANALYSIS_DATA_ID_REQUIRED', 'data_id is required', 400)
    
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
                    'student_name': (
                        analysis_result.student_name
                        if analysis_result and analysis_result.student_name
                        else (answer_sheet.student_id or '未設定の生徒')
                    ),
                    'score': (
                        analysis_result.score
                        if analysis_result and analysis_result.score is not None
                        else (answer_sheet.score if answer_sheet.score is not None else 0)
                    ),
                    'status': answer_sheet.status,
                    'questions': (
                        analysis_result.total_questions
                        if analysis_result and analysis_result.total_questions is not None
                        else (answer_sheet.total_questions if answer_sheet.total_questions is not None else 20)
                    ),
                    'correct_answers': (
                        analysis_result.correct_count
                        if analysis_result and analysis_result.correct_count is not None
                        else (answer_sheet.correct_count if answer_sheet.correct_count is not None else 0)
                    ),
                    'error_patterns': (
                        _deserialize_error_patterns(analysis_result.error_patterns)
                        if analysis_result
                        else _deserialize_error_patterns(answer_sheet.error_patterns)
                    ),
                    'weak_areas': ['基礎分野の再確認'],
                    'processing_time': analysis_result.processing_time if analysis_result else '',
                }
    
    if not student_data:
        return make_error_response('ANALYSIS_NOT_FOUND', 'Data not found', 404, extra={'data_id': data_id})
    
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
                created_at=utc_now(),
                updated_at=utc_now(),
            ))
            db.session.commit()
        
        return make_success_response({
            'message': 'Report generated successfully',
            'report': result
        }, 200)
    
    except ValueError as e:
        # API キー未設定エラー
        return make_error_response(
            'ANALYSIS_API_CONFIGURATION_ERROR',
            'API configuration error',
            500,
            details=str(e),
            extra={'hint': 'OPENAI_API_KEY environment variable must be set'}
        )

    except Exception as e:
        error_message = str(e)

        if 'API key not valid' in error_message or 'API_KEY_INVALID' in error_message or 'Incorrect API key' in error_message or '401' in error_message:
            return make_error_response(
                'ANALYSIS_INVALID_OPENAI_KEY',
                'Failed to generate report',
                500,
                details='OPENAI_API_KEY is invalid for the OpenAI API.',
                extra={'hint': 'Use a valid API key from OpenAI and replace the current OPENAI_API_KEY value.'}
            )
    
        # その他のエラー
        return make_error_response(
            'ANALYSIS_REPORT_GENERATION_FAILED',
            'Failed to generate report',
            500,
            details=error_message,
            extra={'traceback': traceback.format_exc()}
        )


# ==================== エクスポート ====================
@analysis_bp.route('/export', methods=['POST'])
@login_required
def export_data():
    """データをエクスポート"""
    data = request.get_json() or {}

    export_format = (data.get('format', 'csv') or 'csv').lower()
    export_profile = (data.get('profile', 'detailed') or 'detailed').lower()
    filters = data.get('filters', {}) or {}

    if export_format not in ('csv', 'json'):
        return make_error_response('ANALYSIS_EXPORT_UNSUPPORTED_FORMAT', 'Unsupported format. Use csv or json.', 400)
    if export_profile not in ('minimal', 'detailed'):
        return make_error_response('ANALYSIS_EXPORT_UNSUPPORTED_PROFILE', 'Unsupported profile. Use minimal or detailed.', 400)

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
    projected_rows, projected_headers = _project_export_rows(export_rows, export_profile)

    timestamp = utc_now().strftime('%Y%m%d_%H%M%S')

    if export_format == 'json':
        body = json.dumps(projected_rows, ensure_ascii=False, indent=2)
        filename = f'answer_data_{timestamp}.json'
        return Response(
            body,
            status=200,
            mimetype='application/json',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
            },
        )

    csv_text = _rows_to_csv_text(projected_rows, projected_headers)
    filename = f'answer_data_{timestamp}.csv'
    return Response(
        csv_text,
        status=200,
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
        },
    )


# ==================== 処理ログ取得 ====================
@analysis_bp.route('/processing-logs/<data_id>', methods=['GET'])
@login_required
def get_processing_logs(data_id):
    """処理ログを取得"""
    teacher_id, teacher_db_id = _resolve_teacher_ids()
    firebase_service = get_firebase_service()
    limit = int(request.args.get('limit', 100) or 100)
    limit = max(1, min(limit, 500))

    if firebase_service.enabled:
        sheet = firebase_service.get_answer_sheet(data_id)
        if not sheet or sheet.get('teacher_id') != teacher_id:
            return make_error_response('ANALYSIS_NOT_FOUND', 'Data not found', 404)

        logs = firebase_service.list_processing_logs_by_answer_sheet_id(data_id, limit=limit)
        return make_success_response({
            'data_id': data_id,
            'logs': logs,
        }, 200)

    if not teacher_db_id:
        return make_error_response('ANALYSIS_NOT_FOUND', 'Data not found', 404)

    sheet = AnswerSheet.query.filter_by(id=data_id, teacher_id=teacher_db_id).first()
    if not sheet:
        return make_error_response('ANALYSIS_NOT_FOUND', 'Data not found', 404)

    logs = ProcessingLog.query.filter_by(answer_sheet_id=data_id).order_by(ProcessingLog.created_at.desc()).limit(limit).all()
    return make_success_response({
        'data_id': data_id,
        'logs': [_serialize_processing_log(log) for log in logs],
    }, 200)


# ==================== 処理再実行 ====================
@analysis_bp.route('/retry-processing', methods=['POST'])
@login_required
def retry_processing():
    """失敗/完了済み処理を再実行する"""
    payload = request.get_json() or {}
    data_id = str(payload.get('data_id', '')).strip()
    override_options = payload.get('processing_options')

    if not data_id:
        return make_error_response('ANALYSIS_DATA_ID_REQUIRED', 'data_id is required', 400)

    teacher_id, teacher_db_id = _resolve_teacher_ids()
    firebase_service = get_firebase_service()
    processing_job_id = str(uuid4())

    if firebase_service.enabled:
        sheet = firebase_service.get_answer_sheet(data_id)
        if not sheet or sheet.get('teacher_id') != teacher_id:
            return make_error_response('ANALYSIS_NOT_FOUND', 'Data not found', 404)

        processing_options = _normalize_processing_options(override_options or sheet.get('processing_options'))
        answer_key = sheet.get('answer_key')

        firebase_service.update_answer_sheet(data_id, {
            'status': 'queued',
            'processing_stage': 'queued',
            'processing_job_id': processing_job_id,
            'current_step': 'queued',
            'completed_steps': [],
            'progress_percent': 0,
            'processing_message': '再実行キューに登録されました',
            'last_error': None,
            'processing_options': processing_options,
        })

        start_answer_sheet_processing(
            current_app._get_current_object(),
            data_id,
            teacher_id,
            {
                'teacher_id': teacher_id,
                'student_grade': sheet.get('student_grade'),
                'student_class': sheet.get('student_class'),
                'student_id': sheet.get('student_id'),
                'test_name': sheet.get('test_name'),
                'subject': sheet.get('subject'),
                'exam_date': sheet.get('exam_date'),
                'notes': sheet.get('notes'),
                'processing_options': processing_options,
                'processing_job_id': processing_job_id,
                'answer_key': answer_key,
            }
        )

        return make_success_response({
            'message': 'Processing retry started',
            'data_id': data_id,
            'processing_job_id': processing_job_id,
        }, 202)

    if not teacher_db_id:
        return make_error_response('ANALYSIS_NOT_FOUND', 'Data not found', 404)

    sheet = AnswerSheet.query.filter_by(id=data_id, teacher_id=teacher_db_id).first()
    if not sheet:
        return make_error_response('ANALYSIS_NOT_FOUND', 'Data not found', 404)

    processing_options = _normalize_processing_options(override_options or sheet.processing_options)

    sheet.status = 'queued'
    sheet.processing_stage = 'queued'
    sheet.processing_job_id = processing_job_id
    sheet.current_step = 'queued'
    sheet.completed_steps = json.dumps([], ensure_ascii=False)
    sheet.progress_percent = 0
    sheet.processing_message = '再実行キューに登録されました'
    sheet.last_error = None
    sheet.processing_options = json.dumps(processing_options, ensure_ascii=False)
    db.session.commit()

    answer_key = None
    if sheet.answer_key:
        try:
            answer_key = json.loads(sheet.answer_key)
        except Exception:
            answer_key = None

    start_answer_sheet_processing(
        current_app._get_current_object(),
        data_id,
        teacher_id,
        {
            'teacher_id': teacher_id,
            'student_grade': sheet.student_grade,
            'student_class': sheet.student_class,
            'student_id': sheet.student_id,
            'test_name': sheet.test_name,
            'subject': sheet.subject,
            'exam_date': sheet.exam_date.isoformat() if sheet.exam_date else None,
            'notes': sheet.notes,
            'processing_options': processing_options,
            'processing_job_id': processing_job_id,
            'answer_key': answer_key,
        }
    )

    return make_success_response({
        'message': 'Processing retry started',
        'data_id': data_id,
        'processing_job_id': processing_job_id,
    }, 202)
