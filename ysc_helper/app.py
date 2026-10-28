"""应用装配与启动。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QApplication

from . import APP_NAME
from .data.repository import RepoScanRepository


def _system_prefers_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:
        return False


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="ysc-helper", description=f"{APP_NAME}（PySide6 桌面版）")
    p.add_argument("--demo", action="store_true", help="离线演示模式：使用内置 Mock 数据运行真实探测引擎")
    p.add_argument("--dark", action="store_true", help="强制深色主题")
    p.add_argument("--light", action="store_true", help="强制浅色主题")
    p.add_argument("--data-dir", type=Path, default=None, help="数据目录（历史数据库 / 快照 / 设置）")
    p.add_argument("--screenshot", type=Path, default=None,
                   help="（调试）自动运行演示扫描后截取主窗口保存到该路径并退出")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("YscHelper")
    app.setStyle("Fusion")
    font = QFont()
    font.setFamilies(["Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", "Noto Sans SC",
                      "WenQuanYi Micro Hei", "Sans Serif"])
    font.setPointSize(10)
    app.setFont(font)

    from .ui.controller import AppController
    from .ui.main_window import MainWindow

    if args.screenshot:
        args.demo = True
    repo = RepoScanRepository(args.data_dir)
    if args.demo:
        from .demo import DEMO_AGGREGATOR_PAGES, DEMO_SEEDS, MockHttpClient
        http = MockHttpClient(speed=0.25 if args.screenshot else 1.0)
        controller = AppController(repo, http, builtin_seeds=DEMO_SEEDS, aggregator_pages=DEMO_AGGREGATOR_PAGES)
    else:
        from .network import ProbeHttpClient
        controller = AppController(repo, ProbeHttpClient())

    prefs = repo.load_ui_prefs()
    dark = True if args.dark else False if args.light else bool(prefs.get("dark", _system_prefers_dark()))
    win = MainWindow(controller, dark=dark, demo=args.demo)
    win.show()
    controller.initialize()

    if args.screenshot:
        _schedule_screenshot(app, win, controller, args.screenshot)
    rc = app.exec()
    repo.close()
    return rc


def _schedule_screenshot(app: QApplication, win, controller, path: Path) -> None:
    shots = {"task": None}
    win.setMinimumSize(1360, 860)  # 截图模式下固定较大尺寸，便于展示完整树

    def grab_task_then_wait() -> None:
        # 扫描进行中先截一张「任务」页
        shots["task"] = win.grab()

    def on_done() -> None:
        def snap() -> None:
            win.result_tab.expand_or_collapse_all(True)
            app.processEvents()
            win.grab().save(str(path))
            if shots["task"] is not None:
                shots["task"].save(str(path.with_name(path.stem + "_task" + path.suffix)))
            print(f"screenshot saved: {path}")
            app.quit()
        QTimer.singleShot(400, snap)

    controller.results_changed.connect(lambda: controller.is_running is False and controller.roots and on_done())
    QTimer.singleShot(300, lambda: controller.start_scan(only_custom=False))
    QTimer.singleShot(900, grab_task_then_wait)
    QTimer.singleShot(30000, lambda: (print("screenshot timeout", file=sys.stderr), app.exit(2)))
