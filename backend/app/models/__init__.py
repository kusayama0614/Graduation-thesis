# ==================== データベースモデル ====================
"""
SQLAlchemy モデルの定義
"""
from app import db
from app.utils.time_utils import utc_now
import uuid

class Teacher(db.Model):
    """教師モデル"""
    __tablename__ = 'teachers'
    
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    teacher_id = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    # リレーション
    learning_data = db.relationship('LearningData', backref='teacher', lazy=True, cascade='all, delete-orphan')
    answer_sheets = db.relationship('AnswerSheet', backref='teacher', lazy=True, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Teacher {self.teacher_id}>'


class LearningData(db.Model):
    """学習データモデル"""
    __tablename__ = 'learning_data'
    
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    teacher_id = db.Column(db.String(36), db.ForeignKey('teachers.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(50), nullable=False)  # test, attendance, assignment, etc.
    description = db.Column(db.Text)
    file_path = db.Column(db.String(255), nullable=False)
    file_name = db.Column(db.String(255), nullable=False)
    file_size = db.Column(db.Integer)  # Bytes
    file_type = db.Column(db.String(10))  # csv, xlsx, json
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    def __repr__(self):
        return f'<LearningData {self.title}>'


class AnswerSheet(db.Model):
    """解答用紙モデル"""
    __tablename__ = 'answer_sheets'
    
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    teacher_id = db.Column(db.String(36), db.ForeignKey('teachers.id'), nullable=False)
    test_name = db.Column(db.String(200), nullable=False)
    subject = db.Column(db.String(50), nullable=False)
    exam_date = db.Column(db.Date, nullable=False)
    student_grade = db.Column(db.String(20))
    student_class = db.Column(db.String(20))
    student_id = db.Column(db.String(100))
    notes = db.Column(db.Text)
    file_path = db.Column(db.String(255), nullable=False)
    file_name = db.Column(db.String(255), nullable=False)
    file_size = db.Column(db.Integer)  # Bytes
    status = db.Column(db.String(50), default='pending')  # pending, processing, completed, failed
    processing_options = db.Column(db.Text)  # JSON
    processing_stage = db.Column(db.String(50), default='pending')
    processing_job_id = db.Column(db.String(64))
    current_step = db.Column(db.String(100))
    completed_steps = db.Column(db.Text)  # JSON list
    progress_percent = db.Column(db.Integer, default=0)
    processing_message = db.Column(db.Text)
    last_error = db.Column(db.Text)
    answer_key = db.Column(db.Text)  # JSON
    ocr_text = db.Column(db.Text)
    score = db.Column(db.Float)
    correct_count = db.Column(db.Integer)
    total_questions = db.Column(db.Integer)
    error_patterns = db.Column(db.Text)  # JSON
    question_results = db.Column(db.Text)  # JSON list
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    # リレーション
    analysis_results = db.relationship('AnalysisResult', backref='answer_sheet', lazy=True, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<AnswerSheet {self.test_name}>'


class AnalysisResult(db.Model):
    """分析結果モデル"""
    __tablename__ = 'analysis_results'
    
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    answer_sheet_id = db.Column(db.String(36), db.ForeignKey('answer_sheets.id'), nullable=False)
    student_name = db.Column(db.String(100), nullable=False)
    score = db.Column(db.Float)  # 0-100
    correct_count = db.Column(db.Integer)
    total_questions = db.Column(db.Integer)
    error_patterns = db.Column(db.Text)  # JSON形式
    analysis_text = db.Column(db.Text)  # AI分析結果
    study_plan = db.Column(db.Text)  # AI生成の学習計画
    processing_time = db.Column(db.String(50))  # 処理時間
    status = db.Column(db.String(50), default='completed')  # completed, failed
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    def __repr__(self):
        return f'<AnalysisResult {self.student_name}>'


class ProcessingLog(db.Model):
    """処理ログモデル"""
    __tablename__ = 'processing_logs'
    
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    answer_sheet_id = db.Column(db.String(36), db.ForeignKey('answer_sheets.id'), nullable=False)
    step = db.Column(db.String(100), nullable=False)  # ocr, analysis, scoring, etc.
    status = db.Column(db.String(50), nullable=False)  # started, completed, failed
    message = db.Column(db.Text)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utc_now)
    
    def __repr__(self):
        return f'<ProcessingLog {self.step}>'


# ==================== 個別最適化解説生成（卒業論文 3章） ====================

class LearnerProfile(db.Model):
    """学習者プロファイル（3.2節）。ExerciseRecord から決定的に再計算される集約。"""

    __tablename__ = 'learner_profiles'
    __table_args__ = (db.UniqueConstraint('teacher_id', 'student_id', name='uq_learner_profile_student'),)

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    teacher_id = db.Column(db.String(64), nullable=False, index=True)  # Firebase 利用時は Firestore の教師IDのため外部キーにしない
    student_id = db.Column(db.String(100), nullable=False)  # 匿名化した生徒ID
    node_states = db.Column(db.Text)  # JSON: {node_id: {confidence, evidence_count, history: [...]}}
    error_tendencies = db.Column(db.Text)  # JSON: {node_id: 誤りの回数}
    judgment_counts = db.Column(db.Text)  # JSON: {判定ラベル: 件数}
    exercise_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return f'<LearnerProfile {self.student_id}>'


class ResearchExclusion(db.Model):
    """研究協力不可の生徒。この生徒の解答解説は生成・配布しない（生徒IDだけを持ち，氏名は扱わない）。"""

    __tablename__ = 'research_exclusions'
    __table_args__ = (db.UniqueConstraint('teacher_id', 'student_id', name='uq_research_exclusion_student'),)

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    teacher_id = db.Column(db.String(64), nullable=False, index=True)  # Firebase 利用時は Firestore の教師IDのため外部キーにしない
    student_id = db.Column(db.String(100), nullable=False)  # 匿名化した生徒ID（例: 1-8-15）
    created_at = db.Column(db.DateTime, default=utc_now)

    def __repr__(self):
        return f'<ResearchExclusion {self.student_id}>'


class ExerciseRecord(db.Model):
    """演習ごとの記録（3.2節 表2）。演習プリントの小問1つにつき1行。"""

    __tablename__ = 'exercise_records'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    teacher_id = db.Column(db.String(64), nullable=False, index=True)  # Firebase 利用時は Firestore の教師IDのため外部キーにしない
    student_id = db.Column(db.String(100), nullable=False)
    answer_sheet_id = db.Column(db.String(64), nullable=False)
    exercise_name = db.Column(db.String(200))
    exercise_date = db.Column(db.Date)
    unit = db.Column(db.String(50))
    question = db.Column(db.String(32), nullable=False)
    nodes = db.Column(db.Text)  # JSON list: この小問が依存する前提知識ノード
    expected_answer = db.Column(db.Text)
    initial_answer = db.Column(db.Text)  # 初答
    corrected = db.Column(db.Boolean, default=False)  # 略解参照後の訂正の有無
    judgment = db.Column(db.String(32), nullable=False)  # correct / wrong / incomplete / form_issue / blank ...
    judge_method = db.Column(db.String(32))  # math_verification の method または teacher
    error_node = db.Column(db.String(64))  # 誤りの種類（診断した前提知識ノード）
    diagnosis_method = db.Column(db.String(32))  # single_node / llm / fallback_first_node
    diagnosis_note = db.Column(db.Text)  # 診断の根拠（LLM の理由，フォールバックの理由）
    created_at = db.Column(db.DateTime, default=utc_now)

    def __repr__(self):
        return f'<ExerciseRecord {self.student_id} {self.question}>'


class TestProblem(db.Model):
    """定期テストの問題（フェーズBの入力）と，Chain 1 による解答ステップへの分解結果。"""

    __tablename__ = 'test_problems'
    __test__ = False  # pytest に Test* クラスとして収集させない

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    teacher_id = db.Column(db.String(64), nullable=False, index=True)  # Firebase 利用時は Firestore の教師IDのため外部キーにしない
    unit = db.Column(db.String(50), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    problem_text = db.Column(db.Text, nullable=False)
    correct_answer = db.Column(db.Text)  # 任意。教員が与えた最終的な答え（Chain 4 で解説の結論を照合）
    steps = db.Column(db.Text)  # JSON list: [{id, title, description, result, nodes}]
    steps_source = db.Column(db.String(20))  # chain1 / teacher
    exam_id = db.Column(db.String(36), index=True)  # PDF から取り込んだ定期試験（ExamImport）
    number = db.Column(db.String(30))  # 試験での問題番号（例: 3(2)）
    points = db.Column(db.Integer)  # 配点
    answer_format = db.Column(db.String(20))  # answer_only（答えのみ）/ written（記述式）
    chain1_status = db.Column(db.String(20))  # 取り込んだ問題の Chain 1: pending / done / error
    chain1_error = db.Column(db.Text)
    template = db.Column(db.Text)  # JSON: マスター解説の TeX のうちこの問題の部分（固定の部分と手順の並び。tex_template）
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    explanations = db.relationship('GeneratedExplanation', backref='problem', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<TestProblem {self.title}>'


class GeneratedExplanation(db.Model):
    """生成した解説と，その根拠（Chain 2 の判断と Chain 4 の検算結果）。"""

    __tablename__ = 'generated_explanations'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    teacher_id = db.Column(db.String(64), nullable=False, index=True)  # Firebase 利用時は Firestore の教師IDのため外部キーにしない
    problem_id = db.Column(db.String(36), db.ForeignKey('test_problems.id'), nullable=False)
    student_id = db.Column(db.String(100))  # master（マスター解説）のときは None
    kind = db.Column(db.String(20), default='individual')  # individual（積み上げ型）/ master / common（解答ステップがない問題の全員共通の解説）
    status = db.Column(db.String(20), nullable=False)  # ok / needs_review
    content = db.Column(db.Text)
    trace = db.Column(db.Text)  # JSON: ステップごとの判断・検算・再生成の記録
    profile_snapshot = db.Column(db.Text)  # JSON: 生成時に参照した定着度
    model_name = db.Column(db.String(100))
    signature = db.Column(db.String(64), index=True)  # 生成の入力（問題・ステップ・判断）の指紋。同じなら同じ指示になる
    reused_from = db.Column(db.String(36))  # 同じ判断の生徒の解説を使い回したときの元の解説
    reviewed = db.Column(db.Boolean, default=False)  # 教員が確かめた（直した）
    original_content = db.Column(db.Text)  # 教員が直す前の生成文
    created_at = db.Column(db.DateTime, default=utc_now)

    embeddings = db.relationship('ExplanationEmbedding', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<GeneratedExplanation {self.kind} {self.student_id}>'


class GenerationJob(db.Model):
    """解説生成のバックグラウンドジョブ（生徒数 × ステップ数の LLM 呼び出しになるため非同期で実行）。"""

    __tablename__ = 'generation_jobs'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    teacher_id = db.Column(db.String(64), nullable=False, index=True)  # Firebase 利用時は Firestore の教師IDのため外部キーにしない
    problem_id = db.Column(db.String(36), db.ForeignKey('test_problems.id'), nullable=False)
    status = db.Column(db.String(20), default='queued')  # queued / running / completed / failed
    request = db.Column(db.Text)  # JSON: {student_ids, include_master}
    total = db.Column(db.Integer, default=0)
    done = db.Column(db.Integer, default=0)
    explanation_ids = db.Column(db.Text)  # JSON list
    errors = db.Column(db.Text)  # JSON list
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return f'<GenerationJob {self.id} {self.status}>'


class ExplanationEmbedding(db.Model):
    """解説の埋め込みベクトルのキャッシュ（意味の類似度，5.5節）。モデルごとに1行。"""

    __tablename__ = 'explanation_embeddings'
    __table_args__ = (db.UniqueConstraint('explanation_id', 'model_name', name='uq_explanation_embedding_model'),)

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    explanation_id = db.Column(db.String(36), db.ForeignKey('generated_explanations.id', ondelete='CASCADE'),
                               nullable=False, index=True)
    model_name = db.Column(db.String(100), nullable=False)
    vector = db.Column(db.Text, nullable=False)  # JSON list[float]
    created_at = db.Column(db.DateTime, default=utc_now)


# ==================== 演習プリントの一括取り込み ====================

class BulkImportBatch(db.Model):
    """演習プリントのスキャン（PDF・画像）をまとめて取り込む単位。ページはクラス・出席番号ごとに振り分ける。"""

    __tablename__ = 'bulk_import_batches'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    teacher_id = db.Column(db.String(64), nullable=False, index=True)  # Firebase 利用時は Firestore の教師IDのため外部キーにしない
    name = db.Column(db.String(200), nullable=False)
    subject = db.Column(db.String(50), default='数学')
    status = db.Column(db.String(20), default='uploading')  # uploading / reading / ready / registered
    files = db.Column(db.Text)  # JSON list: [{id, name, pages, grade, class, date}]
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    pages = db.relationship('ScannedPage', backref='batch', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<BulkImportBatch {self.name}>'


class ScannedPage(db.Model):
    """取り込んだ1ページ。見出し（プリント番号・クラス・出席番号）を読み取り，表面と裏面を1枚にまとめる。"""

    __tablename__ = 'scanned_pages'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    batch_id = db.Column(db.String(36), db.ForeignKey('bulk_import_batches.id', ondelete='CASCADE'),
                         nullable=False, index=True)
    file_id = db.Column(db.String(36), nullable=False)
    file_name = db.Column(db.String(255))
    page_index = db.Column(db.Integer, nullable=False)  # ファイル内の 0 始まりのページ番号
    image_path = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(20), default='pending')  # pending / read / error / confirmed
    kind = db.Column(db.String(20))  # front（見出しあり）/ back（裏面）/ excluded（対象外）
    print_no = db.Column(db.Integer)
    print_title = db.Column(db.String(300))
    grade = db.Column(db.Integer)
    class_no = db.Column(db.Integer)
    student_no = db.Column(db.Integer)
    confidence = db.Column(db.String(10))  # high / medium / low
    reading = db.Column(db.Text)  # JSON: 読み取りの生の結果
    error = db.Column(db.Text)
    answer_sheet_id = db.Column(db.String(64))  # 答案として登録した後の AnswerSheet の id
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return f'<ScannedPage {self.file_name} p{self.page_index + 1}>'


class SheetTranscription(db.Model):
    """答案の答えの書き起こし（初答・訂正）と照合の結果。確定した結果は AnswerSheet.question_results に書き込む。"""

    __tablename__ = 'sheet_transcriptions'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    teacher_id = db.Column(db.String(64), nullable=False, index=True)  # Firebase 利用時は Firestore の教師IDのため外部キーにしない
    batch_id = db.Column(db.String(36), index=True)
    answer_sheet_id = db.Column(db.String(64), nullable=False, unique=True)
    print_no = db.Column(db.Integer)
    status = db.Column(db.String(20), default='pending')  # pending / done / error / confirmed
    model_name = db.Column(db.String(100))
    items = db.Column(db.Text)  # JSON list: 小問ごとの書き起こしと判定
    raw = db.Column(db.Text)  # JSON: LLM の出力そのもの
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return f'<SheetTranscription {self.answer_sheet_id} {self.status}>'


class ExamImport(db.Model):
    """定期試験の問題用紙と模範解答の PDF。Gemini で読み取った問題の候補を，教員が確かめてから登録する。"""

    __tablename__ = 'exam_imports'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    teacher_id = db.Column(db.String(64), nullable=False, index=True)  # Firebase 利用時は Firestore の教師IDのため外部キーにしない
    name = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), default='reading')  # reading / ready / error / saved
    pages = db.Column(db.Text)  # JSON list: [{kind: problem|answer, index, path}]
    candidates = db.Column(db.Text)  # JSON list: 読み取った問題の候補
    model_name = db.Column(db.String(100))
    error = db.Column(db.Text)
    template_tex = db.Column(db.Text)  # 教員が作ったマスター解説の TeX（個別の解説はこの体裁で内容だけを個別化する）
    template_name = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return f'<ExamImport {self.name}>'


class ExamGenerationJob(db.Model):
    """定期試験の全問題 × 生徒の解説をまとめて生成するジョブ（途中で止まっても続きから再開できる）。"""

    __tablename__ = 'exam_generation_jobs'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    teacher_id = db.Column(db.String(64), nullable=False, index=True)  # Firebase 利用時は Firestore の教師IDのため外部キーにしない
    exam_id = db.Column(db.String(36), nullable=False, index=True)
    status = db.Column(db.String(20), default='queued')  # queued / running / paused / completed / failed
    request = db.Column(db.Text)  # JSON: {student_ids, problem_ids, include_master, reuse}
    total = db.Column(db.Integer, default=0)  # 生成・使い回し・省略を合わせた件数（問題 × 生徒）
    done = db.Column(db.Integer, default=0)
    generated = db.Column(db.Integer, default=0)  # Gemini で生成した件数
    reused = db.Column(db.Integer, default=0)  # 同じ判断の生徒の解説を使い回した件数
    skipped = db.Column(db.Integer, default=0)  # 判断が変わっておらず，既にある解説をそのまま使った件数
    current = db.Column(db.String(200))
    message = db.Column(db.Text)
    errors = db.Column(db.Text)  # JSON list
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return f'<ExamGenerationJob {self.exam_id} {self.status}>'


class PdfExportJob(db.Model):
    """生徒ごとの解答解説 PDF（A: 個別化した解説，B: マスター解説）を書き出すジョブ。"""

    __tablename__ = 'pdf_export_jobs'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    teacher_id = db.Column(db.String(64), nullable=False, index=True)  # Firebase 利用時は Firestore の教師IDのため外部キーにしない
    exam_id = db.Column(db.String(36), nullable=False, index=True)
    status = db.Column(db.String(20), default='queued')  # queued / running / completed / failed
    request = db.Column(db.Text)  # JSON: {student_ids, classes}
    total = db.Column(db.Integer, default=0)
    done = db.Column(db.Integer, default=0)
    files = db.Column(db.Text)  # JSON list: [{student_id, kind, filename, insufficient, evidence, path}]
    message = db.Column(db.Text)
    errors = db.Column(db.Text)  # JSON list
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return f'<PdfExportJob {self.exam_id} {self.status}>'
