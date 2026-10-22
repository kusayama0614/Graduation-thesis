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
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-3.8-flash
GEMINI_THINKING_LEVEL=
GEMINI_EMBEDDING_MODEL=gemini-embedding-2
SESSION_TYPE=filesystem
PERMANENT_SESSION_LIFETIME=3600
UPLOAD_FOLDER=uploads
MAX_CONTENT_LENGTH=52428800
LANGCHAIN_API_KEY=optional
LANGCHAIN_TRACING_V2=false
```

`PORT=5001` を設定しておくと、`python run.py` 実行時に起動ポートを固定しやすくなります。

このプロジェクトでは LLM に Gemini（既定 `gemini-3.8-flash`）を使います。Google AI Studio で発行した API キーを `GEMINI_API_KEY` に設定してください（`GOOGLE_API_KEY` という名前でも読み込みます）。

⚠️ **重要**: `GEMINI_API_KEY` を設定してください（レポート生成・解説生成で必須）

- `GEMINI_MODEL`: 使うモデル（既定 `gemini-3.8-flash`）。文章生成（解説・診断・抜き出し・レポート）はすべてこのモデルを使います。
- `GEMINI_THINKING_LEVEL`: 任意。`low` にすると応答が速く安くなります。未設定ならモデルの既定。
- `GEMINI_EMBEDDING_MODEL`: 解説どうしの意味の類似度（5.5節）に使う埋め込みモデル（既定 `gemini-embedding-2`）。`gemini-embedding-2` には意味的類似度の指示（`task: sentence similarity | query: `）を本文の前に付け，`gemini-embedding-001` を指定した場合は `task_type=SEMANTIC_SIMILARITY` を使います。
- Gemini は常に API キー方式（Gemini Developer API）で接続し，Firebase・Cloud Vision 用の `GOOGLE_APPLICATION_CREDENTIALS` とは独立です。
- Gemini 3 以降では temperature を指定しません（Google の推奨。指定すると推論の劣化やループの原因になるため）。

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
- auto_score: "false"
- ocr_process: "true"
- generate_report: "false"
```

アップロード後、OCRで抽出した得点・正答数・各問題の○×候補を教師が確認・修正して保存できます。保存結果は解答データ一覧と生徒別学習進捗に反映されます。OCR候補は完全とは限らないため、確認してから登録してください。

### データ分析 (`/api/analysis`)

#### 解答データ一覧取得
```bash
GET /api/analysis/answer-data?test_name=中間テスト&subject=数学&status=completed
```

#### 解答データ詳細取得
```bash
GET /api/analysis/answer-data/1
```

#### 採点済みプリントのOCR候補取得・確定結果保存
```bash
GET /api/analysis/answer-data/1/scored-result-suggestions
PUT /api/analysis/answer-data/1/scored-results
Content-Type: application/json

{
  "score": 80,
  "correct_count": 8,
  "total_questions": 10,
  "question_results": [
    {"question": "1", "result": "correct"},
    {"question": "2", "result": "incorrect"}
  ]
}
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
- `score`: 教師が確認した得点 (0-100)
- `correct_count`: 正答数
- `total_questions`: 総問題数
- `question_results`: 教師が確認した問題ごとの正誤 (JSON)
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
| langchain-google-genai | >=4.0.0 | Gemini API |
| SQLAlchemy | 2.0.51 | データベースORM |
| SQLite | 標準搭載 | デフォルトDB |
| pytesseract | 0.3.10 | OCR処理 |
| Pillow | 10.0.0 | 画像処理 |
| sympy | >=1.12 | 採点時の数学的同値判定 |

---

## 🧮 採点ロジック（数学的検証）

`app/utils/scoring.py` の `score_with_key()` は、解答キーと生徒の解答を `app/utils/math_verification.py` の
`judge_answer(expected, actual)` で比較します。文字列の完全一致ではなく、sympyによる数学的な同値判定です（LLMは使わず決定的）。

| 例（解答キー → 生徒の解答） | 判定 | `method` |
|---|---|---|
| `1/√3` → `√3/3` | 正解 | `symbolic_equal` |
| `x^2+2x+1` → `(x+1)²` | 正解 | `symbolic_equal` |
| `23.6`（小数第1位）→ `23.62` / `23.7` | 正解 / 不正解 | `numeric_equal` / `numeric_mismatch` |
| `16/3>m` → `m<16/3` | 正解 | `set_equal` |
| `16/3>m` → `m≦16/3` | 不正解（境界が異なる） | `set_mismatch` |
| `m=-6, 2` → `2, m=-6`（順不同） | 正解 | `list_equal` |
| `m=-6, 2` → `m=-6` | 不正解 | `incomplete` |
| `x=±√3` → `√3, -√3` | 正解（± は2つの答えに展開） | `list_equal` |
| `ア` → `ア`（選択式など数式でないもの） | 正解 | `string_exact`（文字列一致へフォールバック） |

- 期待値が小数のときは、その桁数での四捨五入の範囲（±0.5×10^-桁）を許容します。小数でないときはほぼ厳密一致です。
- 数式として解釈できない解答、または安全でない入力（`[ ] { } " ' ; : \ @ # $ & ` | ~`、`__`、属性アクセス、Pythonキーワード、200文字超）は
  sympyに渡さず、大文字小文字・全角半角を正規化した文字列一致で判定します。
- `score_with_key()` の `details` の各要素に、判定方法を示す `method` キーが含まれます（`empty` は未回答/キー無し）。

OCRテキストからの解答抽出（`parse_answers_from_ocr()`）は、1行 = 「問番号 + 区切り + 解答」として読み取ります。

- 区切りは `)` `.` `:` 空白（全角含む）と `-`。ただし `-` を区切りとみなすのは番号の数字に直接付く場合（`1-A`）か後ろが空白の場合（`1 - 4`）だけで、
  `1 -2`・`1)-2`・`1--2` のように他の区切りの後ろで文字に付く `-` は負号として解答に残します。
- 解答内の空白は、演算子やカンマに隣接する場合だけ許します（`m=-6, 2`、`x = 2` はOK）。`1 A B` や `1 2 3` のように区切りが曖昧な行は読み取りません。

## 🧠 学習者プロファイルと個別化した解説の生成（卒業論文 3章）

`app/learning/` に，卒業論文草稿（`docs/thesis/卒業論文草稿_1013.docx`）3章の設計を実装しています。
API はすべてログインが必要で，`/api/learning` 配下にあります。

### 前提知識依存グラフ（3.3節）
- `app/learning/data/prerequisite_graph.json`。ノードは草稿の表3（2次関数 11ノード）・表4（図形と計量 7ノード）のとおり。
- 辺（前提関係）は単元の構造を示すためのもので，**解説生成の判断（Chain 2）や定着度の更新には使いません**（方針として確定。各ステップの詳しさは，そのステップのノード自身の定着度と誤りの記録だけで決めます）。
- 辺の向き・有無は草稿に記載がないため仮置きで，すべて `"status": "要確認"`。論文などで図として示す場合は確認してください。前提ノードの弱さを判断に反映することは今後の課題です。
- `GET /api/learning/graph` でグラフと詳しさの判断規則を取得できます。

### フェーズA: 演習 → 学習者プロファイル（3.2節）
1. 解答キーの各小問に前提知識ノードを付ける。
   - アップロード時: CSV/Excel の `nodes` 列（`question,answer,nodes`，複数は `;` 区切り）または JSON の `nodes`，フォームの `unit`
   - あとから: `PUT /api/learning/answer-data/<id>/node-tags`  `{"unit": "quadratic_functions", "nodes": {"1": ["inequality_manipulation", "exact_value"]}}`
2. `POST /api/learning/answer-data/<id>/apply` で反映する（同じプリントの再反映は置き換えで，二重に更新されない）。
   - 小問の判定は，教員が確認した採点結果（`PUT /api/analysis/answer-data/<id>/scored-results`）があればそれを優先し，なければ解答キーと OCR テキストを数学的検証チェーンで判定します。
   - 教員確認の `question_results` には任意で `judgment`（`correct` / `correct_form_issue` / `wrong` / `incomplete` / `form_issue` / `blank` / `unreadable` / `unmarked`），初答 `answer`，訂正の有無 `corrected` を付けられます。
   - 誤答の「誤りの種類」は，小問のノードが1つならそのノード，複数なら LLM が候補から1つ選びます（LLM 未設定時は最初のノード）。
3. 定着度 `c_t = 0.3·r_t + 0.7·c_(t−1)`（初期値 0.5）。正解は小問の全ノードに，誤答は診断したノードだけに，空欄は全ノードに反映します。
   - `GET /api/learning/profiles`，`GET /api/learning/profiles/<生徒ID>`（演習ごとの記録つき）

### フェーズB: 定期テスト問題 → 生徒ごとの解説（3.4節・3.5節）
| チェーン | 実装 | 担い手 |
|---|---|---|
| Chain 1 | `POST /api/learning/problems`（`steps` 省略時）で問題文を解答ステップに分解。ノードは JSON Schema の選択肢に制約。教員は `PUT /api/learning/problems/<id>` で修正可 | LLM |
| Chain 2 | 定着度から含める要素を決め（下表），その指示で説明を書く | 判断はプログラム，文章は LLM |
| Chain 3 | ステップの説明を順に結合 | プログラム |
| Chain 4 | 説明中の「左辺 = 右辺」の計算に加えて，不等式・方程式の変形（直前の式を変形したもの）を抜き出し，sympy で検算（等式は値の一致，変形は解集合の一致）。誤りは正しい値や解集合を添えて Chain 2 に差し戻し，3回で直らなければ `needs_review`（教員確認）。計算も変形も抜き出されないステップは警告 | 抜き出しは LLM，判定は sympy |

詳しさの決め方（3.4節の方式(1)，ステップが依存するノードのうち最も弱いものの定着度 c で判断）:

| 要素 | 含める条件 |
|---|---|
| 結果と使う公式 | 常に |
| 途中の式変形を省略しない説明 | c < 0.5（草稿の閾値） |
| 前提知識の定義・公式の意味の復習 | c < 0.35 |
| 類題での確認 | c < 0.2 |
| その生徒の誤りの傾向への注意 | 誤りの記録があるノードの定着度 < 0.7 |

- `GET /api/learning/problems/<id>/plan?student_id=...` で，LLM を呼ばずに判断とその理由（例:「double_root の定着度 0.35 が閾値 0.5 を下回るため…」）を確認できます。
- `POST /api/learning/problems/<id>/explanations`  `{"student_ids": [...], "include_master": true}` で生成（バックグラウンドのジョブ。`GET /api/learning/jobs/<job_id>` で進捗）。
  `student_ids` を省略するとプロファイルのある全生徒が対象です。問題に `correct_answer` を登録しておくと，解説の結論も照合します。
- 生成結果は `GET /api/learning/explanations/<id>`。ステップごとの判断・検算・差し戻しの記録（`trace`）と，参照した定着度（`profile_snapshot`）が残ります。
- 比較（5.5節 表10）: `GET /api/learning/problems/<id>/comparison` でマスター解説と積み上げ型（個別化した解説）の指標と，文字列の一致度（difflib）・意味の類似度（Gemini の埋め込み `gemini-embedding-2` によるコサイン類似度）を返します。埋め込みは解説ごとに保存し，2回目以降は再計算しません（`?semantic=false` で省略可）。

LLM は Gemini で，環境変数 `GEMINI_API_KEY`（必須）と `GEMINI_MODEL`（既定 `gemini-3.8-flash`）で設定します。未設定のときは LLM を使う API が 503 を返し，プロファイルの更新は LLM なしで動きます。

### 演習プリントの一括取り込み（`/api/bulk`）

クラスごとにスキャンした演習プリント（約1,100枚など）をまとめて取り込み，クラス・出席番号ごとの答案に振り分けます（画面: `screens/learning/bulk-import.html`）。

1. **取り込み** `POST /api/bulk/batches` → `POST /api/bulk/batches/<id>/files`（1ファイルずつ。PDF・画像）。PyMuPDF でページごとの JPEG（150dpi）に分けます（poppler は不要）。ファイル名が「1(1)-No1-0916.pdf」の形なら学年・クラス・提出日を推定します。
2. **見出しの読み取り** `POST /api/bulk/batches/<id>/read`。各ページを Gemini（`GEMINI_MODEL`）の画像読み取りに渡し，「2学期 No.1【単元】」「1年 _組 _番」からプリント番号・学年・クラス・出席番号を抜き出します（**氏名は読み取りません**）。見出しのないページは裏面として直前の表面にまとめます。並列数は `BULK_READ_CONCURRENCY`（既定 4）。失敗したページは同じ API で続きから読み取り直せます。1ページにつき1回呼び出します。
3. **確認** `GET /api/bulk/batches/<id>` で答案のまとまりと確認が必要な点（読めない番号・自信が低い・ファイルのクラスと違う・同じ生徒の重複・表面のない裏面）を返します。`PATCH /api/bulk/pages/<id>` で修正，`POST /api/bulk/batches/<id>/confirm` で確認済みにします。画像は `GET /api/bulk/pages/<id>/image`。
4. **登録** `POST /api/bulk/batches/<id>/register`  `{"prints": {"1": {"test_name", "unit", "answer_key", "exam_date"}}, "run_ocr": false}`。確認の済んだ答案だけを，表面と裏面を1つの PDF にまとめて AnswerSheet として登録します。生徒 ID は「学年-クラス-出席番号」（例 `1-1-20`）。解答キーのファイルは `POST /api/bulk/answer-key/parse` で変換できます。`run_ocr` を指定すると既存の OCR・自動採点を実行します（同時に処理する数は `ANSWER_SHEET_CONCURRENCY`，既定 2）。

### 答えの書き起こしと採点（一括取り込みの続き）

一括取り込みで登録した答案の答えを Gemini で書き起こし，解答キーと照合して採点します（画面: 一括取り込みの「答えの書き起こしと採点」）。

- **解答キー**: `app/learning/data/answer_keys.json`（No.1〜No.9。元は `docs/answer_keys/演習プリント解答キー_No1-No9.md`）。登録時に解答キーのファイルを選ばなければ，プリント番号から自動で付きます（前提知識ノードの初期値も付くので，「① 演習の反映」のまとめて反映でそのまま使えます）。
- **書き起こし（層2）** `POST /api/bulk/batches/<id>/transcribe`  `{"print_nos": [1], "redo": false}`。答案の全ページを1回で Gemini に渡し，小問ごとに鉛筆の初答と色ペンの訂正，自己採点の丸，書き方の問題，読み取りの自信を返させます。**正解は渡しません**（誤答を正しい値に直す過剰修正を防ぐため）。
- **照合（層3）** `app/utils/transcription.py`。初答だけを解答キーと数式処理で照合します。sinθ・cosθ・tanθ のような組は順序どおりに比べる（取り違えは誤り），三角比の表の値は桁を粗くすると誤り（0.4067 を 0.41），「m=2 のとき x=−1」型は組ごとに比べて一部だけなら不完全，「すべての実数」「解はない」は言い換えを許す，「cos16°」は形そのものを比べる。No.7 のグラフは，Gemini が形（選択肢から）・グラフに書かれた点・書かれた式を書き起こし，数式処理で照合する。形が正しく，正解の点（頂点・軸との交点）が全て書かれていれば正解（ほかの点や式が書かれていても減点せず，参考として理由に残す）。正解の点が欠けていれば不完全，形が違う・正解の点がなくグラフ上にない点だけなら誤答。なお，**形の分類は Gemini が画像を見て選択肢から選ぶ（層2の変換）**ため，形の正誤は実質的に LLM の読み取りに依存する（数式処理が確かめるのは点と式）。確認画面で教員が直せる。
- **確認** `GET /api/bulk/batches/<id>/transcriptions` で一覧，`PUT /api/bulk/transcriptions/<答案ID>` で直す（照合し直して確認済みにする）。結果は答案の採点結果（`question_results`）に書き込まれます。

### 定期試験の PDF からの問題の取り込み（`/api/learning/exam-imports`）

画面「③ 定期テスト問題と解説」の「PDF から取り込む」から使います。

1. `POST /api/learning/exam-imports`（multipart: `problem_pdf` 必須，`answer_pdf` 任意，`name` 任意）。PDF を 170dpi のページ画像にし，全ページを1回で Gemini に渡して，小問ごとに番号・題・単元・問題文（数式は `$...$`）・正しい答え（数式処理で照合できる形）・模範解答の表記・答えのみ／記述式・配点・図の補足を読み取ります（数式の PDF は文字の層が崩れていることが多いため画像として読む）。
2. `GET /api/learning/exam-imports/<id>` で候補を確認（ページ画像は `.../pages/<n>/image`）。失敗・読み直しは `POST .../read`。
3. `POST /api/learning/exam-imports/<id>/save`  `{"questions": [...]}` で，教員が直した候補を問題（TestProblem）として登録し，Chain 1（解答ステップへの分解）を裏で1問ずつ実行します。失敗した問題は `POST .../chain1` で作り直せます。

### 定期試験の解説の一括生成と配布（画面「④ 解説の生成と配布」）

試験の全問題 × 選んだ生徒（クラス単位）の解説をまとめて生成し，教員が要確認のものを確かめてから，生徒ごとの冊子として印刷します（3.1節「試験の返却と同時に全員へ解説を配布」）。

- **見積もり** `GET /api/learning/exams/<exam_id>/generation-plan?classes=1-1&reuse=true`：LLM を呼ばずに，問題ごとの「生成・使い回し・省略」の件数と Gemini の呼び出し回数の目安を返します。
- **生成** `POST /api/learning/exams/<exam_id>/generation-jobs` `{"classes": ["1-1"] | "student_ids": [...], "problem_ids"?: [...], "reuse": true, "include_master": false}`。進み具合は `GET /api/learning/exam-generation-jobs/<job_id>`。
- **使い回し（指紋）**：LLM に渡す指示は「問題・ステップ・Chain 2 の判断（含める要素と誤りへの注意）・モデル」だけで決まるため，これらの指紋（signature）が同じ生徒には1回だけ生成した解説を使い回します（`reused_from` に元の解説）。判断の理由と定着度の記録はその生徒自身のものに置き換えます。判断が前回から変わっていない生徒は作り直しません（省略）。`reuse: false` で生徒ごとに生成できます（同じ判断の生徒どうしの類似度（表10）は，使い回すと 1.0 になる点に注意）。
- **解答ステップがない問題**（Chain 1 で分解できなかったグラフの問題など）：生徒ごとの判断ができないため，個別化しない**全員共通の解説**（`kind: "common"`）を1問につき1件だけ生成し，全員の冊子に載せます。書き方は，教員が参考に示した参考書（チャート式）の解答編の構成と文体に合わせています（「> **指針**」「> **CHART**」の枠 → 「### 解答」（である調，式番号 ……①，場合分け [1][2]，条件の確認，「← 」の補足，答えは太字）→ 必要なら「### 別解」「### 検討」）。規則は `app/learning/explanation.py` の `COMMON_STYLE` にあり，変えたときは `COMMON_STYLE_VERSION` を上げると既存の共通の解説が次の生成で作り直されます。途中の式変形を省略せずに書かせ，個別の解説と同じく Chain 4 で検算して，直らなければ要確認として教員に回します。Chain 1 の実行中の問題は生成しません。
- **一時停止と再開**：Gemini の利用上限（429 / RESOURCE_EXHAUSTED）に達するとジョブは `paused` になります。`POST /api/learning/exam-generation-jobs/<job_id>/resume` で続きから生成します（済んだ解説は指紋が同じなので省略されます）。並列数は `EXPLANATION_CONCURRENCY`（既定 3）。
- **問題ごとの状況** `GET /api/learning/exams/<exam_id>/explanations-summary`：解説の揃い具合，異なる解説の数，要確認の件数と，**模範解答の照合**（Chain 1 が問題文から導いた最後のステップの結果と模範解答を sympy で比較。表6 層1の考え方で，略解の誤記や読み取り・分解の誤りに気づくため）を返します。`review` には要確認で未確認の解説が，使い回しの元ごとに1件ずつ並びます。
- **教員の確認** `PUT /api/learning/explanations/<id>` `{"content"?: "直した本文", "reviewed": true, "apply_to_group": true}`：確認済みにします。本文を直すと，生成された元の文を `original_content` に残し，同じ解説を使い回した生徒にも反映します（生徒ごとに直した解説は上書きしません）。画面は解説の表示画面の「直す」「確認済みにする」。
- **冊子** `GET /api/learning/exams/<exam_id>/booklet?classes=1-1`：生徒ごとに問題番号順の最新の解説。画面 `screens/learning/booklet.html` で1人ずつ改ページして印刷でき，末尾にふり返りアンケート（分かりやすさ・詳しさ・自分のつまずきに合っていたか・自由記述）を付けられます。要確認で未確認の解説がある生徒は画面上で警告します。

### マスター解説の TeX をテンプレートにした個別化（画面「④」の「テンプレート」）

教員が作ったマスター解説の TeX（`\daimon{番号}{題}`・`\shomon{(1) ...}`・`kotae`・`\kangae`・`\tejun`・`chui`）を読み込むと，個別の解説は**その体裁のまま内容だけを個別化**します。

- `POST /api/learning/exam-imports/<exam_id>/template`（multipart の `file`，または `{"tex": "..."}`）。大問・小問の番号（例 `1(1)`，小問のない大問は `2`）で③の問題と対応させます。大問の冒頭の共通部分（大問4の考え方など）は最初の小問に含めます。`DELETE` で外せます。
- 対応した問題は，**テンプレートの「手順」をそのまま解答ステップ**にし，各手順が依存する前提知識ノードだけを Gemini が付けます（Chain 1 の分解の代わり。③で直せます）。
- 個別の解説（Chain 2）は，各手順の本文を土台に，Chain 2 の判断に応じて内容だけを変えます：定着が弱い手順は途中式を補う，定着済みの手順は簡潔にする，`\fukushu{...}`（前提知識の復習），その生徒の誤りに向けた `chui` 環境の注意，`\ruidai{...}`（類題）。**答・考え方・手順の見出し・図（tikz）・表・テンプレートの注意はプログラムがテンプレートのまま組み立て**ます（Chain 3）。図は ⟦図1⟧ の印に置き換えて LLM に渡さず，必ず元の位置に戻します。手順ごとの計算の検算（Chain 4）はこれまでどおりです（答はテンプレートの「答」なので，最後の結論の照合はしません）。
- 比較用のマスター解説はテンプレートそのもの（LLM を呼ばない）です。手順のない小問は，テンプレートの部分をそのまま全員共通の解説にします。
- `GET /api/learning/exams/<exam_id>/booklet.tex?classes=1-1`：テンプレートと同じプリアンブル・表題で，生徒ごとに改ページした1つの TeX 文書を返します（表題の「□組□番」に組・番を記入。`\fukushu`・`\ruidai` は `\providecommand` で補うので，テンプレート側で定義すればその見た目になります）。xelatex などテンプレートと同じ方法でコンパイルします。画面の表示・冊子では TeX を見出しや枠に直して表示します（図は「印刷用の TeX を参照」）。
- テンプレートを読み込み直す・内容を変えると，指紋が変わるため次の生成で作り直されます。

### 要確認の解説だけを直す（画面「④」の「4. 要確認の解説を確かめる」）

「要確認の解説だけ再生成する」ボタンは，Gemini の呼び出しを最小にして要確認の解説を直します。

1. **再検算（Gemini を呼ばない）**: 各ステップの最後の文章について，記録に残っている「抜き出した計算」を現在の検算（sympy）でもう一度確かめます。検算側の不具合（複数解の並記，`b'`，中黒など）が原因だったものは，ここで直ります。
2. **書き直し（必要なステップだけ）**: それでも通らないステップだけを，検算の指摘を添えて書き直します（他のステップの文章と判断はそのまま。1ステップにつき書く1回＋抜き出し1回，差し戻しが続けば最大3倍）。
3. 同じ解説を使い回した生徒はまとめて1回だけ直し，記録（判断の理由）はそれぞれの生徒のものにします。教員が確認済みにした解説は対象外です。元の解説は残り，直したものが新しい版として保存されます。

- 見積もり（Gemini を呼ばない）: `GET /api/learning/exams/<exam_id>/repair-plan?classes=1-1`
- 実行: `POST /api/learning/exams/<exam_id>/repair-jobs` `{"classes": ["1-1"]}`（進み具合・一時停止と再開は生成のジョブと同じ）

### 検算が止まらないようにする仕組み

sympy の計算には終わらないもの（入れ子の絶対値の解集合など）があるため，検算・採点（`check_claim`・`check_relation_step`・`judge_answer`）は常駐の作業プロセス（`app/utils/sympy_isolation.py`）で実行し，1回 `SYMPY_TIME_LIMIT` 秒（既定 10）を超えたら作業プロセスごと止めて「検算不能」として先に進みます（作業プロセスは `SYMPY_WORKERS`，既定 3）。Gemini の呼び出しも `GEMINI_TIMEOUT` 秒（既定 180）・再試行 `GEMINI_MAX_RETRIES` 回（既定 2）で打ち切ります。

長いジョブの途中で Mac がスリープするとサーバーも止まります。`caffeinate -i .venv_local/bin/python run.py` のように起動すると，実行中はスリープしません。

**生成の並行**: 使い回せない解説は，問題をまたいで `EXPLANATION_CONCURRENCY`（既定 3）件ずつ並行に作ります（1問に生成が1〜2件しかなくても，同時に動ける数を使い切る）。画面には Gemini の応答を待っている呼び出しと経過秒数が出ます。

**止まって見えるときの調べ方**: 端末には，Gemini の各呼び出しの所要時間と思考の深さ・トークン数（例 `[3(2) 1-8-6] Gemini 文章: 12.3秒（思考 low，入力 3200・出力 800（うち思考 300）トークン）`）と，検算の打ち切りが1行ずつ出ます（画面の数秒ごとの問い合わせの行は出しません。`QUIET_POLLING=0` で出す）。ジョブが `DIAG_STALL_SECONDS`（既定 600）秒進まなければ，全スレッドが今どこで待っているかを自動で端末に出します。ブラウザで `http://localhost:5001/api/learning/debug/threads` を開いても同じものが見られます。

**Gemini の時間切れ（504 DEADLINE_EXCEEDED）**: 1回の応答が `GEMINI_TIMEOUT`（既定 180）秒を超えると起きます。手順の文章を書く・整える呼び出しは，計算を sympy で確かめるので思考を浅く（`GEMINI_WRITING_THINKING_LEVEL`，既定 `low`）して応答を速くしています。Gemini 側の一時的な混雑（503 UNAVAILABLE「high demand」，504，500）は，呼び出しごとに 15・45・90 秒待って作り直します（`GEMINI_TRANSIENT_RETRY_DELAYS`。利用上限の 429 は待たずに一時停止）。それでも失敗した解説はジョブの最後にもう1回まとめて作り直し（「文章を整える」も同じ），それでも失敗したら，テンプレートがある問題はマスター解説の手順をそのまま代わりに載せます（どの生徒にも解説のない問題を残さない）。代わりの解説には指紋を付けないので，次に「解説を生成する」を押すと個別化し直します。全員共通の解説も失敗したら1回作り直します。④の生徒一覧に「解説なし」の生徒がいるときは「足りない解説を作る」で，足りない分だけを作れます。

### 研究協力不可の生徒

④の「研究協力不可の生徒」に「1-8-15」か「1年8組15番」の形で登録した生徒の解答解説は作りません（`POST /api/learning/exclusions` {students, delete_existing}，一覧は `GET`，外すのは `DELETE /api/learning/exclusions/<生徒ID>`）。解説の生成・文章を整える・要確認の手直し・冊子・TeX・PDF の対象から外し，登録の前に作ったジョブを「続きから生成」しても作りません。「既に作った解説も削除する」を選ぶと，その生徒の解説を消します（ほかの生徒がその解説を使い回していた場合は，その生徒の解説を新しい元にするので，ほかの生徒の解説は残ります）。演習の記録や学習者プロファイルは消しません。

### 文章を整える（文体・誤字・注意の重複）

個別化した解説（A）の書き足した部分がマスター解説（B）と違う文体（敬体）だと，A・B を伏せて配ってもどちらが個別化したものか気づかれるため，`app/learning/polish.py` で次を直します。

- **決まった誤字**（「下にとつ」→「下に凸」など）: プログラムで直す（Gemini を呼ばない）
- **マスター解説・同じ大問の前の小問・前の手順と同じ内容の注意**（ほぼ同じ文，または元の注意の語がすべて含まれる言い換え。例:「両辺に $-1$ をかけると不等号の向きが逆になる」に対する「両辺に負の数をかけると…」）: 追加した方をプログラムで削る（Gemini を呼ばない）
- **敬体（です・ます・しましょう。後ろに数式や改行が続く場合も数える）**，**3文以上・150字を超える注意**，**言い換えで似ている注意**（例:「$-1$ をかけると不等号の向きが逆になる」と「負の数をかけると…」）: 該当するステップだけを，解説1件につき1回の呼び出しでまとめて直す。似ている注意は，同じ誤りへの注意なら削り，別の場合への注意（「接するとき」と「共有点をもたないとき」など）なら残すよう Gemini に判断させ，一度確かめた手順は次からは確かめない
- 数式は ⟦式1⟧ のような印に置き換えて渡し，返ってきた文章に元の数式を戻す（Gemini は数式を書き換えられない）。敬体や長い注意が残った手順だけ，もう1回呼ぶ（解説1件につき最大2回）。それでも残った敬体は，地の文の語尾を機械的に常体にする（「解きます」→「解く」，「範囲です」→「範囲である」など）
- 直した文章は，数式が1つでも変わる・注意の外の数式が消える・図の印が消える・TeX が壊れる場合は採用しない（検算済みの計算は変わらない）

新しく生成する解説は，生成の指示で常体・正しい用語・注意の重複禁止を伝え，それでも残った場合だけ生成の最後に1回整えます。生成済みの解説は，④の「文章を整える（選んだクラス）」（`POST /api/learning/exams/<exam_id>/polish-jobs`，見積もりは `GET .../polish-plan`）で直します。教員が本文を直した解説は変えません（確認済みにしただけの解説は，計算を変えずに文章だけ整え，確認済みのまま残します）。「要確認のみ再生成」で書き直した手順も，生成のときと同じように整えます。整えたあとに「PDF を作る」を押すと，変わった解説の PDF だけ作り直します。

### 生徒ごとの PDF とクラスごとの一括ダウンロード（画面「④」の「5. 配布する」）

テンプレートを読み込んだ試験は，生徒ごとに2つの PDF を作ります。

- **ファイル名**: `1年1組20番_解答解説A.pdf`（その生徒の個別化した解説），`1年1組20番_解答解説B.pdf`（マスター解説）。表題の枠（`\kaisetsulabel`）も「解説A」「解説B」だけにし，どちらが個別化したものかはタイトルなどから分からないようにしています。個別化の指示でも，生徒の記録や「あなたは〜」に触れないように書かせています。
- **※ の基準**: 学習者プロファイルの根拠が少なく，個別化しているとは言い難い生徒の A は `…_解答解説A※.pdf`。試験の全問題のステップのうち，依存する前提知識の根拠（演習で出題された小問の数）がすべて **3問以上** のステップが **50% 未満** なら ※（記録のないノードは定着度の初期値 0.5 で判断されるため）。設定 `PERSONALIZATION_MIN_EVIDENCE`・`PERSONALIZATION_MIN_RATIO` で変えられます。④の生徒一覧にも「※ 記録が少ない」と表示します。
- **API**: `POST /api/learning/exams/<exam_id>/pdf-exports` `{"classes": ["1-1"]}` で作成開始（裏で xelatex を実行。並列数 `PDF_EXPORT_CONCURRENCY`，既定 2）。進み具合は `GET /api/learning/pdf-exports/<job_id>`，1件は `.../files/<n>`，クラス分の一括ダウンロードは `.../zip`（`?kind=A` / `?kind=B` で片方だけ）。同じ内容の PDF は保存しておき作り直しません。
- **必要なもの**: xelatex（MacTeX など）と，テンプレートが使うフォント（Noto Sans/Serif CJK JP，TeX Gyre Termes）。xelatex が PATH にないときは `/Library/TeX/texbin/xelatex` なども探します。見つからなければ `.env` に `PDF_LATEX_ENGINE=/Library/TeX/texbin/xelatex` のように書いてください。

### 評価の集計（5.4節・5.5節）

比較はマスター解説と積み上げ型（個別化した解説）の2つで行います（引き算型は比較の対象から外しました。以前に作った引き算型の解説はデータベースに残っていても，一覧・比較・集計には出しません）。

記録から決定的に集計します（LLM は呼びません）。出力先は `docs/evaluation/`（手順は同フォルダの README）。

```bash
.venv_local/bin/python scripts/evaluation_report.py          # 数学的整合率・表10（最新の試行の記録から）
.venv_local/bin/python scripts/evaluation_report.py --db     # 画面で生成した解説（データベース）から
.venv_local/bin/python scripts/diagnosis_agreement.py sheet  # 誤りの種類のラベル付けの表を作る
.venv_local/bin/python scripts/diagnosis_agreement.py score  # 研究者のラベルとの一致率・κ
```

- 数学的整合率: (a) 初回で全ての計算が検算OK，(b) 差し戻しで修正，(c) 上限に達して教員の確認へ，の割合（解説単位・ステップ単位）。
- 表10: 未定着ステップを詳しく書いた割合，定着済みステップの文字数，誤りへの注意を含むステップ，マスター解説との文字列一致度・意味の類似度。CSV の「固有の内容（研究者判定）」列は研究者が記入します。
- 一致率: Gemini が候補ノードから選んだ行（診断方法 `llm`）が対象。候補が一つの小問は参考として別に数えます。

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

### ポート5001が既に使用されている場合
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
4. ポート5001が利用可能か

---

**作成日:** 2026年5月24日  
**最終更新:** 2026年5月24日  
**ステータス:** 🟢 セットアップ完了、機能実装待ち
