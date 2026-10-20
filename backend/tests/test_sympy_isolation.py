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


def test_fallback_thread_respects_time_limit():
    import time

    from app.utils import sympy_isolation

    original = sympy_isolation._functions
    sympy_isolation._functions = lambda: {'slow': lambda: time.sleep(5)}
    try:
        started = time.monotonic()
        try:
            sympy_isolation._run_in_thread('slow', [], 0.5)
            raised = False
        except sympy_isolation.TimeLimitExceeded:
            raised = True
        assert raised and time.monotonic() - started < 2
    finally:
        sympy_isolation._functions = original


def test_diagnostics_dump_and_quiet_polling():
    import logging

    from app import _QuietPolling
    from app.utils import diagnostics

    assert '--- スレッド' in diagnostics.thread_stacks()
    quiet = _QuietPolling()
    poll = logging.LogRecord('werkzeug', logging.INFO, '', 0,
                             '127.0.0.1 - - "GET /api/learning/exam-generation-jobs/c4919f69-f76f HTTP/1.1" 200 -', None, None)
    other = logging.LogRecord('werkzeug', logging.INFO, '', 0, '127.0.0.1 - - "POST /api/learning/exams/x/pdf-exports HTTP/1.1" 202 -',
                              None, None)
    assert not quiet.filter(poll) and quiet.filter(other)


def test_inflight_calls_are_listed_while_running():
    from app.learning.llm import _usage
    from app.utils import diagnostics

    diagnostics.label('3(2) 1-8-6')
    try:
        with diagnostics.timed('Gemini 文章', track=True) as info:
            listed = diagnostics.inflight()
            info['detail'] = 'x'
    finally:
        diagnostics.label(None)
    assert listed and listed[0]['label'] == '3(2) 1-8-6' and listed[0]['what'] == 'Gemini 文章'
    assert diagnostics.inflight() == []

    class Message:
        usage_metadata = {'input_tokens': 1200, 'output_tokens': 900, 'output_token_details': {'reasoning': 700}}
    assert _usage(Message()) == '入力 1200・出力 900（うち思考 700）トークン'


def test_transient_gemini_errors_are_retried_after_waiting():
    from app.learning.llm import call_with_retry

    waits, attempts = [], []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("503 UNAVAILABLE. {'error': {'message': 'This model is currently experiencing high demand.'}}")
        return 'ok'

    assert call_with_retry(flaky, 'test', delays=[15, 45, 90], sleep=waits.append) == 'ok'
    assert waits == [15, 45]

    def quota():
        raise RuntimeError('429 RESOURCE_EXHAUSTED quota')
    waits.clear()
    try:
        call_with_retry(quota, 'test', delays=[15], sleep=waits.append)
        raised = False
    except RuntimeError:
        raised = True
    assert raised and waits == []  # 利用上限は待っても通らないので，すぐに一時停止させる

    def always():
        raise RuntimeError('504 DEADLINE_EXCEEDED')
    try:
        call_with_retry(always, 'test', delays=[1, 2], sleep=waits.append)
    except RuntimeError:
        pass
    assert waits == [1, 2]
