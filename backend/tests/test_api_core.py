import io
import os

import pytest

from app import create_app, db


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


def test_export_endpoint_returns_csv_payload(client):
    _register_and_login(client, teacher_id="teacher_export")

    response = client.post(
        "/api/analysis/export",
        json={"format": "csv", "filters": {"data_id": "all"}},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["format"] == "csv"
    assert payload["count"] == 0
    assert "test_name" in payload["csv"]
