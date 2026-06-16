# ==================== 認証ルート ====================
"""
認証関連のAPI エンドポイント
"""
from flask import Blueprint, request, jsonify, session
from app import db
from app.models import Teacher
from app.utils.firebase_service import get_firebase_service
from werkzeug.security import generate_password_hash, check_password_hash
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

    firebase_service = get_firebase_service()
    teacher = None
    if firebase_service.enabled:
        try:
            teacher = firebase_service.authenticate_teacher(teacher_id, password)
        except Exception:
            teacher = None

    if teacher:
        session['teacher_id'] = teacher['teacher_id']
        session['teacher_db_id'] = teacher.get('id', teacher['teacher_id'])
        session['teacher_name'] = teacher.get('name')
        session.permanent = True

        return jsonify({
            'success': True,
            'message': 'Login successful',
            'teacher_id': teacher['teacher_id'],
            'name': teacher.get('name')
        }), 200

    teacher = Teacher.query.filter_by(teacher_id=teacher_id).first()
    if not teacher or not check_password_hash(teacher.password_hash, password):
        return jsonify({'error': 'Invalid credentials'}), 401

    session['teacher_id'] = teacher.teacher_id
    session['teacher_db_id'] = teacher.id
    session['teacher_name'] = teacher.name
    session.permanent = True

    return jsonify({
        'success': True,
        'message': 'Login successful',
        'teacher_id': teacher.teacher_id,
        'name': teacher.name
    }), 200


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
            'teacher_id': session['teacher_id'],
            'name': session.get('teacher_name')
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

    firebase_service = get_firebase_service()

    if firebase_service.enabled:
        try:
            firebase_service.create_teacher(teacher_id, password, name, email)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 409
    else:
        if Teacher.query.filter_by(teacher_id=teacher_id).first():
            return jsonify({'error': 'Teacher ID already exists'}), 409

        hashed_password = generate_password_hash(password)
        teacher = Teacher(
            teacher_id=teacher_id,
            password_hash=hashed_password,
            name=name,
            email=email
        )

        db.session.add(teacher)
        db.session.commit()

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
    
    teacher_id = session.get('teacher_id')
    firebase_service = get_firebase_service()

    if firebase_service.enabled:
        try:
            teacher = firebase_service.authenticate_teacher(teacher_id, data['old_password'])
            if not teacher:
                return jsonify({'error': 'Old password is incorrect'}), 400

            firebase_service.update_password(teacher_id, data['new_password'])
        except Exception:
            teacher = Teacher.query.filter_by(teacher_id=teacher_id).first()

            if not teacher or not check_password_hash(teacher.password_hash, data['old_password']):
                return jsonify({'error': 'Old password is incorrect'}), 400

            teacher.password_hash = generate_password_hash(data['new_password'])
            db.session.commit()
    else:
        teacher = Teacher.query.filter_by(teacher_id=teacher_id).first()

        if not teacher or not check_password_hash(teacher.password_hash, data['old_password']):
            return jsonify({'error': 'Old password is incorrect'}), 400

        teacher.password_hash = generate_password_hash(data['new_password'])
        db.session.commit()

    return jsonify({
        'success': True,
        'message': 'Password changed successfully'
    }), 200
