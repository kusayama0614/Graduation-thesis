# ==================== アプリケーション初期化 ====================
"""
Flask アプリケーション初期化モジュール
"""
from flask import Flask
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_session import Session
import os
from dotenv import load_dotenv

# 環境変数の読み込み
load_dotenv()

# データベースの初期化
db = SQLAlchemy()

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
        origins=[
            'http://127.0.0.1:8000',
            'http://localhost:8000',
            'http://10.1.55.211:8000'
        ]
    )
    Session(app)
    
    # ==================== データベーステーブル作成 ====================
    with app.app_context():
        db.create_all()
    
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
    
    return app
