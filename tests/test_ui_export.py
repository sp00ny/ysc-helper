"""GUI 导出：结果页的格式选择（影视仓/TVBox ↔ 全剧视界）驱动「复制JSON」与「导出配置」。"""
import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _wait(qapp, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    assert cond()


@pytest.fixture()
def window(qapp, tmp_path):
    from ysc_helper.data.repository import RepoScanRepository
    from ysc_helper.demo import DEMO_AGGREGATOR_PAGES, DEMO_SEEDS, MockHttpClient
    from ysc_helper.ui.controller import AppController
    from ysc_helper.ui.main_window import MainWindow
    repo = RepoScanRepository(tmp_path / "data")
    c = AppController(repo, MockHttpClient(speed=0.02), builtin_seeds=DEMO_SEEDS,
                      aggregator_pages=DEMO_AGGREGATOR_PAGES)
    win = MainWindow(c, dark=False, demo=True)
    c.initialize()
    c.start_scan(only_custom=False)
    _wait(qapp, lambda: not c.is_running and c.has_exportable(), 20)
    yield win, c
    c.shutdown()
    win.close()
    repo.close()


def test_format_choice_drives_copy_and_save(qapp, window, tmp_path):
    from ysc_helper.ui.controller import AppController
    win, c = window
    tab = win.result_tab
    assert [tab.format_combo.itemText(i) for i in range(tab.format_combo.count())] == \
        ["影视仓 / TVBox 多仓", "全剧视界 自定义源"]

    clip = QtWidgets.QApplication.clipboard()
    tab.format_combo.setCurrentIndex(0)
    tab._copy_json()
    _wait(qapp, lambda: '"urls"' in clip.text())
    assert "urls" in json.loads(clip.text())

    tab.format_combo.setCurrentIndex(1)
    assert tab.export_format == AppController.EXPORT_QUANJU
    tab._copy_json()
    _wait(qapp, lambda: "duanju-custom-sources" in clip.text())
    copied = json.loads(clip.text())
    assert copied["format"] == "duanju-custom-sources" and copied["count"] == len(copied["sites"]) > 0
    assert "全剧视界：导出" in tab.export_hint.text()

    out_q, out_t = tmp_path / "q.json", tmp_path / "t.json"
    c.write_export_to_file(str(out_q), AppController.EXPORT_QUANJU)
    c.write_export_to_file(str(out_t), AppController.EXPORT_TVBOX)
    _wait(qapp, lambda: out_q.exists() and out_t.exists())
    _wait(qapp, lambda: out_q.stat().st_size > 0 and out_t.stat().st_size > 0)
    assert json.loads(out_q.read_text(encoding="utf-8")) == copied
    assert "urls" in json.loads(out_t.read_text(encoding="utf-8"))
