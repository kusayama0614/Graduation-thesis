# ==================== アプリケーション初期化 ====================
"""
Flask アプリケーション初期化モジュール
"""
from flask import Flask, redirect, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_session import Session
import os
import re
import socket
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

# 環境変数の読み込み
load_dotenv()

# データベースの初期化
db = SQLAlchemy()


def _get_screens_dir():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    return os.path.join(repo_root, 'screens')


def _get_local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('8.8.8.8', 80))
            return sock.getsockname()[0]
    except OSError:
        return '127.0.0.1'


def _get_host_ip():
    return os.getenv('HOST_IP', '').strip() or _get_local_ip()


def _seed_default_teachers():
    from app.models import Teacher
    from app.utils.firebase_service import get_firebase_service

    env_name = os.getenv('FLASK_ENV', 'production').strip().lower()
    demo_seed_enabled = os.getenv('ENABLE_DEMO_TEACHERS', '').strip().lower() in ('1', 'true', 'yes')
    if env_name != 'development' and not demo_seed_enabled:
        return

    default_teachers = [
        {
            'teacher_id': 'teacher001',
            'password': 'password123',
            'name': '山田先生',
            'email': 'teacher001@example.com'
        },
        {
            'teacher_id': 'teacher002',
            'password': 'password456',
            'name': '佐藤先生',
            'email': 'teacher002@example.com'
        }
    ]

    for teacher_info in default_teachers:
        existing_teacher = Teacher.query.filter_by(teacher_id=teacher_info['teacher_id']).first()
        if existing_teacher:
            continue

        db.session.add(Teacher(
            teacher_id=teacher_info['teacher_id'],
            password_hash=generate_password_hash(teacher_info['password']),
            name=teacher_info['name'],
            email=teacher_info['email']
        ))

    db.session.commit()

    firebase_service = get_firebase_service()
    if firebase_service.enabled:
        try:
            firebase_service.seed_default_teachers(default_teachers)
        except Exception:
            # Firestore 権限が未整備でもアプリ起動を止めない
            pass

def create_app():
    """Flask アプリケーションファクトリ"""
    app = Flask(__name__)
    
    # ==================== 設定 ====================
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-key-change-in-production')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
        'DATABASE_URL', 
        'sqlite:///graduation_system.db'
    )
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    # セッション設定
    app.config['SESSION_TYPE'] = os.getenv('SESSION_TYPE', 'filesystem')
    app.config['PERMANENT_SESSION_LIFETIME'] = int(
        os.getenv('PERMANENT_SESSION_LIFETIME', 3600)
    )
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    
    # ファイルアップロード設定
    app.config['UPLOAD_FOLDER'] = os.getenv('UPLOAD_FOLDER', 'uploads')
    app.config['MAX_CONTENT_LENGTH'] = int(
        os.getenv('MAX_CONTENT_LENGTH', 52428800)
    )
    
    # ==================== アップロードフォルダ作成 ====================
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    
    # ==================== 拡張機能の初期化 ====================
    db.init_app(app)
    CORS(
        app,
        supports_credentials=True,
        origins=re.compile(r'^http://(localhost|127\.0\.0\.1|[A-Za-z0-9.-]+):8000$')
    )
    Session(app)
    
    # ==================== データベーステーブル作成 ====================
    with app.app_context():
        from app import models  # noqa: F401
        db.create_all()
        _seed_default_teachers()
    
    # ==================== ブループリント登録 ====================
    from app.routes.auth import auth_bp
    from app.routes.upload import upload_bp
    from app.routes.analysis import analysis_bp
    
    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    app.register_blueprint(upload_bp, url_prefix='/api/upload')
    app.register_blueprint(analysis_bp, url_prefix='/api/analysis')
    
    # ==================== エラーハンドラ ====================
    @app.errorhandler(400)
    def bad_request(error):
        return {'error': 'Bad request'}, 400
    
    @app.errorhandler(404)
    def not_found(error):
        return {'error': 'Not found'}, 404
    
    @app.errorhandler(500)
    def internal_error(error):
        return {'error': 'Internal server error'}, 500
    
    # ==================== ヘルスチェック ====================
    @app.route('/health', methods=['GET'])
    def health_check():
        return {'status': 'ok'}, 200

    @app.route('/health/firebase', methods=['GET'])
    def firebase_health_check():
        from app.utils.firebase_service import get_firebase_service

        firebase_service = get_firebase_service()
        status = firebase_service.status()

        return {
            'status': 'ok' if status['enabled'] else 'not_configured',
            'firebase': status
        }, 200

    @app.route('/network-url', methods=['GET'])
    def network_url():
        ip_address = _get_host_ip()
        runtime_port = os.getenv('APP_PORT') or os.getenv('PORT', '5000')
        return {
            'host': ip_address,
            'url': f'http://{ip_address}:{runtime_port}/screens/login/login.html'
        }, 200

    # ==================== フロントエンド配信 ====================
    @app.route('/')
    def index():
        return redirect('/screens/login/login.html')

    @app.route('/screens/<path:filename>')
    def screens(filename):
        return send_from_directory(_get_screens_dir(), filename)
    
    return app
