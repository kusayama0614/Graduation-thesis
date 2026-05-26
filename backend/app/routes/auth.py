# ==================== 認証ルート ====================
"""
認証関連のAPI エンドポイント
"""
from flask import Blueprint, request, jsonify, session
from app import db
from app.models import Teacher
from werkzeug.security import generate_password_hash, check_password_hash
import os
from functools import wraps

auth_bp = Blueprint('auth', __name__)

# ==================== ログイン確認デコレータ ====================
def login_required(f):
    """ログイン確認用デコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'teacher_id' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated_function


# ==================== ログイン ====================
@auth_bp.route('/login', methods=['POST'])
def login():
    """教師のログイン処理"""
    data = request.get_json()
    
    # バリデーション
    if not data or not data.get('teacher_id') or not data.get('password'):
        return jsonify({'error': 'Missing credentials'}), 400
    
    teacher_id = data.get('teacher_id', '').strip()
    password = data.get('password')
    
    # 入力チェック
    if len(teacher_id) < 3:
        return jsonify({'error': 'Teacher ID must be at least 3 characters'}), 400
    
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    
    # ==================== デモ用ハードコード ====================
    # 本来はデータベースから取得してハッシュと比較
    DEMO_CREDENTIALS = {
        'teacher001': 'password123',
        'teacher002': 'password456'
    }
    
    if teacher_id in DEMO_CREDENTIALS and DEMO_CREDENTIALS[teacher_id] == password:
        # セッションに保存
        session['teacher_id'] = teacher_id
        session.permanent = True
        
        return jsonify({
            'success': True,
            'message': 'Login successful',
            'teacher_id': teacher_id
        }), 200
    
    return jsonify({'error': 'Invalid credentials'}), 401


# ==================== ログアウト ====================
@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    """教師のログアウト処理"""
    session.clear()
    return jsonify({'success': True, 'message': 'Logout successful'}), 200


# ==================== セッション確認 ====================
@auth_bp.route('/session', methods=['GET'])
def check_session():
    """現在のセッション情報を確認"""
    if 'teacher_id' in session:
        return jsonify({
            'authenticated': True,
            'teacher_id': session['teacher_id']
        }), 200
    
    return jsonify({
        'authenticated': False
    }), 200


# ==================== 登録（開発用） ====================
@auth_bp.route('/register', methods=['POST'])
def register():
    """教師の登録（デモ用）"""
    data = request.get_json()
    
    # バリデーション
    if not data or not data.get('teacher_id') or not data.get('password'):
        return jsonify({'error': 'Missing required fields'}), 400
    
    teacher_id = data.get('teacher_id', '').strip()
    password = data.get('password')
    name = data.get('name', teacher_id)
    email = data.get('email', f'{teacher_id}@example.com')
    
    # 入力チェック
    if len(teacher_id) < 3:
        return jsonify({'error': 'Teacher ID must be at least 3 characters'}), 400
    
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    
    # ==================== デモ用レスポンス ====================
    # 本来はデータベースに保存
    return jsonify({
        'success': True,
        'message': 'Registration successful',
        'teacher_id': teacher_id
    }), 201


# ==================== パスワード変更 ====================
@auth_bp.route('/change-password', methods=['POST'])
@login_required
def change_password():
    """パスワードの変更"""
    data = request.get_json()
    
    if not data or not data.get('old_password') or not data.get('new_password'):
        return jsonify({'error': 'Missing required fields'}), 400
    
    # 本来はデータベースから取得して確認
    return jsonify({
        'success': True,
        'message': 'Password changed successfully'
    }), 200
