"""「任务」页（对应 Android 版 ui/screens/TaskScreen.kt + ThreadPoolMonitorCard.kt）。"""
from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
                               QRadioButton, QScrollArea, QSlider, QVBoxLayout, QWidget)

from ..models import ProbeSettings, ScanProgress, ScanStage, SeedLine, ThreadPoolTelemetry
from .controller import AppController
from .widgets import Card, MetricTile, muted, title


class TaskTab(QScrollArea):
    def __init__(self, c: AppController) -> None:
        super().__init__()
        self.c = c
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        inner.setObjectName("TaskInner")
        self.setWidget(inner)
        root = QVBoxLayout(inner)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(14)

        # 1. 主任务控制面板
        card = Card()
        self.start_btn = QPushButton("▶  全网搜寻与多线程极速验证")
        self.start_btn.setObjectName("BigPrimary")
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.clicked.connect(self._on_main_button)
        card.lay.addWidget(self.start_btn)

        self.progress_box = QWidget()
        pl = QVBoxLayout(self.progress_box)
        pl.setContentsMargins(0, 8, 0, 0)
        pl.setSpacing(4)
        row = QHBoxLayout()
        self.stage_lbl = QLabel()
        self.stage_lbl.setObjectName("Accent")
        self.pct_lbl = muted()
        row.addWidget(self.stage_lbl)
        row.addStretch(1)
        row.addWidget(self.pct_lbl)
        pl.addLayout(row)
        self.url_lbl = muted()
        self.url_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pl.addWidget(self.url_lbl)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(9)
        pl.addWidget(self.bar)
        self.progress_box.setVisible(False)
        card.lay.addWidget(self.progress_box)
        card.lay.addWidget(muted("原生多线程引擎基于 requests 连接池 + CPU 解析池并发探测，支持单仓/多仓递归展开、"
                                 "图片隐写(**)、AES 加密($#)解码与 Single-Flight 智能去重。", wrap=True))
        root.addWidget(card)

        # 2. 多线程池实时监控
        mon = Card(margins=14)
        hr = QHBoxLayout()
        hr.addWidget(title("原生多线程引擎实时监控"))
        hr.addStretch(1)
        self.conc_badge = QLabel()
        self.conc_badge.setObjectName("Badge")
        hr.addWidget(self.conc_badge)
        mon.lay.addLayout(hr)
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.t_io = MetricTile("探测IO线程")
        self.t_cpu = MetricTile("CPU解析线程")
        self.t_http = MetricTile("在途HTTP")
        self.t_sf = MetricTile("去重复用")
        for t in (self.t_io, self.t_cpu, self.t_http, self.t_sf):
            tiles.addWidget(t, 1)
        mon.lay.addLayout(tiles)
        self.mon_footer = muted()
        mon.lay.addWidget(self.mon_footer)
        root.addWidget(mon)

        # 3. 并发与安全参数
        cfg = Card()
        cfg.lay.addWidget(title("多线程并发与安全参数"))
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("全局并发工作线程数"))
        r1.addStretch(1)
        self.conc_lbl = QLabel()
        self.conc_lbl.setObjectName("Accent")
        r1.addWidget(self.conc_lbl)
        cfg.lay.addLayout(r1)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(1, 8)  # 4,8,...,32
        self.slider.setPageStep(1)
        self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider.valueChanged.connect(lambda v: self.conc_lbl.setText(f"{v * 4} 线程"))
        self.slider.sliderReleased.connect(self._push_settings)
        self.slider.valueChanged.connect(lambda _v: (not self.slider.isSliderDown()) and self._push_settings())
        cfg.lay.addWidget(self.slider)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("每节点测速采样次数"))
        r2.addStretch(1)
        self.samples = QButtonGroup(self)
        for cnt, label in ((1, "1次(极速复用)"), (2, "2次"), (3, "3次(中位数)")):
            rb = QRadioButton(label)
            self.samples.addButton(rb, cnt)
            r2.addWidget(rb)
        self.samples.idClicked.connect(lambda _i: self._push_settings())
        cfg.lay.addLayout(r2)

        r3 = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(QLabel("严格仅允许 HTTPS 接口"))
        col.addWidget(muted("开启后自动拦截所有明文 http:// 源以防中间人篡改 JAR 插件"))
        r3.addLayout(col, 1)
        self.https_only = QCheckBox("启用")
        self.https_only.toggled.connect(lambda _v: self._push_settings())
        r3.addWidget(self.https_only)
        cfg.lay.addLayout(r3)

        r5 = QHBoxLayout()
        col5 = QVBoxLayout()
        col5.setSpacing(2)
        col5.addWidget(QLabel("兼容代理 Fake-IP 模式 (198.18.0.0/15)"))
        col5.addWidget(muted("使用 Clash / Surge 等 TUN 或 Fake-IP 代理时所有域名都会解析到 198.18.x.x，"
                             "开启后放行该网段（仅限域名解析结果，IP 字面量仍拦截）", wrap=True))
        r5.addLayout(col5, 1)
        self.fake_ip = QCheckBox("启用")
        self.fake_ip.toggled.connect(lambda _v: self._push_settings())
        r5.addWidget(self.fake_ip)
        cfg.lay.addLayout(r5)
        root.addWidget(cfg)

        # 4. 自定义接口源
        cs = Card()
        hr2 = QHBoxLayout()
        self.cs_title = title("自定义接口源 (0)")
        hr2.addWidget(self.cs_title)
        hr2.addStretch(1)
        self.only_custom_btn = QPushButton("仅验证自定义源")
        self.only_custom_btn.clicked.connect(lambda: self.c.start_scan(only_custom=True))
        hr2.addWidget(self.only_custom_btn)
        cs.lay.addLayout(hr2)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("备注名称 (可选)")
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("单仓或多仓接口 URL (支持中文域名)")
        self.url_edit.textChanged.connect(self._sync_enabled)
        self.url_edit.returnPressed.connect(lambda: self._add(False))
        cs.lay.addWidget(self.name_edit)
        cs.lay.addWidget(self.url_edit)
        r4 = QHBoxLayout()
        self.add_btn = QPushButton("＋ 加入列表")
        self.add_btn.clicked.connect(lambda: self._add(False))
        self.add_probe_btn = QPushButton("添加并立即测速")
        self.add_probe_btn.setObjectName("Primary")
        self.add_probe_btn.clicked.connect(lambda: self._add(True))
        r4.addWidget(self.add_btn, 1)
        r4.addWidget(self.add_probe_btn, 1)
        cs.lay.addLayout(r4)
        self.seed_list = QVBoxLayout()
        self.seed_list.setSpacing(2)
        cs.lay.addLayout(self.seed_list)
        root.addWidget(cs)
        root.addStretch(1)

        self._running = False
        self._seeds: List[SeedLine] = []
        self._loading = False
        c.running_changed.connect(self._on_running)
        c.progress_changed.connect(self._on_progress)
        c.telemetry_changed.connect(self._on_telemetry)
        c.settings_changed.connect(self._on_settings)
        c.custom_seeds_changed.connect(self._on_seeds)
        self._sync_enabled()

    # ---------------- 交互 ----------------
    def _on_main_button(self) -> None:
        if self._running:
            self.c.cancel_scan()
        else:
            self.c.start_scan(only_custom=False)

    def _push_settings(self) -> None:
        if self._loading:
            return
        s = ProbeSettings(max_concurrency=self.slider.value() * 4,
                          speed_samples=max(1, self.samples.checkedId()),
                          https_only=self.https_only.isChecked(),
                          allow_fake_ip=self.fake_ip.isChecked())
        self.c.update_settings(s)

    def _add(self, probe_now: bool) -> None:
        url = self.url_edit.text().strip()
        if not url or self._running:
            return
        if self.c.add_custom_seed(self.name_edit.text(), url, probe_now):
            self.name_edit.clear()
            self.url_edit.clear()

    def _sync_enabled(self) -> None:
        has_url = bool(self.url_edit.text().strip())
        self.add_btn.setEnabled(has_url and not self._running)
        self.add_probe_btn.setEnabled(has_url and not self._running)
        self.only_custom_btn.setVisible(bool(self._seeds) and not self._running)
        for w in (self.slider, self.https_only, self.fake_ip, *self.samples.buttons()):
            w.setEnabled(not self._running)

    # ---------------- 状态回调 ----------------
    def _on_running(self, running: bool) -> None:
        self._running = running
        if running:
            self.start_btn.setText("■  立即停止多线程任务")
            self.start_btn.setObjectName("BigDanger")
        else:
            self.start_btn.setText("▶  全网搜寻与多线程极速验证")
            self.start_btn.setObjectName("BigPrimary")
        self.start_btn.style().unpolish(self.start_btn)
        self.start_btn.style().polish(self.start_btn)
        self._sync_enabled()

    def _on_progress(self, p: ScanProgress) -> None:
        self.progress_box.setVisible(p.stage != ScanStage.IDLE)
        suffix = f" ({p.completed}/{p.total})" if p.total > 0 else ""
        self.stage_lbl.setText(p.stage.title + suffix)
        self.pct_lbl.setText(f"{p.percent}% · {p.elapsed_ms // 1000}s")
        fm = self.url_lbl.fontMetrics()
        self.url_lbl.setText(fm.elidedText(p.current_url, Qt.TextElideMode.ElideMiddle,
                                           max(200, self.url_lbl.width())))
        self.url_lbl.setToolTip(p.current_url)
        self.bar.setValue(int(p.fraction * 1000))

    def _on_telemetry(self, t: ThreadPoolTelemetry) -> None:
        self.conc_badge.setText(f"并发上限 {t.configured_concurrency} 线程")
        self.t_io.set_value(f"{t.active_probe_io_threads}/{t.probe_pool_size}")
        self.t_cpu.set_value(f"{t.active_cpu_parse_threads}/{t.cpu_pool_size}")
        self.t_http.set_value(str(t.in_flight_http_calls))
        self.t_sf.set_value(str(t.single_flight_cache_hits))
        self.mon_footer.setText(f"爬虫活跃线程: {t.active_scrape_threads} · 待执行队列: {t.queued_tasks} · "
                                f"已完成线程任务: {t.completed_tasks}")

    def _on_settings(self, s: ProbeSettings) -> None:
        self._loading = True
        self.slider.setValue(max(1, min(8, round(s.max_concurrency / 4))))
        self.conc_lbl.setText(f"{self.slider.value() * 4} 线程")
        btn = self.samples.button(s.speed_samples)
        if btn:
            btn.setChecked(True)
        self.https_only.setChecked(s.https_only)
        self.fake_ip.setChecked(s.allow_fake_ip)
        self._loading = False

    def _on_seeds(self, seeds: List[SeedLine]) -> None:
        self._seeds = seeds
        self.cs_title.setText(f"自定义接口源 ({len(seeds)})")
        while self.seed_list.count():
            item = self.seed_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for s in seeds:
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 2, 0, 2)
            col = QVBoxLayout()
            col.setSpacing(0)
            if s.name:
                nl = QLabel(s.name)
                nl.setStyleSheet("font-weight: 600; font-size: 12px;")
                col.addWidget(nl)
            ul = muted(s.url)
            ul.setToolTip(s.url)
            col.addWidget(ul)
            h.addLayout(col, 1)
            d = QPushButton("删除")
            d.setObjectName("LinkDanger")
            d.clicked.connect(lambda _=False, u=s.url: self.c.remove_custom_seed(u))
            h.addWidget(d)
            self.seed_list.addWidget(w)
        self._sync_enabled()
