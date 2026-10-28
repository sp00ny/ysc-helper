"""本地持久化仓库（对应 Android 版 Room + DataStore + GZIP 快照 RepoScanRepository.kt）。

* SQLite ``scan_history`` 表仅存轻量索引元数据；
* 完整结果树以 GZIP 压缩 JSON 写入 ``snapshots/`` 目录（最多保留 20 份）；
* 设置与自定义种子源保存为 ``settings.json``（替代 DataStore）。
"""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from ..engine.probe_engine import PipelineOutput
from ..models import ExportedUrlsConfig, ProbeSettings, RepoNode, ScanStatistics, SeedLine

MAX_HISTORY_KEEP = 20


def default_data_dir() -> Path:
    env = os.environ.get("YSC_HELPER_DATA_DIR")
    if env:
        return Path(env)
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
        return base / "YscHelper"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ysc-helper"


@dataclass
class ScanHistoryEntity:
    id: int
    timestamp: int
    duration_ms: int
    top_level_total: int
    total_nodes_scanned: int
    available_total: int
    available_singles: int
    available_multis: int
    exportable_unique_count: int
    avg_latency_ms: Optional[int]
    snapshot_file_name: str


@dataclass
class PersistedSnapshot:
    roots: List[RepoNode]
    exported_config: ExportedUrlsConfig
    statistics: ScanStatistics


_COLS = ("id, timestamp, duration_ms, top_level_total, total_nodes_scanned, available_total, "
         "available_singles, available_multis, exportable_unique_count, avg_latency_ms, snapshot_file_name")


class RepoScanRepository:
    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.snapshots_dir = self.data_dir / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.data_dir / "settings.json"
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.data_dir / "ysc_helper.db"), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS scan_history ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER NOT NULL, duration_ms INTEGER NOT NULL, "
            "top_level_total INTEGER, total_nodes_scanned INTEGER, available_total INTEGER, "
            "available_singles INTEGER, available_multis INTEGER, exportable_unique_count INTEGER, "
            "avg_latency_ms INTEGER, snapshot_file_name TEXT NOT NULL)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_scan_history_ts ON scan_history(timestamp)")
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------------- 设置 & 自定义种子 ----------------
    def _read_prefs(self) -> dict:
        try:
            return json.loads(self.settings_path.read_text("utf-8"))
        except Exception:
            return {}

    def _write_prefs(self, prefs: dict) -> None:
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, self.settings_path)

    def load_settings(self) -> ProbeSettings:
        p = self._read_prefs()
        return ProbeSettings(max_concurrency=int(p.get("max_concurrency", 16)),
                             speed_samples=int(p.get("speed_samples", 1)),
                             https_only=bool(p.get("https_only", False)),
                             allow_fake_ip=bool(p.get("allow_fake_ip", False))).clamped()

    def update_settings(self, s: ProbeSettings) -> None:
        with self._lock:
            p = self._read_prefs()
            s.clamped()
            p.update(max_concurrency=s.max_concurrency, speed_samples=s.speed_samples, https_only=s.https_only,
                     allow_fake_ip=s.allow_fake_ip)
            self._write_prefs(p)

    def load_ui_prefs(self) -> dict:
        return self._read_prefs().get("ui", {})

    def update_ui_prefs(self, **kw) -> None:
        with self._lock:
            p = self._read_prefs()
            ui = p.get("ui", {})
            ui.update(kw)
            p["ui"] = ui
            self._write_prefs(p)

    def load_custom_seeds(self) -> List[SeedLine]:
        return [SeedLine.from_dict(d) for d in self._read_prefs().get("custom_seeds", [])]

    def add_custom_seed(self, seed: SeedLine) -> None:
        with self._lock:
            p = self._read_prefs()
            cur = [d for d in p.get("custom_seeds", [])
                   if str(d.get("url", "")).strip().lower() != seed.url.strip().lower()]
            seed.is_custom = True
            cur.insert(0, seed.to_dict())
            p["custom_seeds"] = cur
            self._write_prefs(p)

    def remove_custom_seed(self, url: str) -> None:
        with self._lock:
            p = self._read_prefs()
            p["custom_seeds"] = [d for d in p.get("custom_seeds", []) if d.get("url") != url]
            self._write_prefs(p)

    # ---------------- 扫描历史 ----------------
    def save_scan_output(self, out: PipelineOutput) -> int:
        now = int(time.time() * 1000)
        fname = f"scan_{now}_{uuid.uuid4().hex[:8]}.json.gz"
        payload = {"roots": [r.to_dict() for r in out.roots],
                   "exportedConfig": out.exported_config.to_dict(),
                   "statistics": out.statistics.to_dict()}
        with gzip.open(self.snapshots_dir / fname, "wb") as gz:
            gz.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        st = out.statistics
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO scan_history (timestamp, duration_ms, top_level_total, total_nodes_scanned, "
                "available_total, available_singles, available_multis, exportable_unique_count, avg_latency_ms, "
                "snapshot_file_name) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now, out.duration_ms, st.top_level_total, st.total_nodes_scanned, st.available_total,
                 st.available_singles, st.available_multis, st.exportable_unique_count, st.avg_latency_ms, fname))
            new_id = cur.lastrowid
            overflow = self._db.execute(
                f"SELECT {_COLS} FROM scan_history ORDER BY timestamp DESC, id DESC LIMIT -1 OFFSET ?",
                (MAX_HISTORY_KEEP,)).fetchall()
            for row in overflow:
                (self.snapshots_dir / row[10]).unlink(missing_ok=True)
                self._db.execute("DELETE FROM scan_history WHERE id = ?", (row[0],))
            self._db.commit()
        return int(new_id)

    def list_history(self) -> List[ScanHistoryEntity]:
        with self._lock:
            rows = self._db.execute(f"SELECT {_COLS} FROM scan_history ORDER BY timestamp DESC, id DESC").fetchall()
        return [ScanHistoryEntity(*r) for r in rows]

    def _get(self, sql: str, args: tuple = ()) -> Optional[ScanHistoryEntity]:
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return ScanHistoryEntity(*row) if row else None

    def load_latest_snapshot(self) -> Optional[Tuple[ScanHistoryEntity, PersistedSnapshot]]:
        e = self._get(f"SELECT {_COLS} FROM scan_history ORDER BY timestamp DESC, id DESC LIMIT 1")
        if e is None:
            return None
        snap = self._read_snapshot(e.snapshot_file_name)
        return (e, snap) if snap else None

    def load_snapshot_by_id(self, hid: int) -> Optional[Tuple[ScanHistoryEntity, PersistedSnapshot]]:
        e = self._get(f"SELECT {_COLS} FROM scan_history WHERE id = ?", (hid,))
        if e is None:
            return None
        snap = self._read_snapshot(e.snapshot_file_name)
        return (e, snap) if snap else None

    def delete_history_by_id(self, hid: int) -> None:
        e = self._get(f"SELECT {_COLS} FROM scan_history WHERE id = ?", (hid,))
        if e is None:
            return
        (self.snapshots_dir / e.snapshot_file_name).unlink(missing_ok=True)
        with self._lock:
            self._db.execute("DELETE FROM scan_history WHERE id = ?", (hid,))
            self._db.commit()

    def _read_snapshot(self, fname: str) -> Optional[PersistedSnapshot]:
        path = self.snapshots_dir / fname
        if not path.exists():
            return None
        try:
            with gzip.open(path, "rb") as gz:
                d = json.loads(gz.read().decode("utf-8"))
            return PersistedSnapshot(roots=[RepoNode.from_dict(r) for r in d.get("roots", [])],
                                     exported_config=ExportedUrlsConfig.from_dict(d.get("exportedConfig", {})),
                                     statistics=ScanStatistics.from_dict(d.get("statistics", {})))
        except Exception:
            return None

    @staticmethod
    def format_export_config_json(cfg: ExportedUrlsConfig) -> str:
        return json.dumps(cfg.to_dict(), ensure_ascii=False, indent=4)
