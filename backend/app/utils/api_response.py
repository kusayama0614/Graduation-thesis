"""API response helpers.

Keep backward compatibility by preserving the legacy `error` string field,
while adding stable `error_code` and `success` keys.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from flask import jsonify


def make_error_response(error_code: str, message: str, status_code: int, details: Optional[Any] = None, extra: Optional[Dict[str, Any]] = None):
    payload: Dict[str, Any] = {
        'success': False,
        'error': message,
        'error_code': error_code,
        'message': message,
    }
    if details is not None:
        payload['details'] = details
    if extra:
        payload.update(extra)
    return jsonify(payload), status_code


def make_success_response(data: Optional[Dict[str, Any]] = None, status_code: int = 200):
    payload: Dict[str, Any] = {'success': True}
    if data:
        payload.update(data)
    return jsonify(payload), status_code
