"""多线程池调度管理器（对应 Android 版 engine/ThreadPoolManager.kt）。

三层物理隔离线程池：
1. scrape   —— 聚合页并发爬虫线程池（8 线程）
2. probe_io —— 线路探测与测速网络线程池（UI 可调 4~32 并发，即全局 HTTP 并发上限）
3. cpu_parse —— 解析池（注释剥离、隐写、AES、正则；绑定 CPU 核数 2~8）
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

from ..models import ThreadPoolTelemetry


class InstrumentedPool:
    def __init__(self, workers: int, prefix: str, stats: "ThreadPoolManager") -> None:
        self.workers = workers
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix)
        self._stats = stats
        self._lock = threading.Lock()
        self.active = 0
        self.queued = 0
        self.closed = False

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future:
        with self._lock:
            self.queued += 1

        def run() -> Any:
            with self._lock:
                self.queued -= 1
                self.active += 1
            try:
                return fn(*args)
            finally:
                with self._lock:
                    self.active -= 1
                self._stats._inc_completed()

        return self._executor.submit(run)

    @property
    def pool_size(self) -> int:
        return len(getattr(self._executor, "_threads", ()))

    def shutdown(self) -> None:
        self.closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


class ThreadPoolManager:
    def __init__(self, initial_concurrency: int = 16) -> None:
        self.configured_concurrency = max(4, min(32, initial_concurrency))
        self.cpu_cores = max(2, min(8, os.cpu_count() or 2))
        self._lock = threading.Lock()
        self._completed = 0
        self.scrape = InstrumentedPool(8, "ysc-scrape", self)
        self.cpu_parse = InstrumentedPool(self.cpu_cores, "ysc-cpu-parse", self)
        self.probe_io: Optional[InstrumentedPool] = None

    def _inc_completed(self) -> None:
        with self._lock:
            self._completed += 1

    def update_concurrency(self, n: int) -> None:
        self.configured_concurrency = max(4, min(32, int(n)))

    def new_probe_pool(self) -> InstrumentedPool:
        """每次任务按当前并发设置重建探测池（Python 线程池不支持运行时扩缩容）。"""
        old = self.probe_io
        if old is not None and not old.closed:
            old.shutdown()
        self.probe_io = InstrumentedPool(self.configured_concurrency, "ysc-probe-io", self)
        return self.probe_io

    def release_probe_pool(self) -> None:
        if self.probe_io is not None:
            self.probe_io.shutdown()

    def snapshot_telemetry(self, in_flight: int, sf_hits: int) -> ThreadPoolTelemetry:
        p = self.probe_io
        return ThreadPoolTelemetry(
            configured_concurrency=self.configured_concurrency,
            active_scrape_threads=self.scrape.active,
            active_probe_io_threads=p.active if p else 0,
            probe_pool_size=(p.pool_size if p and not p.closed else 0),
            active_cpu_parse_threads=self.cpu_parse.active,
            cpu_pool_size=self.cpu_parse.pool_size,
            in_flight_http_calls=in_flight,
            queued_tasks=self.scrape.queued + self.cpu_parse.queued + (p.queued if p and not p.closed else 0),
            completed_tasks=self._completed,
            single_flight_cache_hits=sf_hits,
        )

    def close(self) -> None:
        self.scrape.shutdown()
        self.cpu_parse.shutdown()
        self.release_probe_pool()
