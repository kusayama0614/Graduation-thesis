"""Firebase/Firestore integration utilities."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from werkzeug.security import check_password_hash, generate_password_hash
from app.utils.time_utils import utc_now


class FirebaseService:
    """Small Firestore wrapper with optional initialization."""

    def __init__(self) -> None:
        self.enabled = False
        self.error: Optional[str] = None
        self.project_id = os.getenv('FIREBASE_PROJECT_ID', '').strip()
        self._client = None
        self._initialize()

    def _initialize(self) -> None:
        credentials_json = os.getenv('FIREBASE_CREDENTIALS_JSON', '').strip()
        credentials_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', '').strip()

        if not credentials_json and not credentials_path:
            return

        try:
            import firebase_admin
            from firebase_admin import credentials, firestore

            if credentials_json:
                service_account_info = json.loads(credentials_json)
                cred = credentials.Certificate(service_account_info)
                self.project_id = self.project_id or service_account_info.get('project_id', '')
            else:
                cred = credentials.Certificate(credentials_path)

            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred, {'projectId': self.project_id} if self.project_id else None)

            self._client = firestore.client()
            self.enabled = True
        except Exception as exc:  # pragma: no cover - initialization failure depends on environment
            self.error = str(exc)
            self.enabled = False

    @property
    def client(self):
        return self._client

    def status(self) -> Dict[str, Any]:
        return {
            'enabled': self.enabled,
            'project_id': self.project_id or None,
            'error': self.error,
        }

    def _now(self) -> datetime:
        return utc_now()

    def _teachers(self):
        return self.client.collection('teachers') if self.enabled else None

    def _learning_data(self):
        return self.client.collection('learning_data') if self.enabled else None

    def _answer_sheets(self):
        return self.client.collection('answer_sheets') if self.enabled else None

    def _analysis_results(self):
        return self.client.collection('analysis_results') if self.enabled else None

    def _processing_logs(self):
        return self.client.collection('processing_logs') if self.enabled else None

    def _serialize_doc(self, doc_snapshot):
        if not doc_snapshot.exists:
            return None

        data = doc_snapshot.to_dict()
        data['id'] = doc_snapshot.id
        for key, value in list(data.items()):
            if isinstance(value, datetime):
                data[key] = value.isoformat()
        return data

    def _where_equal(self, query, field_path: str, value: Any):
        """Prefer modern Firestore filter API while keeping compatibility."""
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter

            return query.where(filter=FieldFilter(field_path, '==', value))
        except Exception:
            # Fallback for environments with older Firestore clients.
            return query.where(field_path, '==', value)

    def seed_default_teachers(self, teachers: List[Dict[str, str]]) -> None:
        if not self.enabled:
            return

        collection = self._teachers()
        if collection is None:
            return

        for teacher in teachers:
            doc_ref = collection.document(teacher['teacher_id'])
            if doc_ref.get().exists:
                continue

            doc_ref.set({
                'teacher_id': teacher['teacher_id'],
                'password_hash': generate_password_hash(teacher['password']),
                'name': teacher['name'],
                'email': teacher['email'],
                'created_at': self._now(),
                'updated_at': self._now(),
            })

    def get_teacher(self, teacher_id: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None

        collection = self._teachers()
        if collection is None:
            return None

        return self._serialize_doc(collection.document(teacher_id).get())

    def authenticate_teacher(self, teacher_id: str, password: str) -> Optional[Dict[str, Any]]:
        teacher = self.get_teacher(teacher_id)
        if not teacher:
            return None

        if not check_password_hash(teacher['password_hash'], password):
            return None

        return teacher

    def create_teacher(self, teacher_id: str, password: str, name: str, email: str) -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError('Firebase is not enabled')

        collection = self._teachers()
        if collection is None:
            raise RuntimeError('Firestore collection is unavailable')

        doc_ref = collection.document(teacher_id)
        if doc_ref.get().exists:
            raise ValueError('Teacher ID already exists')

        payload = {
            'teacher_id': teacher_id,
            'password_hash': generate_password_hash(password),
            'name': name,
            'email': email,
            'created_at': self._now(),
            'updated_at': self._now(),
        }
        doc_ref.set(payload)
        return payload | {'id': teacher_id}

    def update_password(self, teacher_id: str, new_password: str) -> None:
        if not self.enabled:
            raise RuntimeError('Firebase is not enabled')

        collection = self._teachers()
        if collection is None:
            raise RuntimeError('Firestore collection is unavailable')

        doc_ref = collection.document(teacher_id)
        if not doc_ref.get().exists:
            raise ValueError('Teacher not found')

        doc_ref.update({
            'password_hash': generate_password_hash(new_password),
            'updated_at': self._now(),
        })

    def save_learning_data(self, teacher_id: str, title: str, category: str, description: str, file_path: str, file_name: str, file_size: int, file_type: str) -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError('Firebase is not enabled')

        collection = self._learning_data()
        if collection is None:
            raise RuntimeError('Firestore collection is unavailable')

        doc_ref = collection.document()
        payload = {
            'teacher_id': teacher_id,
            'title': title,
            'category': category,
            'description': description,
            'file_path': file_path,
            'file_name': file_name,
            'file_size': file_size,
            'file_type': file_type,
            'created_at': self._now(),
            'updated_at': self._now(),
        }
        doc_ref.set(payload)
        return payload | {'id': doc_ref.id}

    def save_answer_sheet(
        self,
        teacher_id: str,
        test_name: str,
        subject: str,
        exam_date: str,
        file_path: str,
        file_name: str,
        file_size: int,
        status: str = 'pending',
        student_grade: Optional[str] = None,
        student_class: Optional[str] = None,
        student_id: Optional[str] = None,
        notes: Optional[str] = None,
        processing_options: Optional[Dict[str, Any]] = None,
        processing_stage: str = 'uploaded',
        current_step: Optional[str] = None,
        completed_steps: Optional[List[str]] = None,
        progress_percent: Optional[int] = None,
        processing_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError('Firebase is not enabled')

        collection = self._answer_sheets()
        if collection is None:
            raise RuntimeError('Firestore collection is unavailable')

        doc_ref = collection.document()
        payload = {
            'teacher_id': teacher_id,
            'test_name': test_name,
            'subject': subject,
            'exam_date': exam_date,
            'student_grade': student_grade,
            'student_class': student_class,
            'student_id': student_id,
            'notes': notes,
            'processing_options': processing_options or {},
            'processing_stage': processing_stage,
            'current_step': current_step,
            'completed_steps': completed_steps or [],
            'progress_percent': progress_percent,
            'processing_message': processing_message,
            'file_path': file_path,
            'file_name': file_name,
            'file_size': file_size,
            'status': status,
            'created_at': self._now(),
            'updated_at': self._now(),
        }
        doc_ref.set(payload)
        return payload | {'id': doc_ref.id}

    def update_answer_sheet(self, data_id: str, updates: Dict[str, Any]) -> None:
        if not self.enabled:
            raise RuntimeError('Firebase is not enabled')

        collection = self._answer_sheets()
        if collection is None:
            raise RuntimeError('Firestore collection is unavailable')

        updates = dict(updates)
        updates['updated_at'] = self._now()
        collection.document(data_id).update(updates)

    def save_processing_log(self, answer_sheet_id: str, step: str, status: str, message: str = '', error: str = '') -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError('Firebase is not enabled')

        collection = self._processing_logs()
        if collection is None:
            raise RuntimeError('Firestore collection is unavailable')

        doc_ref = collection.document()
        payload = {
            'answer_sheet_id': answer_sheet_id,
            'step': step,
            'status': status,
            'message': message,
            'error': error,
            'created_at': self._now(),
        }
        doc_ref.set(payload)
        return payload | {'id': doc_ref.id}

    def list_processing_logs_by_answer_sheet_id(self, answer_sheet_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []

        collection = self._processing_logs()
        if collection is None:
            return []

        query = self._where_equal(collection, 'answer_sheet_id', answer_sheet_id).limit(limit)
        logs = [self._serialize_doc(doc) for doc in query.stream() if doc.exists]
        logs.sort(key=lambda row: row.get('created_at') or '', reverse=True)
        return logs

    def list_answer_sheets(self, teacher_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []

        collection = self._answer_sheets()
        if collection is None:
            return []

        query = collection.limit(limit)
        if teacher_id:
            query = self._where_equal(query, 'teacher_id', teacher_id)

        return [self._serialize_doc(doc) for doc in query.stream() if doc.exists]

    def get_answer_sheet(self, data_id: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None

        collection = self._answer_sheets()
        if collection is None:
            return None

        return self._serialize_doc(collection.document(data_id).get())

    def get_analysis_result_by_answer_sheet_id(self, answer_sheet_id: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None

        collection = self._analysis_results()
        if collection is None:
            return None

        query = self._where_equal(collection, 'answer_sheet_id', answer_sheet_id)
        for doc in query.stream():
            if doc.exists:
                return self._serialize_doc(doc)

        return None

    def save_analysis_result(self, answer_sheet_id: str, student_name: str, score: Optional[float], correct_count: Optional[int], total_questions: Optional[int], error_patterns: Any, analysis_text: str, study_plan: str, processing_time: str, status: str = 'completed') -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError('Firebase is not enabled')

        collection = self._analysis_results()
        if collection is None:
            raise RuntimeError('Firestore collection is unavailable')

        doc_ref = collection.document()
        payload = {
            'answer_sheet_id': answer_sheet_id,
            'student_name': student_name,
            'score': score,
            'correct_count': correct_count,
            'total_questions': total_questions,
            'error_patterns': error_patterns,
            'analysis_text': analysis_text,
            'study_plan': study_plan,
            'processing_time': processing_time,
            'status': status,
            'created_at': self._now(),
            'updated_at': self._now(),
        }
        doc_ref.set(payload)
        return payload | {'id': doc_ref.id}


_firebase_service: Optional[FirebaseService] = None


def get_firebase_service() -> FirebaseService:
    global _firebase_service
    if _firebase_service is None:
        _firebase_service = FirebaseService()
    return _firebase_service