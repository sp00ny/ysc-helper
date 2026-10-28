"""MVVM 风格的应用控制器（对应 Android 版 viewmodel/MainViewModel.kt）。

所有网络 / 解析 / 磁盘工作都在 QThreadPool 与引擎内部线程池中执行，
通过 Qt 信号（跨线程自动排队）把结果送回 UI 线程。
"""
from __future__ import annotations

import traceback
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from ..data.repository import PersistedSnapshot, RepoScanRepository
from ..engine.probe_engine import MultiThreadProbeEngine, PipelineOutput, ScanCancelled
from ..engine.thread_pool_manager import ThreadPoolManager
from ..export import QuanjuExportReport, build_quanju_export, format_quanju_json
from ..models import (ExportedUrlsConfig, FilterState, ProbeSettings, RepoNode, ScanProgress, ScanStage,
                      ScanStatistics, SeedLine, ThreadPoolTelemetry)
from ..security import Invalid, set_allow_fake_ip, validate_and_prepare_url


class _Signals(QObject):
    progress = Signal(object)
    finished = Signal(object)      # PipelineOutput
    cancelled = Signal()
    failed = Signal(str)
    result = Signal(object)        # 通用后台任务结果


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], object], signals: _Signals) -> None:
        super().__init__()
        self.fn = fn
        self.signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:  # noqa: D401
        try:
            self.signals.result.emit(self.fn())
        except Exception as e:  # pragma: no cover
            self.signals.failed.emit(f"{e.__class__.__name__}: {e}")


class _ScanTask(QRunnable):
    def __init__(self, controller: "AppController", seeds: List[SeedLine], only_custom: bool,
                 settings: ProbeSettings) -> None:
        super().__init__()
        self.c = controller
        self.seeds = seeds
        self.only_custom = only_custom
        self.settings = settings
        self.signals = controller._scan_signals
        self.setAutoDelete(True)

    def run(self) -> None:
        c = self.c
        try:
            out = c.engine.execute_full_scan(
                custom_seeds=self.seeds, include_builtin_seeds=not self.only_custom,
                scrape_aggregators=not self.only_custom, settings=self.settings,
                on_progress=self.signals.progress.emit,
                builtin_seeds=c.builtin_seeds, aggregator_pages=c.aggregator_pages)
            c.repository.save_scan_output(out)
            self.signals.finished.emit(out)
        except ScanCancelled:
            self.signals.cancelled.emit()
        except Exception as e:
            traceback.print_exc()
            self.signals.failed.emit(str(e) or e.__class__.__name__)


class AppController(QObject):
    running_changed = Signal(bool)
    progress_changed = Signal(object)
    telemetry_changed = Signal(object)
    results_changed = Signal()
    history_changed = Signal(list)
    settings_changed = Signal(object)
    custom_seeds_changed = Signal(list)
    toast = Signal(str)
    switch_tab = Signal(int)

    TAB_TASK, TAB_RESULT, TAB_HISTORY = 0, 1, 2

    def __init__(self, repository: RepoScanRepository, http_client, builtin_seeds: Optional[List[SeedLine]] = None,
                 aggregator_pages: Optional[List[str]] = None, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.repository = repository
        self.http = http_client
        self.builtin_seeds = builtin_seeds
        self.aggregator_pages = aggregator_pages
        self.settings: ProbeSettings = repository.load_settings()
        set_allow_fake_ip(self.settings.allow_fake_ip)
        self.pools = ThreadPoolManager(self.settings.max_concurrency)
        self.engine = MultiThreadProbeEngine(self.pools, http_client)
        self.custom_seeds: List[SeedLine] = repository.load_custom_seeds()
        self.roots: List[RepoNode] = []
        self.exported_config: Optional[ExportedUrlsConfig] = None
        self.statistics = ScanStatistics()
        self.progress = ScanProgress()
        self.is_running = False
        self.filter_state = FilterState()
        self.last_quanju_report: Optional[QuanjuExportReport] = None
        self.pool = QThreadPool.globalInstance()

        self._scan_signals = _Signals()
        self._scan_signals.progress.connect(self._on_progress)
        self._scan_signals.finished.connect(self._on_finished)
        self._scan_signals.cancelled.connect(self._on_cancelled)
        self._scan_signals.failed.connect(self._on_failed)

        self._telemetry_timer = QTimer(self)
        self._telemetry_timer.setInterval(150)
        self._telemetry_timer.timeout.connect(self.refresh_telemetry)
        self._keep: list = []

    # ---------------- 后台小任务 ----------------
    def _run_bg(self, fn: Callable[[], object], on_result: Callable[[object], None]) -> None:
        sig = _Signals()
        self._keep.append(sig)

        def done(v: object) -> None:
            if sig in self._keep:
                self._keep.remove(sig)
            on_result(v)

        sig.result.connect(done)
        sig.failed.connect(lambda m: self.toast.emit(f"执行出错：{m}"))
        self.pool.start(_Task(fn, sig))

    # ---------------- 启动 ----------------
    def initialize(self) -> None:
        """启动时仅异步恢复一次最近快照。"""
        self.settings_changed.emit(self.settings)
        self.custom_seeds_changed.emit(list(self.custom_seeds))
        self.refresh_telemetry()

        def on_loaded(v: object) -> None:
            latest, hist = v  # type: ignore[misc]
            if latest is not None and not self.roots:
                self._apply_snapshot(latest[1])
            self.history_changed.emit(hist)

        self._run_bg(lambda: (self.repository.load_latest_snapshot(), self.repository.list_history()), on_loaded)

    def _apply_snapshot(self, snap: PersistedSnapshot) -> None:
        self.roots = snap.roots
        self.exported_config = snap.exported_config
        self.statistics = snap.statistics
        self.results_changed.emit()

    def refresh_history(self) -> None:
        self._run_bg(self.repository.list_history, lambda h: self.history_changed.emit(h))

    # ---------------- 设置 / 自定义源 ----------------
    def update_settings(self, s: ProbeSettings) -> None:
        self.settings = s.clamped()
        set_allow_fake_ip(self.settings.allow_fake_ip)
        self.pools.update_concurrency(s.max_concurrency)
        self.repository.update_settings(self.settings)
        self.settings_changed.emit(self.settings)
        self.refresh_telemetry()

    def add_custom_seed(self, name: str, url: str, probe_now: bool) -> bool:
        check = validate_and_prepare_url(url, https_only=self.settings.https_only)
        if isinstance(check, Invalid):
            self.toast.emit(f"地址校验未通过：{check.reason}")
            return False
        self.repository.add_custom_seed(SeedLine(name.strip(), url.strip(), True))
        self.custom_seeds = self.repository.load_custom_seeds()
        self.custom_seeds_changed.emit(list(self.custom_seeds))
        self.toast.emit("已添加自定义线路")
        if probe_now:
            self.start_scan(only_custom=True)
        return True

    def remove_custom_seed(self, url: str) -> None:
        self.repository.remove_custom_seed(url)
        self.custom_seeds = self.repository.load_custom_seeds()
        self.custom_seeds_changed.emit(list(self.custom_seeds))
        self.toast.emit("已移除自定义线路")

    # ---------------- 扫描 ----------------
    def start_scan(self, only_custom: bool = False) -> None:
        if self.is_running:
            self.toast.emit("多线程任务正在运行中...")
            return
        if only_custom and not self.custom_seeds:
            self.toast.emit("请先添加至少一条自定义接口地址")
            return
        self._set_running(True)
        self.switch_tab.emit(self.TAB_TASK)
        self._telemetry_timer.start()
        self.pool.start(_ScanTask(self, list(self.custom_seeds), only_custom,
                                  ProbeSettings(**vars(self.settings))))

    def cancel_scan(self) -> None:
        """一键即时取消：中断全部工作线程任务并关闭在途网络连接。"""
        if not self.is_running:
            return
        self.engine.cancel()

    def _set_running(self, v: bool) -> None:
        self.is_running = v
        self.running_changed.emit(v)

    def _on_progress(self, p: ScanProgress) -> None:
        if not self.is_running:
            return
        self.progress = p
        self.progress_changed.emit(p)

    def _finish_common(self) -> None:
        self._set_running(False)
        self._telemetry_timer.stop()
        self.refresh_telemetry()

    def _on_finished(self, out: PipelineOutput) -> None:
        self.roots = out.roots
        self.exported_config = out.exported_config
        self.statistics = out.statistics
        self.progress = ScanProgress(ScanStage.COMPLETED, self.progress.total, self.progress.total, "",
                                     out.duration_ms)
        self.progress_changed.emit(self.progress)
        self._finish_common()
        self.results_changed.emit()
        self.refresh_history()
        self.switch_tab.emit(self.TAB_RESULT)
        self.toast.emit(f"验证完成：共扫描 {out.statistics.total_nodes_scanned} 节点，"
                        f"导出 {out.statistics.exportable_unique_count} 条去重可用单仓")

    def _on_cancelled(self) -> None:
        self.progress = ScanProgress(ScanStage.CANCELLED, self.progress.completed, self.progress.total,
                                     "已停止所有工作线程与网络连接", self.progress.elapsed_ms)
        self.progress_changed.emit(self.progress)
        self._finish_common()
        self.toast.emit("已取消当前检测任务")

    def _on_failed(self, msg: str) -> None:
        self.progress = ScanProgress(ScanStage.FAILED, self.progress.completed, self.progress.total, msg,
                                     self.progress.elapsed_ms)
        self.progress_changed.emit(self.progress)
        self._finish_common()
        self.toast.emit(f"执行出错：{msg}")

    def refresh_telemetry(self) -> None:
        t: ThreadPoolTelemetry = self.pools.snapshot_telemetry(self.http.in_flight_call_count(),
                                                               self.engine.current_single_flight_hits())
        self.telemetry_changed.emit(t)

    # ---------------- 历史 ----------------
    def restore_history(self, hid: int) -> None:
        def done(v: object) -> None:
            if v is None:
                self.toast.emit("历史快照文件不存在或已损坏")
                return
            self._apply_snapshot(v[1])  # type: ignore[index]
            self.switch_tab.emit(self.TAB_RESULT)
            self.toast.emit("已恢复历史验证结果")

        self._run_bg(lambda: self.repository.load_snapshot_by_id(hid), done)

    def delete_history(self, hid: int) -> None:
        def done(_v: object) -> None:
            self.refresh_history()
            self.toast.emit("已删除该历史归档")

        self._run_bg(lambda: self.repository.delete_history_by_id(hid), done)

    # ---------------- 导出 ----------------
    EXPORT_TVBOX = "tvbox"    # 影视仓 / TVBox 多仓 {"urls":[...]}
    EXPORT_QUANJU = "quanju"  # 全剧视界 自定义源 {"format":"duanju-custom-sources",...}

    def has_exportable(self) -> bool:
        return bool(self.exported_config and self.exported_config.urls)

    def export_json_or_none(self) -> Optional[str]:
        cfg = self.exported_config
        if cfg is None or not cfg.urls:
            return None
        return self.repository.format_export_config_json(cfg)

    def _fetch_ext_rule_text(self, url: str) -> Optional[str]:
        """抓取 XBPQ 外链规则文件：复用带 SSRF/DNS 重绑定防护、4MB 上限的探测客户端。"""
        s = self.settings
        out = self.http.fetch(url, s.connect_timeout_ms, s.read_timeout_ms, https_only=s.https_only)
        if not out.ok or not out.body_bytes:
            return None
        raw = out.body_bytes
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("gb18030", errors="replace")

    def build_quanju_now(self) -> "tuple[Optional[str], QuanjuExportReport]":
        """同步构建全剧视界 JSON（在后台线程调用）；没有可导入站点时文本为 None。"""
        if not self.is_running and getattr(self.http, "cancelled", False):
            self.http.reset_cancel()
        payload, report = build_quanju_export(self.roots, fetch_text=self._fetch_ext_rule_text)
        return (format_quanju_json(payload) if payload["count"] else None), report

    def build_export_async(self, fmt: str, on_ready: Callable[[Optional[str], str], None]) -> None:
        """按格式生成导出文本；回调 (text 或 None, 摘要) 总在 UI 线程执行。"""
        if fmt == self.EXPORT_QUANJU:
            if not self.has_exportable():
                on_ready(None, "暂无可导出的可用单仓配置")
                return

            def done(v: object) -> None:
                text, report = v  # type: ignore[misc]
                self.last_quanju_report = report
                on_ready(text, report.summary_text() if text else
                         "没有可导入全剧视界的站点：" + report.summary_text())
            self._run_bg(self.build_quanju_now, done)
            return
        text = self.export_json_or_none()
        n = len(self.exported_config.urls) if self.exported_config else 0
        on_ready(text, f"{n} 条可用源" if text else "暂无可导出的可用单仓配置")

    def write_export_to_file(self, path: str, fmt: str = EXPORT_TVBOX) -> None:
        def ready(text: Optional[str], summary: str) -> None:
            if text is None:
                self.toast.emit(summary)
                return

            def write() -> object:
                try:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(text)
                    return None
                except OSError as e:
                    return str(e)

            def done(err: object) -> None:
                if err:
                    self.toast.emit(f"保存失败：{err}")
                elif fmt == self.EXPORT_QUANJU:
                    self.toast.emit(f"已保存全剧视界导入文件。{summary}")
                else:
                    self.toast.emit(f"配置已成功保存 ({summary})")

            self._run_bg(write, done)

        self.build_export_async(fmt, ready)

    def shutdown(self) -> None:
        self.engine.cancel()
        self.pools.close()
