"""「历史」页（对应 Android 版 ui/screens/HistoryScreen.kt）。"""
from __future__ import annotations

import datetime as _dt
from typing import List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPushButton, QStackedWidget,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..data.repository import ScanHistoryEntity
from .controller import AppController


class HistoryTab(QWidget):
    def __init__(self, c: AppController) -> None:
        super().__init__()
        self.c = c
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        self.stack = QStackedWidget()
        self.empty = QLabel("暂无历史归档记录\n完成一次多线程验证后，系统将自动使用 SQLite + GZIP 压缩归档（最多保留 20 份快照）")
        self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["时间", "摘要", "操作"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.cellDoubleClicked.connect(self._on_double)
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.table)
        lay.addWidget(self.stack)
        self._items: List[ScanHistoryEntity] = []
        c.history_changed.connect(self._on_history)

    def _on_double(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._items):
            self.c.restore_history(self._items[row].id)

    def _on_history(self, items: List[ScanHistoryEntity]) -> None:
        self._items = items
        if not items:
            self.stack.setCurrentWidget(self.empty)
            return
        self.stack.setCurrentWidget(self.table)
        self.table.setRowCount(len(items))
        for row, e in enumerate(items):
            ts = _dt.datetime.fromtimestamp(e.timestamp / 1000).strftime("%Y-%m-%d %H:%M:%S")
            t = QTableWidgetItem(ts)
            f = t.font()
            f.setBold(True)
            t.setFont(f)
            self.table.setItem(row, 0, t)
            self.table.setItem(row, 1, QTableWidgetItem(
                f"扫描 {e.total_nodes_scanned} 节点 · 导出单仓 {e.exportable_unique_count} · "
                f"有效多仓 {e.available_multis} · 耗时 {e.duration_ms // 1000}s"))
            w = QWidget()
            hl = QHBoxLayout(w)
            hl.setContentsMargins(4, 0, 4, 0)
            r = QPushButton("恢复")
            r.clicked.connect(lambda _=False, i=e.id: self.c.restore_history(i))
            d = QPushButton("删除")
            d.setObjectName("LinkDanger")
            d.clicked.connect(lambda _=False, i=e.id: self.c.delete_history(i))
            hl.addWidget(r)
            hl.addWidget(d)
            self.table.setCellWidget(row, 2, w)
