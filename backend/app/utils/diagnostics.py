"""ジョブが止まったように見えるときの原因を調べるための記録。

- log(): 時刻と「どの処理か」（スレッドごとの札）を付けて端末に1行出す
- label(): いまのスレッドの札（例:「3(2) 1-8-5」）を付ける
- thread_stacks(): 全スレッドが今どこで待っているか（スタック）を文字列で返す
- watch_job(): 生成ジョブの進み具合を見張り，DIAG_STALL_SECONDS 秒（既定 600）進まなければ全スレッドのスタックを端末に出す
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Optional, Tuple

_local = threading.local()


def label(text: Optional[str]) -> None:
    _local.label = text


def current_label() -> str:
    return getattr(_local, 'label', None) or threading.current_thread().name


def log(message: str) -> None:
    print(f'[{datetime.now():%H:%M:%S}] [{current_label()}] {message}', file=sys.stderr, flush=True)


@contextmanager
def timed(what: str, warn_after: float = 60.0):
    """what にかかった時間を記録する（warn_after 秒を超えたら目立つように出す）。"""
    started = time.monotonic()
    try:
        yield
    except Exception as exc:
        log(f'{what}: 失敗 {time.monotonic() - started:.1f}秒 {exc.__class__.__name__}: {str(exc)[:200]}')
        raise
    elapsed = time.monotonic() - started
    if elapsed >= warn_after:
        log(f'{what}: {elapsed:.1f}秒（時間がかかっています）')
    elif os.getenv('DIAG_VERBOSE', '1') != '0':
        log(f'{what}: {elapsed:.1f}秒')


def thread_stacks() -> str:
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():
        stack = ''.join(traceback.format_stack(frame))
        parts.append(f'--- スレッド {names.get(ident, ident)} ---\n{stack}')
    return '\n'.join(parts)


def watch_job(read_progress: Callable[[], Optional[Tuple]], is_running: Callable[[], bool],
              interval: float = 30.0) -> threading.Thread:
    """read_progress() の値が DIAG_STALL_SECONDS 秒変わらなければ，全スレッドのスタックを端末に出す。"""
    stall = float(os.getenv('DIAG_STALL_SECONDS', '600'))

    def run():
        last, since, dumped = None, time.monotonic(), False
        while is_running():
            time.sleep(interval)
            try:
                progress = read_progress()
            except Exception:  # noqa: BLE001  見張りの失敗でジョブを止めない
                continue
            if progress != last:
                last, since, dumped = progress, time.monotonic(), False
            elif not dumped and time.monotonic() - since >= stall:
                dumped = True
                log(f'ジョブが {stall:.0f} 秒進んでいません（{progress}）。各スレッドの状態:\n{thread_stacks()}')

    thread = threading.Thread(target=run, daemon=True, name='job-watchdog')
    thread.start()
    return thread
