"""「结果」页（对应 Android 版 ui/screens/ResultScreen.kt + RepoTreeCard.kt）。

QTreeWidget 树形展示多仓 / 子仓，支持展开 / 收起、180ms 防抖搜索、类型 / 可用性 / 速度筛选，
筛选在子节点命中时自动保留并展开父多仓。
"""
from __future__ import annotations

from typing import Optional, Set

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QPushButton,
                               QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..models import FilterState, PayloadFormat, RepoKind, RepoNode, SecurityFlag, SpeedGrade
from ..tree_filter import FilteredTreeData, compute_filtered_tree
from . import theme
from .controller import AppController
from .widgets import MetricTile, muted

COLUMNS = ["名称", "类型", "状态", "速度", "延迟", "TTFB", "速率", "详情", "安全", "接口地址"]
C_NAME, C_KIND, C_STATUS, C_SPEED, C_LAT, C_TTFB, C_RATE, C_DETAIL, C_SEC, C_URL = range(len(COLUMNS))
ROLE_NODE_ID = Qt.ItemDataRole.UserRole + 1
ROLE_URL = Qt.ItemDataRole.UserRole + 2


def _fmt_speed(kbps: Optional[float]) -> str:
    if kbps is None:
        return ""
    return f"{kbps / 1024:.2f}MB/s" if kbps >= 1024 else f"{kbps:.0f}KB/s"


class ResultTab(QWidget):
    def __init__(self, c: AppController) -> None:
        super().__init__()
        self.c = c
        self.expanded_ids: Set[str] = set()
        self.filtered = FilteredTreeData()
        self.filter = FilterState()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)

        # 1. 统计摘要
        stats = QHBoxLayout()
        stats.setSpacing(8)
        self.s_total = MetricTile("扫描总节点", tile=False)
        self.s_export = MetricTile("去重可用单仓", tile=False)
        self.s_multi = MetricTile("有效多仓", tile=False)
        self.s_lat = MetricTile("平均延迟", "-", tile=False)
        for w in (self.s_total, self.s_export, self.s_multi, self.s_lat):
            stats.addWidget(w, 1)
        lay.addLayout(stats)

        # 2. 搜索框（180ms 防抖）
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍  搜索线路名称或接口 URL...")
        self.search.setClearButtonEnabled(True)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(180)
        self._debounce.timeout.connect(self._apply_keyword)
        self.search.textChanged.connect(self._on_keyword)
        lay.addWidget(self.search)

        # 3. 筛选标签
        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.chip_avail = self._chip("仅看可用")
        self.chip_single = self._chip("单线路")
        self.chip_store = self._chip("多仓(store)")
        self.chip_urls = self._chip("多仓(urls)")
        self.chip_fast = self._chip("极速(<500ms)")
        for ch in (self.chip_avail, self.chip_single, self.chip_store, self.chip_urls, self.chip_fast):
            chips.addWidget(ch)
        chips.addStretch(1)
        lay.addLayout(chips)

        # 4. 操作栏
        ops = QHBoxLayout()
        self.match_lbl = muted("匹配 0 条")
        ops.addWidget(self.match_lbl)
        b_exp = QPushButton("全部展开")
        b_exp.setObjectName("Link")
        b_exp.clicked.connect(lambda: self.expand_or_collapse_all(True))
        b_col = QPushButton("全部收起")
        b_col.setObjectName("Link")
        b_col.clicked.connect(lambda: self.expand_or_collapse_all(False))
        ops.addWidget(b_exp)
        ops.addWidget(b_col)
        ops.addStretch(1)
        ops.addWidget(muted("导出格式"))
        self.format_combo = QComboBox()
        self.format_combo.setObjectName("ExportFormat")
        self.format_combo.addItem("影视仓 / TVBox 多仓", AppController.EXPORT_TVBOX)
        self.format_combo.addItem("全剧视界 自定义源", AppController.EXPORT_QUANJU)
        self.format_combo.setItemData(0, "{\"urls\":[{name,url}]} —— 影视仓/TVBox「多仓」直接加载", Qt.ToolTipRole)
        self.format_combo.setItemData(
            1, "{\"format\":\"duanju-custom-sources\",...} —— 全剧视界「自定义源 → 导入（文件）」\n"
               "仅包含 MacCMS 采集接口站与 XBPQ 规则站；jar/JS 爬虫站点无法在全剧视界运行，会被跳过", Qt.ToolTipRole)
        self.format_combo.setToolTip("选择「复制JSON」与「导出配置」使用的格式")
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        ops.addWidget(self.format_combo)
        self.copy_btn = QPushButton("⧉ 复制JSON")
        self.copy_btn.clicked.connect(self._copy_json)
        self.export_btn = QPushButton("⤓ 导出配置")
        self.export_btn.setObjectName("Primary")
        self.export_btn.clicked.connect(self._export)
        ops.addWidget(self.copy_btn)
        ops.addWidget(self.export_btn)
        lay.addLayout(ops)
        self.export_hint = muted("")
        self.export_hint.setWordWrap(True)
        self.export_hint.setVisible(False)
        lay.addWidget(self.export_hint)

        # 5. 树形列表
        self.stack = QStackedWidget()
        self.empty = QLabel()
        self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(18)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemExpanded.connect(lambda it: self.expanded_ids.add(it.data(0, ROLE_NODE_ID)))
        self.tree.itemCollapsed.connect(lambda it: self.expanded_ids.discard(it.data(0, ROLE_NODE_ID)))
        hdr = self.tree.header()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, w in ((C_NAME, 230), (C_KIND, 132), (C_STATUS, 62), (C_SPEED, 50), (C_LAT, 70), (C_TTFB, 70),
                       (C_RATE, 80), (C_DETAIL, 250), (C_SEC, 120)):
            self.tree.setColumnWidth(col, w)
        QShortcut(QKeySequence.StandardKey.Copy, self.tree, activated=self._copy_selected_url)
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.tree)
        lay.addWidget(self.stack, 1)

        c.results_changed.connect(self._on_results)
        self._on_results()

    # ---------------- 筛选 ----------------
    def _chip(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("Chip")
        b.setCheckable(True)
        b.toggled.connect(lambda _v, b=b: self._on_chip(b))
        return b

    def _on_chip(self, b: QPushButton) -> None:
        kinds = {self.chip_single: RepoKind.SINGLE, self.chip_store: RepoKind.MULTI_STOREHOUSE,
                 self.chip_urls: RepoKind.MULTI_URLS}
        if b in kinds and b.isChecked():  # 类型筛选互斥
            for other in kinds:
                if other is not b and other.isChecked():
                    other.blockSignals(True)
                    other.setChecked(False)
                    other.blockSignals(False)
        kind = next((k for btn, k in kinds.items() if btn.isChecked()), None)
        self.filter = FilterState(keyword=self.filter.keyword, kind=kind,
                                  available_only=True if self.chip_avail.isChecked() else None,
                                  speed_grade=SpeedGrade.FAST if self.chip_fast.isChecked() else None)
        self._recompute()

    def _on_keyword(self, text: str) -> None:
        if not text:
            self._debounce.stop()
            self._apply_keyword()
        else:
            self._debounce.start()

    def _apply_keyword(self) -> None:
        f = self.filter
        self.filter = FilterState(self.search.text(), f.kind, f.available_only, f.speed_grade)
        self._recompute()

    def _on_results(self) -> None:
        st = self.c.statistics
        self.s_total.set_value(str(st.total_nodes_scanned))
        self.s_export.set_value(str(st.exportable_unique_count), theme.STATUS_OK_GREEN)
        self.s_multi.set_value(str(st.available_multis))
        self.s_lat.set_value(f"{st.avg_latency_ms}ms" if st.avg_latency_ms is not None else "-")
        can_export = st.exportable_unique_count > 0
        self.copy_btn.setEnabled(can_export)
        self.export_btn.setEnabled(can_export)
        self._on_format_changed()
        self._recompute()

    def _recompute(self) -> None:
        self.filtered = compute_filtered_tree(self.c.roots, self.filter)
        self.match_lbl.setText(f"匹配 {self.filtered.matched_node_count} 条")
        self._rebuild_tree()

    # ---------------- 树 ----------------
    def _rebuild_tree(self) -> None:
        roots = self.filtered.visible_roots
        if not roots:
            self.empty.setText("尚无检测结果，请前往「任务」页启动多线程搜寻验证"
                               if self.c.statistics.total_nodes_scanned == 0 and not self.c.roots
                               else "当前筛选条件下无匹配线路")
            self.stack.setCurrentWidget(self.empty)
            self.tree.clear()
            return
        self.stack.setCurrentWidget(self.tree)
        effective = self.expanded_ids | self.filtered.auto_expanded_ids
        self.tree.setUpdatesEnabled(False)
        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            pending = []
            for r in roots:
                it = self._make_item(r)
                self.tree.addTopLevelItem(it)
                pending.append((it, r))
            while pending:
                it, node = pending.pop()
                for ch in node.children:
                    ci = self._make_item(ch)
                    it.addChild(ci)
                    pending.append((ci, ch))
                if node.id in effective and node.children:
                    it.setExpanded(True)
        finally:
            self.tree.blockSignals(False)
            self.tree.setUpdatesEnabled(True)

    def _make_item(self, n: RepoNode) -> QTreeWidgetItem:
        it = QTreeWidgetItem()
        it.setData(0, ROLE_NODE_ID, n.id)
        it.setData(0, ROLE_URL, n.url)
        dark = self.c_dark()
        base_fg = QColor(theme.DARK.on_surface if dark else theme.LIGHT.on_surface)
        muted_fg = QColor(theme.DARK.muted if dark else theme.LIGHT.muted)

        name = n.name or n.url
        if n.is_shared_ref:
            name += "  ↺"
        it.setText(C_NAME, name)
        f = QFont()
        f.setBold(True)
        it.setFont(C_NAME, f)

        if n.kind.is_multi:
            kind_txt, kind_col = n.kind.label, theme.ACCENT_PURPLE
        elif n.kind == RepoKind.SINGLE:
            kind_txt, kind_col = "单线路", theme.PRIMARY_BLUE
        else:
            kind_txt, kind_col = "未知", theme.NEUTRAL_GRAY
        it.setText(C_KIND, kind_txt)
        it.setForeground(C_KIND, QBrush(QColor(kind_col)))

        it.setText(C_STATUS, "可用" if n.available else "不可用")
        it.setForeground(C_STATUS, QBrush(QColor(theme.STATUS_OK_GREEN if n.available else theme.STATUS_BAD_RED)))
        it.setFont(C_STATUS, f)

        if n.available and not n.kind.is_multi:
            sc = {SpeedGrade.FAST: theme.STATUS_OK_GREEN, SpeedGrade.MEDIUM: theme.STATUS_WARN_AMBER}.get(
                n.speed_grade, theme.STATUS_BAD_RED)
            it.setText(C_SPEED, n.speed_grade.label)
            it.setForeground(C_SPEED, QBrush(QColor(sc)))
        if n.total_ms is not None:
            it.setText(C_LAT, f"{n.total_ms}ms")
        if n.ttfb_ms is not None:
            it.setText(C_TTFB, f"{n.ttfb_ms}ms")
        if n.available:
            it.setText(C_RATE, _fmt_speed(n.speed_kbps))

        details = []
        if n.available:
            if n.kind.is_multi:
                details.append(f"子仓 {len(n.children)} 个 · ✓ {n.available_child_count} 可用")
            elif n.site_count > 0:
                details.append(f"站点 {n.site_count}")
            if n.payload_format not in (PayloadFormat.PLAIN_JSON, PayloadFormat.UNRECOGNIZED):
                details.append(n.payload_format.label)
            if n.is_shared_ref:
                details.append("去重复用")
        else:
            details.append(f"原因：{n.error or '不可用'}")
            it.setForeground(C_DETAIL, QBrush(QColor(theme.STATUS_BAD_RED)))
        it.setText(C_DETAIL, " · ".join(details))

        if n.available:
            if SecurityFlag.CLEARTEXT_SPIDER_JAR in n.security_flags:
                it.setText(C_SEC, "⚠ 明文JAR插件风险")
                it.setForeground(C_SEC, QBrush(QColor(theme.STATUS_BAD_RED)))
            elif SecurityFlag.CLEARTEXT_HTTP in n.security_flags:
                it.setText(C_SEC, "HTTP明文")
                it.setForeground(C_SEC, QBrush(QColor(theme.STATUS_WARN_AMBER)))
            elif SecurityFlag.HTTPS_SAFE in n.security_flags:
                it.setText(C_SEC, "🔒 HTTPS")
                it.setForeground(C_SEC, QBrush(QColor(theme.STATUS_OK_GREEN)))

        it.setText(C_URL, n.url)
        it.setForeground(C_URL, QBrush(muted_fg))
        if not n.available:
            fg = QColor(base_fg)
            fg.setAlphaF(0.68)
            it.setForeground(C_NAME, QBrush(fg))
        it.setToolTip(C_NAME, f"{n.name}\n{n.url}")
        it.setToolTip(C_URL, n.url)
        it.setToolTip(C_DETAIL, it.text(C_DETAIL))
        return it

    def c_dark(self) -> bool:
        w = self.window()
        return bool(getattr(w, "dark", False))

    def expand_or_collapse_all(self, expand: bool) -> None:
        if not expand:
            self.expanded_ids = set()
        else:
            ids: Set[str] = set()
            stack = list(self.c.roots)
            while stack:
                n = stack.pop()
                if n.kind.is_multi and n.children:
                    ids.add(n.id)
                    stack.extend(n.children)
            self.expanded_ids = ids
        self._rebuild_tree()
        if not expand:
            self.tree.collapseAll()

    # ---------------- 复制 / 导出 ----------------
    def _context_menu(self, pos: QPoint) -> None:
        it = self.tree.itemAt(pos)
        if it is None:
            return
        m = QMenu(self)
        url = it.data(0, ROLE_URL)
        m.addAction("复制接口地址", lambda: self._copy_text(url, "已复制接口地址"))
        m.addAction("复制名称", lambda: self._copy_text(it.text(C_NAME), "已复制到剪贴板"))
        if it.childCount():
            m.addSeparator()
            m.addAction("收起子仓" if it.isExpanded() else "展开子仓", lambda: it.setExpanded(not it.isExpanded()))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _copy_selected_url(self) -> None:
        it = self.tree.currentItem()
        if it is not None:
            self._copy_text(it.data(0, ROLE_URL), "已复制接口地址")

    def _copy_text(self, text: str, msg: str) -> None:
        QGuiApplication.clipboard().setText(text or "")
        self.c.toast.emit(msg)

    # ---------------- 导出 ----------------
    @property
    def export_format(self) -> str:
        return self.format_combo.currentData() or AppController.EXPORT_TVBOX

    def _on_format_changed(self, _i: int = 0) -> None:
        if self.export_format == AppController.EXPORT_QUANJU:
            self.export_hint.setText("全剧视界：导出为「自定义源」导入文件（全剧视界 → 自定义源 → 导入 → 文件）。"
                                     "仅收录 MacCMS 采集接口与 XBPQ 规则站点，jar/JS 爬虫站点会被跳过。")
            self.export_hint.setVisible(True)
        else:
            self.export_hint.setVisible(False)

    def _set_busy(self, busy: bool) -> None:
        can = self.c.has_exportable() and not busy
        self.copy_btn.setEnabled(can)
        self.export_btn.setEnabled(can)

    def _show_summary(self, summary: str) -> None:
        if self.export_format == AppController.EXPORT_QUANJU:
            self.export_hint.setText(summary)
            self.export_hint.setVisible(True)

    def preview_export(self, on_done=None) -> None:
        """后台生成当前格式的导出内容，仅刷新摘要（截图/自检用）。"""
        self._set_busy(True)

        def ready(text, summary: str) -> None:
            self._set_busy(False)
            self._show_summary(summary)
            if on_done:
                on_done(text, summary)
        self.c.build_export_async(self.export_format, ready)

    def _copy_json(self) -> None:
        fmt = self.export_format
        self._set_busy(True)

        def ready(text, summary: str) -> None:
            self._set_busy(False)
            self._show_summary(summary)
            if text is None:
                self.c.toast.emit(summary)
                return
            label = "全剧视界导入 JSON" if fmt == AppController.EXPORT_QUANJU else "影视仓多仓 JSON"
            self._copy_text(text, f"已复制{label}到剪贴板（{summary}）")
        self.c.build_export_async(fmt, ready)

    def _export(self) -> None:
        if not self.c.has_exportable():
            self.c.toast.emit("暂无可导出的可用单仓配置")
            return
        fmt = self.export_format
        if fmt == AppController.EXPORT_QUANJU:
            from ..export import quanju_default_filename
            default, title = quanju_default_filename(), "导出全剧视界自定义源"
        else:
            default, title = "ysc_config.json", "导出影视仓配置"
        path, _ = QFileDialog.getSaveFileName(self, title, default, "JSON 文件 (*.json)")
        if path:
            self.c.write_export_to_file(path, fmt)
