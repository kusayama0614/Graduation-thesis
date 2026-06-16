#!/usr/bin/env python
# ==================== アプリケーション起動ファイル ====================
"""
Flaskアプリケーションの起動スクリプト
使用方法: python run.py
"""
import os
import sys
import socket
from dotenv import load_dotenv

# 環境変数を読み込み
load_dotenv()

# アプリケーションを作成
from app import create_app

app = create_app()


def get_local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('8.8.8.8', 80))
            return sock.getsockname()[0]
    except OSError:
        return '127.0.0.1'


def find_available_port(preferred_port):
    for port in range(preferred_port, preferred_port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if sock.connect_ex(('127.0.0.1', port)) != 0:
                return port
    return preferred_port


def get_runtime_port(preferred_port):
    assigned_port = os.getenv('APP_PORT')
    if assigned_port:
        return int(assigned_port)

    port = find_available_port(preferred_port)
    os.environ['APP_PORT'] = str(port)
    return port

if __name__ == '__main__':
    # 開発環境での起動
    debug_mode = os.getenv('FLASK_ENV', 'production') == 'development'
    preferred_port = int(os.getenv('PORT', 5000))
    port = get_runtime_port(preferred_port)
    
    print('=' * 60)
    print('🚀 Graduation Thesis System - Backend')
    print('=' * 60)
    print(f'Environment: {os.getenv("FLASK_ENV", "production")}')
    print(f'Debug Mode: {debug_mode}')
    print(f'Running on: http://localhost:{port}')
    print(f'LAN URL: http://{get_local_ip()}:{port}/screens/login/login.html')
    if port != preferred_port:
        print(f'Preferred port {preferred_port} was busy; using {port} instead.')
    print('=' * 60)
    
    app.run(
        host='0.0.0.0',
        port=port,
        debug=debug_mode,
        use_reloader=debug_mode
    )
