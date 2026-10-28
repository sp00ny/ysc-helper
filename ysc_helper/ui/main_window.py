"""主窗口（对应 Android 版 MainActivity + ui/screens/MainScreen.kt）。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QTabWidget,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from . import theme
from .controller import AppController
from .history_tab import HistoryTab
from .result_tab import ResultTab
from .task_tab import TaskTab


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController, dark: bool = False, demo: bool = False) -> None:
        super().__init__()
        self.c = controller
        self.dark = dark
        self.setWindowTitle(f"{APP_NAME} v{__version__}" + ("（演示模式）" if demo else ""))
        self.resize(1280, 820)

        root = QWidget()
        root.setObjectName("Root")
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QFrame()
        header.setObjectName("Header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 10, 16, 10)
        tcol = QVBoxLayout()
        tcol.setSpacing(0)
        t = QLabel(APP_NAME)
        t.setObjectName("Title")
        s = QLabel("桌面正式版 · 多线程池并发探测 / 隐写解密 / SSRF防护")
        s.setObjectName("Subtitle")
        tcol.addWidget(t)
        tcol.addWidget(s)
        hl.addLayout(tcol)
        hl.addStretch(1)
        if demo:
            db = QLabel("演示模式 · 离线 Mock 数据")
            db.setObjectName("DemoBadge")
            hl.addWidget(db, 0, Qt.AlignmentFlag.AlignVCenter)
        self.theme_btn = QPushButton()
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_btn.clicked.connect(self.toggle_theme)
        hl.addWidget(self.theme_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        v.addWidget(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.task_tab = TaskTab(controller)
        self.result_tab = ResultTab(controller)
        self.history_tab = HistoryTab(controller)
        self.tabs.addTab(self.task_tab, "📡 任务")
        self.tabs.addTab(self.result_tab, "📊 结果")
        self.tabs.addTab(self.history_tab, "🕘 历史")
        v.addWidget(self.tabs, 1)
        self.setCentralWidget(root)

        controller.toast.connect(lambda m: self.statusBar().showMessage(m, max(4000, 90 * len(m))))
        controller.switch_tab.connect(self.tabs.setCurrentIndex)
        self.statusBar().showMessage("就绪")
        self.apply_theme(dark)

    def apply_theme(self, dark: bool) -> None:
        self.dark = dark
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.stylesheet(theme.DARK if dark else theme.LIGHT))
        self.theme_btn.setText("☀ 浅色模式" if dark else "🌙 深色模式")
        self.result_tab._rebuild_tree()

    def toggle_theme(self) -> None:
        self.apply_theme(not self.dark)
        self.c.repository.update_ui_prefs(dark=self.dark)

    def closeEvent(self, e: QCloseEvent) -> None:  # noqa: N802
        self.c.shutdown()
        super().closeEvent(e)
