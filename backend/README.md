# 🎓 Graduation Thesis System - バックエンド セットアップガイド

## 📋 プロジェクト構成

```
backend/
├── app/                          # Flaskアプリケーション本体
│   ├── __init__.py              # アプリケーション初期化
│   ├── models/
│   │   └── __init__.py          # SQLAlchemy モデル定義
│   ├── routes/
│   │   ├── auth.py              # 認証API
│   │   ├── upload.py            # ファイルアップロードAPI
│   │   └── analysis.py          # データ分析API
│   ├── utils/                   # ユーティリティ（未実装）
│   ├── templates/               # Jinja2テンプレート（未実装）
│   └── static/                  # 静的ファイル（CSS,JS）
├── run.py                       # アプリケーション起動スクリプト
├── requirements.txt             # Python依存パッケージ
├── .env                         # 環境変数（.gitignoreに追加済み）
└── README.md                    # このファイル
```

---

## 🚀 セットアップ手順

### ステップ1: 仮想環境の作成

```bash
cd backend

# 仮想環境を作成
python3 -m venv venv

# 仮想環境を有効化
source venv/bin/activate  # Linux/Mac
# fish の場合
source venv/bin/activate.fish
# または
venv\Scripts\activate  # Windows
```

### ステップ2: 依存パッケージのインストール

```bash
# requirements.txt からインストール
python -m pip install -r requirements.txt

# 既存の venv で古い SQLAlchemy が残る場合は、
# venv を作り直してから再実行してください

# または個別にインストール（カスタマイズ時）
pip install Flask==3.0.0
pip install Flask-CORS==4.0.0
pip install Flask-SQLAlchemy==3.1.1
pip install langchain==0.1.0
# ... その他のパッケージ
```

### ステップ3: 環境変数の設定

`.env` ファイルを確認して、必要に応じて修正してください。

```bash
# .env ファイルの例
FLASK_ENV=development
FLASK_APP=run.py
SECRET_KEY=your-secret-key
DATABASE_URL=sqlite:///graduation_system.db
PORT=5001
GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/serviceAccountKey.json
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=gpt-4o-mini
SESSION_TYPE=filesystem
PERMANENT_SESSION_LIFETIME=3600
UPLOAD_FOLDER=uploads
MAX_CONTENT_LENGTH=52428800
LANGCHAIN_API_KEY=optional
LANGCHAIN_TRACING_V2=false
```

`PORT=5001` を設定しておくと、`python run.py` 実行時に起動ポートを固定しやすくなります。

このプロジェクトでは OpenAI を使うため、API キーは `OPENAI_API_KEY` に設定してください。

⚠️ **重要**: `OPENAI_API_KEY` を設定してください（OpenAI/LangChain 統合時に必須）

### Firebase を使う場合

Firestore をデータベースとして使う場合は、次のいずれかを設定してください。

1. `GOOGLE_APPLICATION_CREDENTIALS` にサービスアカウント JSON の絶対パスを指定する
2. `FIREBASE_CREDENTIALS_JSON` にサービスアカウント JSON をそのまま入れる

`GOOGLE_APPLICATION_CREDENTIALS` のパスは、実際に配置したサービスアカウント JSON に合わせて置き換えてください。

未設定の場合は既存の SQLite 実装にフォールバックします。

### ステップ4: アップロードフォルダの作成

```bash
mkdir -p uploads
```

### ステップ5: アプリケーションの起動

```bash
python run.py
```

ブラウザで以下にアクセス：
```
http://localhost:5001/health
```

成功すれば以下が表示されます：
```json
{"status": "ok"}
```

Firebase の接続状態も確認できます:

```bash
curl http://localhost:5001/health/firebase
```

Firebase が使える場合は `enabled: true`、未設定なら `enabled: false` が返ります。

---

## 🔌 API エンドポイント

### 認証関連 (`/api/auth`)

#### ログイン
```bash
POST /api/auth/login
Content-Type: application/json

{
  "teacher_id": "teacher001",
  "password": "password123"
}

# レスポンス
{
  "success": true,
  "message": "Login successful",
  "teacher_id": "teacher001"
}
```

#### ログアウト
```bash
POST /api/auth/logout
```

#### セッション確認
```bash
GET /api/auth/session

# ログイン状態
{
  "authenticated": true,
  "teacher_id": "teacher001"
}

# ログアウト状態
{
  "authenticated": false
}
```

### ファイルアップロード (`/api/upload`)

#### 学習データアップロード
```bash
POST /api/upload/learning-data
Content-Type: multipart/form-data

- files: [複数ファイル]
- title: "2026年5月 中間テスト成績"
- category: "test"
- description: "中間テストの成績データ"
```

#### 解答用紙アップロード
```bash
POST /api/upload/answer-sheet
Content-Type: multipart/form-data

- files: [複数ファイル]
- test_name: "2026年5月 中間テスト"
- subject: "数学"
- exam_date: "2026-05-24"
- auto_score: "true"
- ocr_process: "true"
- generate_report: "false"
```

### データ分析 (`/api/analysis`)

#### 解答データ一覧取得
```bash
GET /api/analysis/answer-data?test_name=中間テスト&subject=数学&status=completed
```

#### 解答データ詳細取得
```bash
GET /api/analysis/answer-data/1
```

#### 統計情報取得
```bash
GET /api/analysis/statistics
```

#### レポート生成
```bash
POST /api/analysis/generate-report
Content-Type: application/json

{
  "data_id": "1",
  "report_type": "analysis"
}
```

---

## 📊 データベース モデル

### Teacher テーブル
- `id`: UUID (主キー)
- `teacher_id`: ユニークな教師ID
- `password_hash`: パスワードハッシュ
- `name`: 教師の名前
- `email`: メールアドレス
- `created_at`: 作成日時
- `updated_at`: 更新日時

### LearningData テーブル
- `id`: UUID (主キー)
- `teacher_id`: 教師ID (外部キー)
- `title`: データタイトル
- `category`: カテゴリー (test, attendance, assignment, etc.)
- `description`: 説明
- `file_path`: ファイルパス
- `file_name`: ファイル名
- `file_size`: ファイルサイズ
- `file_type`: ファイル形式 (csv, xlsx, json)
- `created_at`: 作成日時

### AnswerSheet テーブル
- `id`: UUID (主キー)
- `teacher_id`: 教師ID (外部キー)
- `test_name`: テスト名
- `subject`: 教科
- `exam_date`: 試験日
- `file_path`: ファイルパス
- `file_name`: ファイル名
- `file_size`: ファイルサイズ
- `status`: ステータス (pending, processing, completed, failed)
- `created_at`: 作成日時

### AnalysisResult テーブル
- `id`: UUID (主キー)
- `answer_sheet_id`: 解答用紙ID (外部キー)
- `student_name`: 生徒名
- `score`: スコア (0-100)
- `correct_count`: 正答数
- `total_questions`: 総問題数
- `error_patterns`: 誤答パターン (JSON)
- `analysis_text`: AI分析結果
- `study_plan`: AI生成の学習計画
- `processing_time`: 処理時間
- `status`: ステータス (completed, failed)
- `created_at`: 作成日時

---

## 🧪 テスト用のデモアカウント

```
教師ID: teacher001
パスワード: password123

教師ID: teacher002
パスワード: password456
```

---

## 📦 依存パッケージ

| パッケージ | バージョン | 用途 |
|-----------|----------|------|
| Flask | 3.0.0 | Webフレームワーク |
| Flask-CORS | 4.0.0 | CORS対応 |
| Flask-SQLAlchemy | 3.1.1 | ORM |
| langchain | 0.1.0 | LLMチェーンフレームワーク |
| openai | 1.3.0 | OpenAI API |
| SQLAlchemy | 2.0.51 | データベースORM |
| SQLite | 標準搭載 | デフォルトDB |
| pytesseract | 0.3.10 | OCR処理 |
| Pillow | 10.0.0 | 画像処理 |

---

## 🔐 セキュリティに関する注意

⚠️ **開発環境では簡易実装です。本番環境では以下を実装してください：**

1. **パスワード管理**: bcrypt または argon2 を使用してハッシュ化
2. **CORS設定**: 許可するオリジンを明示的に指定
3. **セッション**: Redis で管理し、DBは標準の SQLite を使用
4. **API認証**: JWT または OAuth2 を実装
5. **入力バリデーション**: より厳密なチェック
6. **HTTPS**: 本番環境では必須

---

## 🔗 ステップ2: 次のステップ

このセットアップが完了したら、以下の実装に進みます：

1. **✅ プロジェクト構造 & 環境構築** ← 完了！
2. **⏳ 基本的なFlaskアプリケーション** ← 次のステップ
3. **⏳ Langchain統合**
4. **⏳ OCR & 自動採点機能**
5. **⏳ フロントエンド統合**

---

## 📝 トラブルシューティング

### ポート5000が既に使用されている場合
```bash
# 別のポートを指定
PORT=8000 python run.py
```

### モジュールが見つからないエラー
```bash
# 仮想環境が有効化されているか確認
which python  # Linux/Mac
# または
where python  # Windows

# 必要に応じて再インストール
pip install -r requirements.txt --force-reinstall
```

### データベースエラー
```bash
# 既存のDBを削除してリセット
rm graduation_system.db
python run.py  # 新しいDBが自動生成されます
```

---

## 💡 推奨事項

- **ホットリロード**: `--reload` オプションで自動リロード可能
- **ロギング**: `logging` モジュールで詳細ログを記録
- **テスト**: `pytest` でユニットテストを実装
- **API仕様**: `Swagger/OpenAPI` で自動ドキュメント生成

---

## 📞 サポート

問題が発生した場合は、以下を確認してください：

1. `.env` ファイルが正しく設定されているか
2. すべての依存パッケージがインストールされているか
3. 仮想環境が有効化されているか
4. ポート5000が利用可能か

---

**作成日:** 2026年5月24日  
**最終更新:** 2026年5月24日  
**ステータス:** 🟢 セットアップ完了、機能実装待ち
