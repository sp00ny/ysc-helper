"""多线程递归探测、Single-Flight 去重与测速引擎（对应 Android 版 engine/MultiThreadProbeEngine.kt）。

无死锁设计（Python 线程版）：
* 探测池（probe_io）中的任务 **只做网络 I/O + 解析**，永远不会阻塞等待子任务；
* 多仓展开采用「续体回调」（continuation）：父节点在 Single-Flight Future 完成后派生子任务，
  由最后一个完成的子任务回调组装父节点。线程池中不存在「父线程占着工作线程等子线程」的情况，
  因此无论嵌套多深、并发多小都不会线程饥饿死锁；
* 全局 HTTP 并发由探测池大小（4~32）严格锁定，彻底终结原版 5×4³=320 的扇出失控；
* Single-Flight：``dict[normalized_key, Future]``，相同 URL 全局仅请求一次，所有引用方共享真实结果；
* ancestors 路径集合自动阻断 A → B → A 循环多仓。
"""
from __future__ import annotations

import itertools
import threading
import time
from concurrent.futures import Future, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, List, Optional, Protocol

from ..models import (ExportedUrlEntry, ExportedUrlsConfig, PayloadFormat, ProbeSettings, RepoKind,
                      RepoNode, ScanProgress, ScanStage, ScanStatistics, SecurityFlag, SeedLine, SpeedGrade)
from ..network import HttpFetchOutcome
from ..parser import parse_payload
from ..security import Invalid, clean_source_url, normalize_url, validate_and_prepare_url
from . import source_seed_provider
from .html_url_extractor import extract_candidate_urls
from .thread_pool_manager import ThreadPoolManager


class HttpFetcher(Protocol):
    def fetch(self, raw_url: str, connect_timeout_ms: int = ..., read_timeout_ms: int = ...,
              https_only: bool = ...) -> HttpFetchOutcome: ...

    def cancel_all_calls(self) -> None: ...

    def reset_cancel(self) -> None: ...

    def in_flight_call_count(self) -> int: ...


class ScanCancelled(Exception):
    pass


@dataclass
class PipelineOutput:
    roots: List[RepoNode]
    exported_config: ExportedUrlsConfig
    statistics: ScanStatistics
    duration_ms: int


@dataclass
class _SFResult:
    ok: bool
    kind: RepoKind = RepoKind.UNKNOWN
    format: PayloadFormat = PayloadFormat.UNRECOGNIZED
    detected_name: str = ""
    site_count: int = 0
    ttfb_ms: Optional[int] = None
    total_ms: Optional[int] = None
    size_kb: Optional[float] = None
    speed_kbps: Optional[float] = None
    security_flags: List[SecurityFlag] = field(default_factory=list)
    sub_entries: List[SeedLine] = field(default_factory=list)
    error: str = ""


def _median(values: List[int]) -> Optional[int]:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 == 1 else (s[mid - 1] + s[mid]) // 2


class MultiThreadProbeEngine:
    def __init__(self, pools: ThreadPoolManager, http: HttpFetcher) -> None:
        self.pools = pools
        self.http = http
        self._ids = itertools.count(1)
        self._sf_hits = 0
        self._hits_lock = threading.Lock()
        self._cancel = threading.Event()

    # ---- 遥测 / 取消 ----
    def current_single_flight_hits(self) -> int:
        return self._sf_hits

    def cancel(self) -> None:
        self._cancel.set()
        self.http.cancel_all_calls()
        self.pools.release_probe_pool()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # ---- 主流水线 ----
    def execute_full_scan(self, custom_seeds: List[SeedLine], include_builtin_seeds: bool,
                          scrape_aggregators: bool, settings: ProbeSettings,
                          on_progress: Callable[[ScanProgress], None],
                          builtin_seeds: Optional[List[SeedLine]] = None,
                          aggregator_pages: Optional[List[str]] = None) -> PipelineOutput:
        self._cancel.clear()
        self.http.reset_cancel()
        self._sf_hits = 0
        settings = settings.clamped()
        self.pools.update_concurrency(settings.max_concurrency)
        start = time.monotonic()
        last_emit = [0.0]
        emit_lock = threading.Lock()

        def emit(stage: ScanStage, done: int, total: int, url: str, force: bool = False) -> None:
            now = time.monotonic()
            with emit_lock:
                if not (force or now - last_emit[0] >= 0.075 or done >= total):
                    return
                last_emit[0] = now
            on_progress(ScanProgress(stage, done, total, url, int((now - start) * 1000)))

        # ============ 阶段 1：搜集聚合页 + 合并种子 ============
        merged: Dict[str, SeedLine] = {}

        def add_seed(seed: SeedLine) -> None:
            v = validate_and_prepare_url(seed.url, https_only=settings.https_only)
            if not isinstance(v, Invalid) and v.normalized_key not in merged:
                merged[v.normalized_key] = seed

        for s in custom_seeds:
            add_seed(s)
        if include_builtin_seeds:
            for s in (builtin_seeds if builtin_seeds is not None else source_seed_provider.DEFAULT_BASE_LINES):
                add_seed(s)

        if scrape_aggregators:
            pages = aggregator_pages if aggregator_pages is not None else source_seed_provider.DEFAULT_AGGREGATOR_PAGES
            emit(ScanStage.SCRAPING, 0, len(pages), "启动聚合爬虫线程池...", force=True)

            def scrape_one(page: str) -> List[str]:
                if self.cancelled:
                    return []
                r = self.http.fetch(page, connect_timeout_ms=4000, read_timeout_ms=5000,
                                    https_only=settings.https_only)
                if r.ok and r.body_bytes:
                    html = r.body_bytes.decode("utf-8", errors="replace")
                    return self.pools.cpu_parse.submit(extract_candidate_urls, html).result()
                return []

            futs = {self.pools.scrape.submit(scrape_one, p): p for p in pages}
            done_cnt = 0
            discovered: List[str] = []
            for f in as_completed(futs):
                if self.cancelled:
                    raise ScanCancelled()
                done_cnt += 1
                try:
                    discovered.extend(f.result())
                except Exception:
                    pass
                emit(ScanStage.SCRAPING, done_cnt, len(pages), futs[f])
            for u in discovered:
                add_seed(SeedLine("", u))

        if self.cancelled:
            raise ScanCancelled()
        top_seeds = list(merged.values())

        # ============ 阶段 2：全局有界并发递归探测 + Single-Flight ============
        io_pool = self.pools.new_probe_pool()
        sf_map: Dict[str, Future] = {}
        sf_lock = threading.Lock()
        expanded: set = set()
        counters = {"planned": len(top_seeds), "done": 0}
        cnt_lock = threading.Lock()

        emit(ScanStage.PROBING, 0, counters["planned"], "分配多线程探测任务...", force=True)

        def fetch_and_verify(raw_url: str) -> _SFResult:
            first = self.http.fetch(raw_url, connect_timeout_ms=settings.connect_timeout_ms,
                                    read_timeout_ms=settings.read_timeout_ms, https_only=settings.https_only)
            if not first.ok:
                return _SFResult(ok=False, ttfb_ms=first.ttfb_ms, total_ms=first.total_ms,
                                 error=first.error or "请求失败")
            parsed = self.pools.cpu_parse.submit(parse_payload, first.body_bytes).result()
            if parsed is None:
                return _SFResult(ok=False, ttfb_ms=first.ttfb_ms, total_ms=first.total_ms, size_kb=first.size_kb,
                                 error="响应不是有效的影视仓配置(JSON/加密/隐写)")
            if parsed.kind == RepoKind.UNKNOWN:
                return _SFResult(ok=False, format=parsed.format, ttfb_ms=first.ttfb_ms, total_ms=first.total_ms,
                                 size_kb=first.size_kb, error="配置结构不可识别(非单仓/多仓)")
            ttfbs = [first.ttfb_ms] if first.ttfb_ms is not None else []
            totals = [first.total_ms] if first.total_ms is not None else []
            for _ in range(max(0, min(4, settings.speed_samples - 1))):
                if self.cancelled:
                    break
                r = self.http.fetch(raw_url, connect_timeout_ms=settings.connect_timeout_ms,
                                    read_timeout_ms=settings.read_timeout_ms, https_only=settings.https_only)
                if r.ok:
                    if r.ttfb_ms is not None:
                        ttfbs.append(r.ttfb_ms)
                    if r.total_ms is not None:
                        totals.append(r.total_ms)
            mt, mtot = _median(ttfbs), _median(totals)
            size_kb = first.size_kb or 0.0
            speed = (size_kb / max(mtot - mt, 1) * 1000.0) if (mt is not None and mtot is not None and size_kb > 0) else None
            flags = [SecurityFlag.HTTPS_SAFE if first.is_https else SecurityFlag.CLEARTEXT_HTTP]
            if parsed.has_cleartext_spider_jar:
                flags.append(SecurityFlag.CLEARTEXT_SPIDER_JAR)
            return _SFResult(ok=True, kind=parsed.kind, format=parsed.format, detected_name=parsed.name,
                             site_count=parsed.site_count, ttfb_ms=mt, total_ms=mtot, size_kb=first.size_kb,
                             speed_kbps=speed, security_flags=flags, sub_entries=parsed.sub_entries)

        def io_task(raw_url: str, fut: Future) -> None:
            try:
                res = _SFResult(ok=False, error="已取消") if self.cancelled else fetch_and_verify(raw_url)
            except Exception as e:  # 永远保证 Future 被完成，避免等待方悬挂
                res = _SFResult(ok=False, error=f"内部错误: {e.__class__.__name__}")
            fut.set_result(res)

        def finish(node: RepoNode, url: str, on_done: Callable[[RepoNode], None]) -> None:
            with cnt_lock:
                counters["done"] += 1
                d, t = counters["done"], counters["planned"]
            emit(ScanStage.PROBING, d, t, url)
            on_done(node)

        def probe(seed: SeedLine, depth: int, ancestors: FrozenSet[str],
                  on_done: Callable[[RepoNode], None]) -> None:
            node_id = f"node-{next(self._ids)}"
            base = dict(id=node_id, name=seed.name, url=seed.url, depth=depth)
            try:
                if self.cancelled:
                    finish(RepoNode(normalized_url=seed.url.strip(), error="已取消", **base), seed.url, on_done)
                    return
                v = validate_and_prepare_url(seed.url, https_only=settings.https_only)
                if isinstance(v, Invalid):
                    finish(RepoNode(normalized_url=seed.url.strip(), error=v.reason, **base), seed.url, on_done)
                    return
                key = v.normalized_key
                if key in ancestors:
                    finish(RepoNode(normalized_url=key, error="检测到多仓循环引用，已自动断环", **base), seed.url, on_done)
                    return
                with sf_lock:
                    fut = sf_map.get(key)
                    owner = fut is None
                    if owner:
                        fut = Future()
                        sf_map[key] = fut
                if owner:
                    try:
                        io_pool.submit(io_task, seed.url, fut)
                    except RuntimeError:  # 池已因取消而关闭
                        fut.set_result(_SFResult(ok=False, error="已取消"))
                else:
                    with self._hits_lock:
                        self._sf_hits += 1

                def cont(f: Future) -> None:
                    try:
                        after_fetch(seed, depth, ancestors, key, node_id, f.result(), not owner, on_done)
                    except Exception as e:
                        finish(RepoNode(normalized_url=key, error=f"内部错误: {e}", **base), seed.url, on_done)

                fut.add_done_callback(cont)
            except Exception as e:
                finish(RepoNode(normalized_url=seed.url, error=f"内部错误: {e}", **base), seed.url, on_done)

        def after_fetch(seed: SeedLine, depth: int, ancestors: FrozenSet[str], key: str, node_id: str,
                        res: _SFResult, from_cache: bool, on_done: Callable[[RepoNode], None]) -> None:
            name = seed.name.strip() or res.detected_name
            common = dict(id=node_id, name=name, url=seed.url, normalized_url=key, depth=depth)
            if not res.ok:
                finish(RepoNode(kind=res.kind, ttfb_ms=res.ttfb_ms, total_ms=res.total_ms, size_kb=res.size_kb,
                                payload_format=res.format, is_shared_ref=from_cache, error=res.error, **common),
                       seed.url, on_done)
                return
            if res.kind == RepoKind.SINGLE:
                finish(RepoNode(kind=RepoKind.SINGLE, available=True,
                                speed_grade=SpeedGrade.from_latency(True, res.total_ms), ttfb_ms=res.ttfb_ms,
                                total_ms=res.total_ms, size_kb=res.size_kb, speed_kbps=res.speed_kbps,
                                site_count=res.site_count, payload_format=res.format,
                                security_flags=list(res.security_flags), is_shared_ref=from_cache, **common),
                       seed.url, on_done)
                return
            if depth >= settings.max_depth:
                finish(RepoNode(kind=res.kind, ttfb_ms=res.ttfb_ms, total_ms=res.total_ms, payload_format=res.format,
                                security_flags=list(res.security_flags),
                                error=f"超过最大嵌套深度({settings.max_depth})", **common), seed.url, on_done)
                return
            with sf_lock:
                first_expand = key not in expanded
                expanded.add(key)
            if not first_expand:
                finish(RepoNode(kind=res.kind, is_shared_ref=True, error="该多仓已在其他分支展开", **common),
                       seed.url, on_done)
                return

            child_seeds = [c for c in res.sub_entries
                           if not isinstance(validate_and_prepare_url(c.url, settings.https_only), Invalid)]
            child_seeds = child_seeds[: settings.max_children_per_multi]

            def assemble(children: List[RepoNode]) -> None:
                any_ok = any(c.available for c in children)
                finish(RepoNode(kind=res.kind, available=any_ok,
                                speed_grade=SpeedGrade.from_latency(any_ok, res.total_ms), ttfb_ms=res.ttfb_ms,
                                total_ms=res.total_ms, size_kb=res.size_kb, speed_kbps=res.speed_kbps,
                                payload_format=res.format, security_flags=list(res.security_flags),
                                is_shared_ref=from_cache, error="" if any_ok else "多仓内无可用子线路",
                                children=children, **common), seed.url, on_done)

            if not child_seeds:
                assemble([])
                return
            with cnt_lock:
                counters["planned"] += len(child_seeds)
            slots: List[Optional[RepoNode]] = [None] * len(child_seeds)
            remaining = [len(child_seeds)]
            slot_lock = threading.Lock()
            next_anc = ancestors | {key}

            def make_child_cb(i: int) -> Callable[[RepoNode], None]:
                def cb(n: RepoNode) -> None:
                    with slot_lock:
                        slots[i] = n
                        remaining[0] -= 1
                        last = remaining[0] == 0
                    if last:
                        assemble([s for s in slots if s is not None])
                return cb

            for i, cs in enumerate(child_seeds):
                probe(cs, depth + 1, next_anc, make_child_cb(i))

        roots: List[Optional[RepoNode]] = [None] * len(top_seeds)
        all_done = threading.Event()
        root_remaining = [len(top_seeds)]
        root_lock = threading.Lock()

        def root_cb(i: int) -> Callable[[RepoNode], None]:
            def cb(n: RepoNode) -> None:
                with root_lock:
                    roots[i] = n
                    root_remaining[0] -= 1
                    if root_remaining[0] == 0:
                        all_done.set()
            return cb

        if not top_seeds:
            all_done.set()
        for i, s in enumerate(top_seeds):
            probe(s, 0, frozenset(), root_cb(i))

        while not all_done.wait(0.1):
            if self.cancelled:
                raise ScanCancelled()
        self.pools.release_probe_pool()
        if self.cancelled:
            raise ScanCancelled()

        # ============ 阶段 3：去重清洗并构建导出配置 ============
        root_nodes = [r for r in roots if r is not None]
        emit(ScanStage.EXPORTING, counters["done"], counters["planned"], "正在生成排序后的多仓配置...", force=True)
        cfg = build_exported_config(root_nodes)
        st = ScanStatistics.compute(root_nodes, exported_count=len(cfg.urls))
        duration = int((time.monotonic() - start) * 1000)
        emit(ScanStage.COMPLETED, counters["planned"], counters["planned"], "", force=True)
        return PipelineOutput(root_nodes, cfg, st, duration)


def build_exported_config(roots: List[RepoNode]) -> ExportedUrlsConfig:
    """收集全部可用单仓叶子，清洗 GitHub 代理前缀、去重并按真实延迟升序导出。"""
    seen: set = set()
    cands = []

    def walk(n: RepoNode) -> None:
        if n.kind.is_multi:
            for c in n.children:
                walk(c)
        elif n.available and n.kind == RepoKind.SINGLE:
            cleaned = clean_source_url(n.url)
            norm = normalize_url(cleaned)
            if norm and norm not in seen:
                seen.add(norm)
                cands.append((n.total_ms if n.total_ms is not None else float("inf"), n.name or cleaned, cleaned))

    for r in roots:
        walk(r)
    cands.sort(key=lambda c: c[0])
    return ExportedUrlsConfig([ExportedUrlEntry(name=c[1], url=c[2]) for c in cands])
