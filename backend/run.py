#!/usr/bin/env python
# ==================== アプリケーション起動ファイル ====================
"""
Flaskアプリケーションの起動スクリプト
使用方法: python run.py
"""
import os
import sys
from dotenv import load_dotenv

# 環境変数を読み込み
load_dotenv()

# アプリケーションを作成
from app import create_app

app = create_app()

if __name__ == '__main__':
    # 開発環境での起動
    debug_mode = os.getenv('FLASK_ENV', 'production') == 'development'
    port = int(os.getenv('PORT', 5000))
    
    print('=' * 60)
    print('🚀 Graduation Thesis System - Backend')
    print('=' * 60)
    print(f'Environment: {os.getenv("FLASK_ENV", "production")}')
    print(f'Debug Mode: {debug_mode}')
    print(f'Running on: http://localhost:{port}')
    print('=' * 60)
    
    app.run(
        host='0.0.0.0',
        port=port,
        debug=debug_mode,
        use_reloader=debug_mode
    )
