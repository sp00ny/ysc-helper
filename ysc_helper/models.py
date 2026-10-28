"""强类型领域模型（对应 Android 版 model/Models.kt）。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class RepoKind(Enum):
    SINGLE = "单线路"
    MULTI_STOREHOUSE = "多仓(storeHouse)"
    MULTI_URLS = "多仓(urls)"
    UNKNOWN = "未知"

    @property
    def label(self) -> str:
        return self.value

    @property
    def is_multi(self) -> bool:
        return self in (RepoKind.MULTI_STOREHOUSE, RepoKind.MULTI_URLS)


class SpeedGrade(Enum):
    FAST = "快"
    MEDIUM = "中"
    SLOW = "慢"
    UNAVAILABLE = "不可用"

    @property
    def label(self) -> str:
        return self.value

    @staticmethod
    def from_latency(available: bool, total_ms: Optional[int]) -> "SpeedGrade":
        """只要节点可用，即使高延迟也只评为「慢」，绝不出现「可用 + 不可用」双重冲突徽章。"""
        if not available or total_ms is None:
            return SpeedGrade.UNAVAILABLE
        if total_ms < 500:
            return SpeedGrade.FAST
        if total_ms < 2000:
            return SpeedGrade.MEDIUM
        return SpeedGrade.SLOW


class SecurityFlag(Enum):
    HTTPS_SAFE = "HTTPS加密"
    CLEARTEXT_HTTP = "HTTP明文风险"
    CLEARTEXT_SPIDER_JAR = "明文JAR插件风险"

    @property
    def label(self) -> str:
        return self.value


class PayloadFormat(Enum):
    PLAIN_JSON = "标准JSON"
    JSON_RELAXED = "带注释JSON"
    BASE64_JSON = "Base64编码"
    IMAGE_STEGO = "图片隐写(**)"
    AES_HEX = "AES加密($#)"
    UNRECOGNIZED = "未知格式"

    @property
    def label(self) -> str:
        return self.value


@dataclass
class SeedLine:
    name: str
    url: str
    is_custom: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "url": self.url, "isCustom": self.is_custom}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SeedLine":
        return SeedLine(name=str(d.get("name", "")), url=str(d.get("url", "")),
                        is_custom=bool(d.get("isCustom", False)))


@dataclass
class RepoNode:
    id: str
    name: str
    url: str
    normalized_url: str
    kind: RepoKind = RepoKind.UNKNOWN
    available: bool = False
    speed_grade: SpeedGrade = SpeedGrade.UNAVAILABLE
    ttfb_ms: Optional[int] = None
    total_ms: Optional[int] = None
    size_kb: Optional[float] = None
    speed_kbps: Optional[float] = None
    site_count: int = 0
    payload_format: PayloadFormat = PayloadFormat.UNRECOGNIZED
    security_flags: List[SecurityFlag] = field(default_factory=list)
    is_shared_ref: bool = False
    depth: int = 0
    error: str = ""
    children: List["RepoNode"] = field(default_factory=list)
    # 全剧视界导出：单仓内可导入的站点候选（None = 旧快照/未解析，无明细）
    quanju_sites: Optional[List[Dict[str, Any]]] = None
    quanju_skipped: Dict[str, int] = field(default_factory=dict)

    @property
    def available_child_count(self) -> int:
        return sum(1 for c in self.children if c.available)

    def copy_with(self, **kw: Any) -> "RepoNode":
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d.update(kw)
        return RepoNode(**d)

    # ---- 序列化（快照持久化用）----
    def to_dict(self) -> Dict[str, Any]:
        d = self._base_dict()
        if self.quanju_sites is not None:
            d["quanjuSites"] = self.quanju_sites
            d["quanjuSkipped"] = dict(self.quanju_skipped)
        return d

    def _base_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "url": self.url,
            "normalizedUrl": self.normalized_url, "kind": self.kind.name,
            "available": self.available, "speedGrade": self.speed_grade.name,
            "ttfbMs": self.ttfb_ms, "totalMs": self.total_ms, "sizeKb": self.size_kb,
            "speedKbps": self.speed_kbps, "siteCount": self.site_count,
            "payloadFormat": self.payload_format.name,
            "securityFlags": [f.name for f in self.security_flags],
            "isSharedRef": self.is_shared_ref, "depth": self.depth, "error": self.error,
            "children": [c.to_dict() for c in self.children],
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "RepoNode":
        def enum_or(cls, key, default):
            try:
                return cls[d.get(key)]
            except Exception:
                return default
        return RepoNode(
            id=str(d.get("id", "")), name=str(d.get("name", "")), url=str(d.get("url", "")),
            normalized_url=str(d.get("normalizedUrl", "")),
            kind=enum_or(RepoKind, "kind", RepoKind.UNKNOWN),
            available=bool(d.get("available", False)),
            speed_grade=enum_or(SpeedGrade, "speedGrade", SpeedGrade.UNAVAILABLE),
            ttfb_ms=d.get("ttfbMs"), total_ms=d.get("totalMs"), size_kb=d.get("sizeKb"),
            speed_kbps=d.get("speedKbps"), site_count=int(d.get("siteCount") or 0),
            payload_format=enum_or(PayloadFormat, "payloadFormat", PayloadFormat.UNRECOGNIZED),
            security_flags=[SecurityFlag[f] for f in d.get("securityFlags", []) if f in SecurityFlag.__members__],
            is_shared_ref=bool(d.get("isSharedRef", False)), depth=int(d.get("depth") or 0),
            error=str(d.get("error", "")),
            children=[RepoNode.from_dict(c) for c in d.get("children", [])],
            quanju_sites=list(d["quanjuSites"]) if isinstance(d.get("quanjuSites"), list) else None,
            quanju_skipped={str(k): int(v) for k, v in (d.get("quanjuSkipped") or {}).items()
                            if isinstance(v, (int, float))},
        )


@dataclass
class ExportedUrlEntry:
    name: str
    url: str


@dataclass
class ExportedUrlsConfig:
    urls: List[ExportedUrlEntry] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"urls": [asdict(u) for u in self.urls]}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ExportedUrlsConfig":
        return ExportedUrlsConfig(urls=[ExportedUrlEntry(str(u.get("name", "")), str(u.get("url", "")))
                                        for u in d.get("urls", [])])


@dataclass
class ScanStatistics:
    top_level_total: int = 0
    total_nodes_scanned: int = 0
    available_total: int = 0
    available_singles: int = 0
    available_multis: int = 0
    exportable_unique_count: int = 0
    avg_latency_ms: Optional[int] = None
    cleartext_http_count: int = 0

    @staticmethod
    def compute(roots: List[RepoNode], exported_count: int) -> "ScanStatistics":
        """单次深度优先遍历完成全部指标聚合。"""
        s = ScanStatistics(top_level_total=len(roots), exportable_unique_count=exported_count)
        lat_sum = 0
        lat_cnt = 0
        stack = list(roots)
        while stack:
            n = stack.pop()
            s.total_nodes_scanned += 1
            if n.available:
                s.available_total += 1
                if n.kind == RepoKind.SINGLE:
                    s.available_singles += 1
                if n.kind.is_multi:
                    s.available_multis += 1
                if n.total_ms is not None:
                    lat_sum += n.total_ms
                    lat_cnt += 1
                if SecurityFlag.CLEARTEXT_HTTP in n.security_flags:
                    s.cleartext_http_count += 1
            stack.extend(n.children)
        s.avg_latency_ms = (lat_sum // lat_cnt) if lat_cnt else None
        return s

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ScanStatistics":
        keys = ScanStatistics.__dataclass_fields__.keys()
        return ScanStatistics(**{k: d[k] for k in keys if k in d})


class ScanStage(Enum):
    IDLE = "就绪"
    SCRAPING = "多线程搜集聚合页线路"
    PROBING = "多线程深度解析与并发测速"
    EXPORTING = "去重清洗与生成配置"
    COMPLETED = "验证完成"
    CANCELLED = "已取消"
    FAILED = "执行失败"

    @property
    def title(self) -> str:
        return self.value


@dataclass
class ScanProgress:
    stage: ScanStage = ScanStage.IDLE
    completed: int = 0
    total: int = 0
    current_url: str = ""
    elapsed_ms: int = 0

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return max(0.0, min(1.0, self.completed / self.total))

    @property
    def percent(self) -> int:
        return int(self.fraction * 100)


@dataclass
class ThreadPoolTelemetry:
    configured_concurrency: int = 16
    active_scrape_threads: int = 0
    active_probe_io_threads: int = 0
    probe_pool_size: int = 0
    active_cpu_parse_threads: int = 0
    cpu_pool_size: int = 0
    in_flight_http_calls: int = 0
    queued_tasks: int = 0
    completed_tasks: int = 0
    single_flight_cache_hits: int = 0


@dataclass
class ProbeSettings:
    max_concurrency: int = 16
    speed_samples: int = 1
    connect_timeout_ms: int = 4500
    read_timeout_ms: int = 5500
    max_depth: int = 3
    max_children_per_multi: int = 100
    https_only: bool = False
    allow_fake_ip: bool = False  # 桌面版新增：兼容 Clash/代理 Fake-IP(198.18.0.0/15) 模式

    def clamped(self) -> "ProbeSettings":
        self.max_concurrency = max(4, min(32, int(self.max_concurrency)))
        self.speed_samples = max(1, min(3, int(self.speed_samples)))
        return self


@dataclass
class FilterState:
    keyword: str = ""
    kind: Optional[RepoKind] = None
    available_only: Optional[bool] = None
    speed_grade: Optional[SpeedGrade] = None
