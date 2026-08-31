import io
import json
import os

import pytest

from app import create_app, db
from app.models import AnswerSheet


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "test.db"
    os.environ["FLASK_ENV"] = "testing"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["ENABLE_DEMO_TEACHERS"] = "false"
    os.environ["FIREBASE_CREDENTIALS_JSON"] = ""
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = ""
    os.environ["SECRET_KEY"] = "test-secret"

    app = create_app()
    app.config.update(TESTING=True)

    with app.test_client() as test_client:
        with app.app_context():
            db.drop_all()
            db.create_all()
        yield test_client


def _register_and_login(client, teacher_id="teacher_test", password="secret12"):
    register_res = client.post(
        "/api/auth/register",
        json={
            "teacher_id": teacher_id,
            "password": password,
            "name": "Test Teacher",
            "email": f"{teacher_id}@example.com",
        },
    )
    assert register_res.status_code == 201

    login_res = client.post(
        "/api/auth/login",
        json={"teacher_id": teacher_id, "password": password},
    )
    assert login_res.status_code == 200


def _upload_answer_sheet(client, *, filename, test_name, subject, student_id, exam_date="2026-08-31"):
    data = {
        "files": (io.BytesIO(b"sample"), filename),
        "test_name": test_name,
        "subject": subject,
        "exam_date": exam_date,
        "student_id": student_id,
        "ocr_process": "false",
        "auto_score": "false",
        "generate_report": "false",
    }
    response = client.post(
        "/api/upload/answer-sheet",
        data=data,
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    return response.get_json()["results"][0]


def test_auth_success_payload_contract(client):
    session_before = client.get("/api/auth/session")
    assert session_before.status_code == 200
    session_before_payload = session_before.get_json()
    assert session_before_payload["success"] is True
    assert session_before_payload["authenticated"] is False

    _register_and_login(client, teacher_id="teacher_contract", password="secret12")

    session_after = client.get("/api/auth/session")
    assert session_after.status_code == 200
    session_after_payload = session_after.get_json()
    assert session_after_payload["success"] is True
    assert session_after_payload["authenticated"] is True
    assert session_after_payload["teacher_id"] == "teacher_contract"



def test_answer_sheet_upload_requires_authentication(client):
    data = {
        "files": (io.BytesIO(b"dummy pdf"), "answer.pdf"),
        "test_name": "Midterm",
        "subject": "math",
        "exam_date": "2026-08-31",
    }
    response = client.post(
        "/api/upload/answer-sheet",
        data=data,
        content_type="multipart/form-data",
    )

    assert response.status_code == 401
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["error_code"] == "AUTH_UNAUTHORIZED"



def test_analysis_endpoints_return_real_empty_data_not_demo(client):
    _register_and_login(client)

    list_response = client.get("/api/analysis/answer-data")
    assert list_response.status_code == 200
    list_payload = list_response.get_json()
    assert list_payload["total"] == 0
    assert list_payload["filtered"] == 0
    assert list_payload["data"] == []

    stats_response = client.get("/api/analysis/statistics")
    assert stats_response.status_code == 200
    stats_payload = stats_response.get_json()
    assert stats_payload["total_data"] == 0
    assert stats_payload["subjects"] == {}



def test_generate_report_uses_real_data_lookup(client):
    _register_and_login(client, teacher_id="teacher_report")

    response = client.post(
        "/api/analysis/generate-report",
        json={"data_id": "non-existent-id", "report_type": "analysis"},
    )

    assert response.status_code == 404
    payload = response.get_json()
    assert payload["error"] == "Data not found"


def test_export_endpoint_returns_downloadable_csv(client):
    _register_and_login(client, teacher_id="teacher_export")

    response = client.post(
        "/api/analysis/export",
        json={"format": "csv", "filters": {"data_id": "all"}},
    )

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    disposition = response.headers.get("Content-Disposition", "")
    assert "attachment;" in disposition
    assert "answer_data_" in disposition
    body = response.get_data(as_text=True)
    assert "test_name" in body


def test_export_endpoint_returns_downloadable_json(client):
    _register_and_login(client, teacher_id="teacher_export_json")

    response = client.post(
        "/api/analysis/export",
        json={"format": "json", "filters": {"data_id": "all"}},
    )

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    disposition = response.headers.get("Content-Disposition", "")
    assert "attachment;" in disposition
    assert "answer_data_" in disposition
    body = response.get_data(as_text=True)
    assert body.strip().startswith("[")


def test_export_endpoint_minimal_profile_csv_headers(client):
    _register_and_login(client, teacher_id="teacher_export_minimal")

    response = client.post(
        "/api/analysis/export",
        json={"format": "csv", "profile": "minimal", "filters": {"data_id": "all"}},
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True).splitlines()[0]
    assert body == "id,test_name,subject,student_id,status,score,upload_date"


def test_local_answer_sheet_upload_persists_student_fields(client):
    _register_and_login(client, teacher_id="teacher_local")

    data = {
        "files": (io.BytesIO(b"sample"), "answer.txt"),
        "test_name": "Final",
        "subject": "math",
        "exam_date": "2026-08-31",
        "student_grade": "2",
        "student_class": "A",
        "student_id": "S100",
        "ocr_process": "false",
        "auto_score": "false",
        "generate_report": "false",
    }
    upload_response = client.post(
        "/api/upload/answer-sheet",
        data=data,
        content_type="multipart/form-data",
    )
    assert upload_response.status_code == 200
    upload_payload = upload_response.get_json()
    assert upload_payload["results"][0]["processing_job_id"]

    list_response = client.get("/api/analysis/answer-data?student_id=S100")
    assert list_response.status_code == 200
    payload = list_response.get_json()

    assert payload["filtered"] == 1
    row = payload["data"][0]
    assert row["student_id"] == "S100"
    assert row["processing_job_id"]
    assert row["processing_stage"] in ("uploaded", "completed")


def test_retry_processing_reissues_job_id_and_logs_endpoint(client):
    _register_and_login(client, teacher_id="teacher_retry")

    data = {
        "files": (io.BytesIO(b"sample"), "retry_target.txt"),
        "test_name": "Retry Test",
        "subject": "math",
        "exam_date": "2026-08-31",
        "student_id": "S200",
        "ocr_process": "false",
        "auto_score": "false",
        "generate_report": "false",
    }
    upload_response = client.post(
        "/api/upload/answer-sheet",
        data=data,
        content_type="multipart/form-data",
    )
    assert upload_response.status_code == 200
    upload_payload = upload_response.get_json()
    record_id = upload_payload["results"][0]["record_id"]
    old_job_id = upload_payload["results"][0]["processing_job_id"]

    retry_response = client.post(
        "/api/analysis/retry-processing",
        json={"data_id": record_id},
    )
    assert retry_response.status_code == 202
    retry_payload = retry_response.get_json()
    assert retry_payload["success"] is True
    assert retry_payload["processing_job_id"]
    assert retry_payload["processing_job_id"] != old_job_id

    logs_response = client.get(f"/api/analysis/processing-logs/{record_id}?limit=20")
    assert logs_response.status_code == 200
    logs_payload = logs_response.get_json()
    assert logs_payload["success"] is True
    assert logs_payload["data_id"] == record_id
    assert isinstance(logs_payload["logs"], list)


def test_retry_processing_requires_data_id(client):
    _register_and_login(client, teacher_id="teacher_retry_missing")

    response = client.post(
        "/api/analysis/retry-processing",
        json={},
    )
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error_code"] == "ANALYSIS_DATA_ID_REQUIRED"


def test_retry_processing_applies_override_options(client, monkeypatch):
    _register_and_login(client, teacher_id="teacher_retry_override")

    # Keep this deterministic by preventing background threads in this test only.
    monkeypatch.setattr("app.routes.upload.start_answer_sheet_processing", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.routes.analysis.start_answer_sheet_processing", lambda *args, **kwargs: None)

    upload_response = client.post(
        "/api/upload/answer-sheet",
        data={
            "files": (io.BytesIO(b"sample"), "retry_override.txt"),
            "test_name": "Retry Override",
            "subject": "math",
            "exam_date": "2026-08-31",
            "student_id": "S300",
            "ocr_process": "false",
            "auto_score": "false",
            "generate_report": "false",
        },
        content_type="multipart/form-data",
    )
    assert upload_response.status_code == 200
    record_id = upload_response.get_json()["results"][0]["record_id"]

    retry_response = client.post(
        "/api/analysis/retry-processing",
        json={
            "data_id": record_id,
            "processing_options": {
                "ocr_process": False,
                "auto_score": True,
                "generate_report": True,
            },
        },
    )
    assert retry_response.status_code == 202
    retry_payload = retry_response.get_json()
    assert retry_payload["success"] is True

    detail_response = client.get(f"/api/analysis/answer-data/{record_id}")
    assert detail_response.status_code == 200
    detail_payload = detail_response.get_json()
    assert detail_payload["processing_options"] == {
        "ocr_process": False,
        "auto_score": True,
        "generate_report": True,
    }


def test_answer_data_supports_server_side_pagination(client, monkeypatch):
    _register_and_login(client, teacher_id="teacher_paging")
    monkeypatch.setattr("app.routes.upload.start_answer_sheet_processing", lambda *args, **kwargs: None)

    _upload_answer_sheet(
        client,
        filename="paging_1.txt",
        test_name="Paging 1",
        subject="math",
        student_id="P001",
    )
    _upload_answer_sheet(
        client,
        filename="paging_2.txt",
        test_name="Paging 2",
        subject="math",
        student_id="P002",
    )
    _upload_answer_sheet(
        client,
        filename="paging_3.txt",
        test_name="Paging 3",
        subject="science",
        student_id="P003",
    )

    page1 = client.get("/api/analysis/answer-data?page=1&per_page=2")
    assert page1.status_code == 200
    payload1 = page1.get_json()
    assert payload1["total"] == 3
    assert payload1["filtered"] == 3
    assert payload1["page"] == 1
    assert payload1["per_page"] == 2
    assert payload1["total_pages"] == 2
    assert payload1["has_next"] is True
    assert payload1["has_prev"] is False
    assert len(payload1["data"]) == 2

    page2 = client.get("/api/analysis/answer-data?page=2&per_page=2")
    assert page2.status_code == 200
    payload2 = page2.get_json()
    assert payload2["page"] == 2
    assert payload2["total_pages"] == 2
    assert payload2["has_next"] is False
    assert payload2["has_prev"] is True
    assert len(payload2["data"]) == 1


def test_learning_progress_returns_student_aggregation(client, monkeypatch):
    _register_and_login(client, teacher_id="teacher_progress")
    monkeypatch.setattr("app.routes.upload.start_answer_sheet_processing", lambda *args, **kwargs: None)

    s1_old = _upload_answer_sheet(
        client,
        filename="progress_s1_old.txt",
        test_name="Progress S1 Old",
        subject="math",
        student_id="S001",
        exam_date="2026-01-01",
    )
    s1_new = _upload_answer_sheet(
        client,
        filename="progress_s1_new.txt",
        test_name="Progress S1 New",
        subject="math",
        student_id="S001",
        exam_date="2026-02-01",
    )
    s2_new = _upload_answer_sheet(
        client,
        filename="progress_s2_new.txt",
        test_name="Progress S2 New",
        subject="science",
        student_id="S002",
        exam_date="2026-03-01",
    )

    with client.application.app_context():
        rows = (
            AnswerSheet.query.filter(AnswerSheet.id.in_([s1_old["record_id"], s1_new["record_id"], s2_new["record_id"]]))
            .all()
        )
        for row in rows:
            row.status = "completed"
            row.processing_stage = "completed"
            row.progress_percent = 100
            if row.id == s1_old["record_id"]:
                row.score = 50
                row.error_patterns = json.dumps([{"pattern": "calc", "count": 2}])
            elif row.id == s1_new["record_id"]:
                row.score = 70
                row.error_patterns = json.dumps([{"pattern": "calc", "count": 1}])
            else:
                row.score = 55
                row.error_patterns = json.dumps([{"pattern": "reading", "count": 3}])
        db.session.commit()

    response = client.get("/api/analysis/learning-progress?page=1&per_page=10")
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["summary"]["total_students"] == 2
    assert payload["summary"]["total_records"] == 3
    assert payload["summary"]["scored_records"] == 3
    assert payload["summary"]["at_risk_students"] == 1
    assert payload["total_students"] == 2
    assert payload["page"] == 1
    assert payload["per_page"] == 10
    assert payload["total_pages"] == 1
    assert len(payload["students"]) == 2

    s1 = next(item for item in payload["students"] if item["student_id"] == "S001")
    assert s1["average_score"] == 60.0
    assert s1["latest_score"] == 70.0
    assert s1["improvement_delta"] == 20.0
    assert s1["at_risk"] is False

    s2 = next(item for item in payload["students"] if item["student_id"] == "S002")
    assert s2["average_score"] == 55.0
    assert s2["latest_score"] == 55.0
    assert s2["at_risk"] is True


def test_learning_progress_supports_at_risk_only_filter(client, monkeypatch):
    _register_and_login(client, teacher_id="teacher_progress_risk")
    monkeypatch.setattr("app.routes.upload.start_answer_sheet_processing", lambda *args, **kwargs: None)

    low = _upload_answer_sheet(
        client,
        filename="risk_low.txt",
        test_name="Risk Low",
        subject="math",
        student_id="R001",
        exam_date="2026-04-01",
    )
    high = _upload_answer_sheet(
        client,
        filename="risk_high.txt",
        test_name="Risk High",
        subject="math",
        student_id="R002",
        exam_date="2026-04-02",
    )

    with client.application.app_context():
        low_row = AnswerSheet.query.filter_by(id=low["record_id"]).first()
        high_row = AnswerSheet.query.filter_by(id=high["record_id"]).first()
        low_row.status = "completed"
        low_row.processing_stage = "completed"
        low_row.score = 45
        high_row.status = "completed"
        high_row.processing_stage = "completed"
        high_row.score = 88
        db.session.commit()

    response = client.get("/api/analysis/learning-progress?at_risk_only=true&page=1&per_page=10")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["summary"]["total_students"] == 1
    assert payload["summary"]["at_risk_students"] == 1
    assert payload["total_students"] == 1
    assert len(payload["students"]) == 1
    assert payload["students"][0]["student_id"] == "R001"
    assert payload["students"][0]["at_risk"] is True


def test_learning_progress_export_returns_csv(client, monkeypatch):
    _register_and_login(client, teacher_id="teacher_progress_export")
    monkeypatch.setattr("app.routes.upload.start_answer_sheet_processing", lambda *args, **kwargs: None)

    record = _upload_answer_sheet(
        client,
        filename="progress_export.txt",
        test_name="Progress Export",
        subject="science",
        student_id="E001",
        exam_date="2026-05-01",
    )

    with client.application.app_context():
        row = AnswerSheet.query.filter_by(id=record["record_id"]).first()
        row.status = "completed"
        row.processing_stage = "completed"
        row.score = 62
        row.error_patterns = json.dumps([{"pattern": "reading", "count": 2}])
        db.session.commit()

    response = client.get("/api/analysis/learning-progress/export?format=csv")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    disposition = response.headers.get("Content-Disposition", "")
    assert "attachment;" in disposition
    assert "learning_progress_" in disposition
    body = response.get_data(as_text=True)
    assert "student_id" in body
    assert "weak_tags" in body
