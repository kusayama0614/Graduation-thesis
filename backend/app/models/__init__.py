# ==================== データベースモデル ====================
"""
SQLAlchemy モデルの定義
"""
from app import db
from datetime import datetime
import uuid

class Teacher(db.Model):
    """教師モデル"""
    __tablename__ = 'teachers'
    
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    teacher_id = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
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
    file_path = db.Column(db.String(255), nullable=False)
    file_name = db.Column(db.String(255), nullable=False)
    file_size = db.Column(db.Integer)  # Bytes
    status = db.Column(db.String(50), default='pending')  # pending, processing, completed, failed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<ProcessingLog {self.step}>'
