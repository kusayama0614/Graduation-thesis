# ==================== 分析ルート ====================
"""
データ分析関連のAPI エンドポイント（Langchain統合）
"""
from flask import Blueprint, request, jsonify, session
from app.routes.auth import login_required
from app.utils.langchain_analyzer import get_report_generator
from app.utils.firebase_service import get_firebase_service
import json
import traceback

analysis_bp = Blueprint('analysis', __name__)


# ==================== 解答データ一覧取得 ====================
@analysis_bp.route('/answer-data', methods=['GET'])
@login_required
def get_answer_data():
    """解答データの一覧を取得"""
    teacher_id = session.get('teacher_id')
    
    # フィルターパラメータ
    test_name = request.args.get('test_name', '').lower()
    subject = request.args.get('subject', '')
    status = request.args.get('status', '')
    
    firebase_service = get_firebase_service()

    if firebase_service.enabled:
        firebase_rows = firebase_service.list_answer_sheets(teacher_id=teacher_id)
        filtered_data = firebase_rows

        if test_name:
            filtered_data = [d for d in filtered_data if test_name in (d.get('test_name', '').lower())]

        if subject:
            filtered_data = [d for d in filtered_data if d.get('subject') == subject]

        if status:
            filtered_data = [d for d in filtered_data if d.get('status') == status]

        return jsonify({
            'total': len(firebase_rows),
            'filtered': len(filtered_data),
            'data': filtered_data
        }), 200

    # ==================== デモデータ ====================
    dummy_data = [
        {
            'id': '1',
            'test_name': '2026年5月 中間テスト',
            'subject': '数学',
            'student_name': '山田太郎',
            'score': 85,
            'status': 'completed',
            'upload_date': '2026-05-24',
            'processing_time': '2分30秒',
            'questions': 20,
            'correct_answers': 17
        },
        {
            'id': '2',
            'test_name': '2026年5月 中間テスト',
            'subject': '英語',
            'student_name': '佐藤花子',
            'score': 92,
            'status': 'completed',
            'upload_date': '2026-05-24',
            'processing_time': '1分45秒',
            'questions': 25,
            'correct_answers': 23
        },
        {
            'id': '3',
            'test_name': '2026年5月 確認テスト',
            'subject': '数学',
            'student_name': '田中次郎',
            'score': None,
            'status': 'processing',
            'upload_date': '2026-05-24',
            'processing_time': '処理中...',
            'questions': 20,
            'correct_answers': None
        },
        {
            'id': '4',
            'test_name': '2026年4月 実力テスト',
            'subject': '国語',
            'student_name': '鈴木美咲',
            'score': 78,
            'status': 'completed',
            'upload_date': '2026-05-23',
            'processing_time': '3分15秒',
            'questions': 15,
            'correct_answers': 12
        }
    ]
    
    # フィルター適用
    filtered_data = dummy_data
    
    if test_name:
        filtered_data = [d for d in filtered_data 
                        if test_name in d['test_name'].lower()]
    
    if subject:
        filtered_data = [d for d in filtered_data if d['subject'] == subject]
    
    if status:
        filtered_data = [d for d in filtered_data if d['status'] == status]
    
    return jsonify({
        'total': len(dummy_data),
        'filtered': len(filtered_data),
        'data': filtered_data
    }), 200


# ==================== 解答データ詳細取得 ====================
@analysis_bp.route('/answer-data/<data_id>', methods=['GET'])
@login_required
def get_answer_data_detail(data_id):
    """解答データの詳細を取得"""
    firebase_service = get_firebase_service()

    if firebase_service.enabled:
        answer_sheet = firebase_service.get_answer_sheet(data_id)
        if answer_sheet:
            return jsonify(answer_sheet), 200

    # ==================== デモデータ ====================
    dummy_details = {
        '1': {
            'id': '1',
            'test_name': '2026年5月 中間テスト',
            'subject': '数学',
            'student_name': '山田太郎',
            'score': 85,
            'status': 'completed',
            'upload_date': '2026-05-24',
            'processing_time': '2分30秒',
            'questions': 20,
            'correct_answers': 17,
            'error_patterns': [
                {'pattern': '計算ミス', 'count': 2, 'examples': ['問題3', '問題7']},
                {'pattern': '概念理解不足', 'count': 1, 'examples': ['問題15']}
            ],
            'analysis': '基本的な計算能力は高いが、複雑な問題では注意散漫が見られます。',
            'study_plan': '1. 計算練習を毎日15分\n2. 複雑な問題の段階的解法練習'
        }
    }
    
    if data_id in dummy_details:
        return jsonify(dummy_details[data_id]), 200
    
    return jsonify({'error': 'Data not found'}), 404


# ==================== 統計情報取得 ====================
@analysis_bp.route('/statistics', methods=['GET'])
@login_required
def get_statistics():
    """統計情報を取得"""
    teacher_id = session.get('teacher_id')
    
    # ==================== デモデータ ====================
    stats = {
        'total_data': 24,
        'completed': 18,
        'processing': 3,
        'failed': 1,
        'pending': 2,
        'average_score': 82.5,
        'high_score': 95,
        'low_score': 65,
        'subjects': {
            'math': {'total': 8, 'avg_score': 81},
            'english': {'total': 6, 'avg_score': 84},
            'japanese': {'total': 5, 'avg_score': 79},
            'science': {'total': 4, 'avg_score': 86},
            'social': {'total': 1, 'avg_score': 90}
        }
    }
    
    return jsonify(stats), 200


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
    
    firebase_service = get_firebase_service()

    if firebase_service.enabled:
        student_data = firebase_service.get_answer_sheet(data_id)
    else:
        student_data = None

    # ==================== デモデータ（実装例） ====================
    # 本来はデータベースから取得
    demo_answer_data = {
        '1': {
            'id': '1',
            'test_name': '2026年5月 中間テスト',
            'subject': '数学',
            'student_name': '山田太郎',
            'score': 85,
            'status': 'completed',
            'upload_date': '2026-05-24',
            'processing_time': '2分30秒',
            'questions': 20,
            'correct_answers': 17,
            'error_patterns': [
                {'pattern': '計算ミス', 'count': 2, 'examples': ['問題3', '問題7']},
                {'pattern': '概念理解不足', 'count': 1, 'examples': ['問題15']}
            ],
            'weak_areas': ['方程式の応用問題', '図形問題の空間認識']
        },
        '2': {
            'id': '2',
            'test_name': '2026年5月 中間テスト',
            'subject': '英語',
            'student_name': '佐藤花子',
            'score': 92,
            'status': 'completed',
            'questions': 25,
            'correct_answers': 23,
            'error_patterns': [
                {'pattern': 'スペルミス', 'count': 1, 'examples': ['問題8']},
                {'pattern': 'リーディング誤読', 'count': 1, 'examples': ['問題18']}
            ],
            'weak_areas': ['複雑な文法構造の理解']
        }
    }
    
    if not student_data:
        student_data = demo_answer_data.get(data_id)
    
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
            'hint': 'GOOGLE_API_KEY environment variable must be set'
        }), 500

    except Exception as e:
        error_message = str(e)

        if 'API key not valid' in error_message or 'API_KEY_INVALID' in error_message:
            return jsonify({
                'error': 'Failed to generate report',
                'details': 'GOOGLE_API_KEY is invalid for Gemini API.',
                'hint': 'Use a valid API key from Google AI Studio and replace the current GOOGLE_API_KEY value.'
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
    data = request.get_json()
    
    export_format = data.get('format', 'csv')  # csv, json, pdf
    filters = data.get('filters', {})
    
    # ==================== TODO: エクスポート実装 ====================
    
    return jsonify({
        'success': True,
        'message': f'Export as {export_format} started',
        'status': 'processing'
    }), 202
