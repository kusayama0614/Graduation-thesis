"""sympy の検算・採点を別プロセスで実行し，時間がかかりすぎたらプロセスごと止める。

入れ子の絶対値の解集合のように，sympy の計算には終わらないものがある。Python のスレッドは外から止められないため，
時間切れの計算が裏で動き続けて CPU を使い，ジョブ全体が止まったように遅くなる。そこで，検算の入口
（math_verification の check_claim・check_relation_step・judge_answer）を常駐の作業プロセスで実行し，
上限（SYMPY_TIME_LIMIT 秒，既定 10）を超えたら，その作業プロセスを止めて作り直す。

- 作業プロセスの数は SYMPY_WORKERS（既定 3）。最初の呼び出しのときに起動する（sympy の読み込みに数秒かかる）
- SYMPY_ISOLATION=off で同じプロセスで直接実行する（テスト用。時間の上限はなくなる）
- 作業プロセスを起動できないときは，作り直してもう1回試し，それでもだめなら同じプロセスの別スレッドで実行する。
  そのときも上限の時間で待つのをやめる（時間切れの扱い）ので，ジョブが止まり続けることはない
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from app.utils import diagnostics

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STARTUP_TIMEOUT = 120


class TimeLimitExceeded(Exception):
    pass


class WorkerUnavailable(Exception):
    pass


def _functions():
    from app.utils import math_verification as mv
    return {
        'judge_answer': mv._judge_answer_direct,
        'check_claim': mv._check_claim_direct,
        'check_relation_step': mv._check_relation_step_direct,
        'check_relation_combination': mv._check_relation_combination_direct,
    }


class _Worker:
    """1つの作業プロセス。1行の JSON で依頼を送り，1行の JSON で結果を受け取る。"""

    def __init__(self):
        env = dict(os.environ, PYTHONPATH=BACKEND_DIR + os.pathsep + os.environ.get('PYTHONPATH', ''),
                   PYTHONIOENCODING='utf-8')
        self.process = subprocess.Popen(
            [sys.executable, '-u', '-m', 'app.utils.sympy_isolation'], cwd=BACKEND_DIR, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, text=True, encoding='utf-8', bufsize=1)
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        # app パッケージの読み込み中の出力が先に来ることがあるので，「ready」の行まで読み飛ばす
        deadline = time.monotonic() + STARTUP_TIMEOUT
        ready = None
        while ready != 'ready':
            try:
                ready = self.lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                ready = None
                break
            if ready is None or time.monotonic() > deadline:
                break
        if ready != 'ready':
            self.kill()
            raise WorkerUnavailable('sympy の作業プロセスを起動できませんでした')

    def _read(self):
        for line in self.process.stdout:
            self.lines.put(line.rstrip('\n'))
        self.lines.put(None)  # プロセスが終わった

    def alive(self) -> bool:
        return self.process.poll() is None

    def call(self, name: str, args: List[Any], limit: float) -> Any:
        self.process.stdin.write(json.dumps({'fn': name, 'args': args}, ensure_ascii=False) + '\n')
        self.process.stdin.flush()
        try:
            line = self.lines.get(timeout=limit)
        except queue.Empty:
            self.kill()
            raise TimeLimitExceeded(f'{limit:g} 秒で計算が終わりませんでした')
        if line is None:
            raise WorkerUnavailable('sympy の作業プロセスが終了しました')
        reply = json.loads(line)
        if 'error' in reply:
            raise RuntimeError(reply['error'])
        return reply['result']

    def kill(self):
        try:
            self.process.kill()
            self.process.wait(timeout=5)
        except Exception:
            pass


class _Pool:
    def __init__(self, size: int):
        self.size = size
        self.idle: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.created = 0
        self.all: List[_Worker] = []

    def _take(self) -> Optional[_Worker]:
        try:
            return self.idle.get_nowait()
        except queue.Empty:
            pass
        with self.lock:
            if self.created < self.size:
                self.created += 1
                return None  # 呼び出し側で新しく起動する
        return self.idle.get()

    def call(self, name: str, args: List[Any], limit: float) -> Any:
        worker = self._take()
        try:
            if worker is None or not worker.alive():
                worker = _Worker()
                self.all.append(worker)
            result = worker.call(name, args, limit)
        except BaseException:
            # 時間切れ・異常終了した作業プロセスは捨て，次に使うときに作り直す
            if worker is not None:
                worker.kill()
            self.idle.put(_Dead())
            raise
        self.idle.put(worker)
        return result

    def shutdown(self):
        for worker in self.all:
            worker.kill()


class _Dead:
    """止めた作業プロセスの代わりの目印（次に取り出したときに作り直す）。"""

    def alive(self) -> bool:
        return False

    def kill(self):
        pass


_pool: Optional[_Pool] = None
_pool_lock = threading.Lock()
_fallback_warned = False


def _get_pool() -> _Pool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _Pool(max(1, int(os.getenv('SYMPY_WORKERS', '3'))))
            atexit.register(_pool.shutdown)
        return _pool


def run_isolated(name: str, args: List[Any], limit: Optional[float] = None) -> Any:
    """math_verification の検算・採点の関数 name を，時間を区切って実行する。時間切れは TimeLimitExceeded。"""
    from app.utils import math_verification as mv

    if os.getenv('SYMPY_ISOLATION', 'process').lower() == 'off' or _IN_WORKER:
        return _functions()[name](*args)
    limit = mv.TIME_LIMIT if limit is None else limit
    for _ in range(2):  # 作業プロセスが落ちていたら，作り直してもう1回
        try:
            return _get_pool().call(name, args, limit)
        except WorkerUnavailable as exc:
            diagnostics.log(f'sympy の作業プロセスが使えません（{exc}）。作り直します')
        except TimeLimitExceeded:
            diagnostics.log(f'sympy の検算 {name} が {limit:g} 秒で終わらなかったため打ち切りました')
            raise
    global _fallback_warned
    if not _fallback_warned:
        _fallback_warned = True
        diagnostics.log('sympy の作業プロセスを使えないため，同じプロセスの別スレッドで検算します（時間の上限は守ります）')
    return _run_in_thread(name, args, limit)


def _run_in_thread(name: str, args: List[Any], limit: float) -> Any:
    box: Dict[str, Any] = {}

    def target():
        try:
            box['result'] = _functions()[name](*args)
        except BaseException as exc:  # noqa: BLE001  呼び出し元で送出し直す
            box['error'] = exc

    thread = threading.Thread(target=target, daemon=True, name=f'sympy-{name}')
    thread.start()
    thread.join(limit)
    if thread.is_alive():  # 止められないスレッドは裏に残るが，ジョブは先に進める
        raise TimeLimitExceeded(f'{limit:g} 秒で計算が終わりませんでした')
    if 'error' in box:
        raise box['error']
    return box['result']


_IN_WORKER = False


def _serve() -> None:
    """作業プロセスの本体: 標準入力の依頼を順に処理し，結果を標準出力に1行ずつ返す。"""
    global _IN_WORKER
    _IN_WORKER = True
    out = sys.stdout
    sys.stdout = sys.stderr  # 読み込み中の print などが結果の行に混ざらないようにする
    functions = _functions()
    out.write('ready\n')
    out.flush()
    for line in sys.stdin:
        try:
            request: Dict[str, Any] = json.loads(line)
            reply = {'result': functions[request['fn']](*request['args'])}
        except Exception as exc:  # noqa: BLE001  呼び出し元で例外として送出し直す
            reply = {'error': f'{exc.__class__.__name__}: {exc}'}
        out.write(json.dumps(reply, ensure_ascii=False, default=str) + '\n')
        out.flush()


if __name__ == '__main__':
    try:
        _serve()
    except KeyboardInterrupt:  # サーバーを Ctrl + C で止めたときに，作業プロセスの Traceback を出さない
        pass
