"""sympy の検算を別プロセスで実行し，終わらない計算をプロセスごと止める仕組みのテスト。"""
import time

import pytest

from app.utils import math_verification as mv
from app.utils import sympy_isolation as iso


@pytest.fixture
def process_mode(monkeypatch):
    monkeypatch.setenv('SYMPY_ISOLATION', 'process')
    monkeypatch.setattr(iso, '_pool', None)
    yield
    if iso._pool is not None:
        iso._pool.shutdown()


def test_checks_run_in_worker_processes(process_mode):
    assert mv.check_claim('1\\cdot(-5)', '-5')['status'] == 'ok'
    assert mv.check_claim("b'^2-ac", '7', {"b'": '-1', 'a': '1', 'c': '-5'}) == {
        'status': 'ng', 'true_value': '6', 'detail': '数値が一致しません'}
    assert mv.check_relation_step('x^2-2*x-5=0', 'x=1\\pm\\sqrt6')['status'] == 'ok'
    assert mv.judge_answer('1/√3', '√3/3')['is_correct']
    assert all(w.alive() for w in iso._pool.all)


def test_slow_computation_is_stopped_and_worker_replaced(process_mode, monkeypatch):
    # 作業プロセスの中で終わらない計算の代わりに，数式でない長い計算をさせる
    start = time.time()
    with pytest.raises(iso.TimeLimitExceeded):
        iso.run_isolated('check_relation_step', ['x>0', 'x>0'], limit=0.0001)
    assert time.time() - start < 30
    # 止めたプロセスの代わりに新しい作業プロセスで続けられる
    assert mv.check_claim('2+3', '5')['status'] == 'ok'
    # 時間切れは「判定できなかった」として返る（ジョブは止まらない）
    monkeypatch.setattr(mv, 'TIME_LIMIT', 0.0001)
    result = mv.check_claim('2+3', '5')
    assert result['status'] == 'unverifiable' and '時間がかかりすぎた' in result['detail']
    assert mv.judge_answer('x=1', 'x=1')['method'] in ('string_exact', 'string_mismatch')
