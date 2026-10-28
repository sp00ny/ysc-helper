"""全剧视界（短剧视界 --all-sources 版）「自定义源」导入格式导出器。

格式依据（非猜测，均来自全剧视界 Go 核心源码 native/core/provider_custom_maccms.go）::

    {"format": "duanju-custom-sources", "version": 1, "count": N,
     "sites": [{"name": "站名", "url": "站点网址", "rule": {XBPQ 规则对象, 可选}, "type": "web|json, 可选"}]}

导入端（customSourceDrafts / customSourceDraftFromEntry / customMaccmsRegistry.add）的约束，
这里逐条在 Python 侧复刻，保证导出的每一条都能被真实导入逻辑接受：

* 只支持 MacCMS 采集接口站（TVBox type 0/1）与 XBPQ 规则站；爬虫 jar（type 3 / spider 字段）
  会被跳过——「爬虫(jar)规则无法在核心内执行」；
* MacCMS 端点（路径含 /api.php 或 /provide/vod）保留完整地址（去尾 /），其他地址收敛为 scheme://host；
  「加速前缀/https://真实网址」拼接形态整体保留；
* 规则源的站点网址取规则的「主页url / 首页url / 请求」，缺省时取 分类url / 搜索url 的站点根；
* 网址必须 http/https、域名含「.」、不得带用户名、参数（?query）或锚点；
* 名称去掉表情/符号噪音与 [版本]/(版本) 尾缀，1~40 个字符；
* 全局按小写网址去重（注册表对同网址第二条直接报「该网址已存在」）；
* 单次导入上限 200 条；整个自定义源注册表落盘上限 256KB（超出时核心静默不写盘）。
"""
from __future__ import annotations

import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

QUANJU_FORMAT = "duanju-custom-sources"
QUANJU_VERSION = 1
IMPORT_LIMIT = 200                      # customSourceImportLimit
RULE_MAX_BYTES = 256 * 1024             # customSourceMaxBytes（单条规则 & 整个注册表）
DEFAULT_REGISTRY_BUDGET = 200 * 1024    # 给用户已有自定义源预留余量
NAME_MAX_RUNES = 40

# customSourceRuleKeys
RULE_KEYS = ("主页url", "首页url", "分类url", "分类Header", "数组", "播放数组", "搜索url", "线路数组", "请求")

# 跳过原因（键 → 中文说明），解析期与导出期共用
SKIP_REASONS: Dict[str, str] = {
    "jar": "爬虫 jar/csp 站点（全剧视界核心无法执行）",
    "script": "drpy/JS/Python 脚本爬虫站点",
    "remote": "type 4 远程接口站点",
    "no_url": "缺少可用 http(s) 地址",
    "ext_unresolved": "XBPQ 外链规则文件抓取失败或不是有效规则",
    "invalid_url": "网址不符合全剧视界要求（参数/锚点/用户名/无效域名）",
    "duplicate": "与更快线路中的站点网址重复",
    "over_limit": f"超出全剧视界单次导入上限 {IMPORT_LIMIT} 条",
    "over_budget": "超出全剧视界自定义源存储上限（256KB）",
    "rule_too_large": "单条规则超过 256KB",
}


# --------------------------------------------------------------------------- XBPQ 规则识别（provider_xbpq.go）

def _norm_key(key: str) -> str:
    key = key.strip().replace("〔", "[").replace("〕", "]")
    return re.sub(r"[ \t\n\r]", "", key)


def _string_value(value: Any) -> str:
    """xbpqStringValue：字符串原样，数字格式化，其他 JSON 序列化。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return format(value, "g") if isinstance(value, float) and not value.is_integer() else str(int(value))
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def xbpq_fields(rule: Any) -> Optional[Dict[str, str]]:
    """parseXBPQRule：对象或 [对象] 数组 → 规整字段表；不是 XBPQ 规则返回 None。"""
    if isinstance(rule, str):
        text = rule.strip()
        if not text or len(text.encode("utf-8")) > RULE_MAX_BYTES:
            return None
        try:
            rule = json.loads(text, strict=False)
        except ValueError:
            try:
                rule = json.loads(re.sub(r",\s*([}\]])", r"\1", text), strict=False)
            except ValueError:
                return None
    nodes: List[Dict[str, Any]]
    if isinstance(rule, dict):
        nodes = [rule]
    elif isinstance(rule, list):
        nodes = [n for n in rule if isinstance(n, dict)]
    else:
        return None
    fields: Dict[str, str] = {}
    for node in nodes:
        for k, v in node.items():
            nk = _norm_key(str(k))
            if nk:
                fields[nk] = _string_value(v).replace("〔", "[").replace("〕", "]").strip()
    if not any(fields.get(k) for k in ("主页url", "首页url", "请求", "分类url", "分类Url")):
        return None
    return fields


def has_rule_keys(entry: Dict[str, Any]) -> bool:
    """customSourceHasRuleKeys（与 Go 一致：规整键与原始键各计一次，命中 ≥2）。"""
    hit = 0
    for key in RULE_KEYS:
        if _norm_key(key) in entry:
            hit += 1
        if key in entry:
            hit += 1
    return hit >= 2


def _host_origin(address: str) -> str:
    idx = address.find("://")
    if idx < 0:
        return ""
    rest = address[idx + 3:]
    m = re.search(r"[/?#]", rest)
    if m:
        rest = rest[:m.start()]
    return address[:idx + 3] + rest


def xbpq_home_url(fields: Dict[str, str]) -> str:
    """xbpqRule.homeURL。"""
    value = next((fields[k] for k in ("主页url", "首页url", "请求") if fields.get(k)), "")
    if not value:
        for alt in ("分类url", "分类Url", "搜索url"):
            cat = fields.get(alt, "")
            if cat.startswith("http"):
                value = _host_origin(cat)
                break
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    try:
        p = urlsplit(value)
    except ValueError:
        return ""
    if not p.netloc:
        return ""
    return urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), "", ""))


def rule_from_entry(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """customSourceRuleFromMap：条目自身或其 ext（对象 / [对象] / JSON 字符串）是否为 XBPQ 规则。"""
    if has_rule_keys(entry) and xbpq_fields(entry) is not None:
        return dict(entry)
    ext = entry.get("ext")
    if isinstance(ext, dict):
        if has_rule_keys(ext) and xbpq_fields(ext) is not None:
            return ext
    elif isinstance(ext, list):
        for node in ext:
            if isinstance(node, dict) and has_rule_keys(node) and xbpq_fields(node) is not None:
                return node
    elif isinstance(ext, str) and ext.strip().startswith(("{", "[")):
        return _rule_from_text(ext)
    return None


def _rule_from_text(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip().lstrip("\ufeff")
    try:
        obj = json.loads(text, strict=False)
    except ValueError:
        from ..parser.tvbox_payload_parser import strip_comments_and_trailing_commas
        try:
            obj = json.loads(strip_comments_and_trailing_commas(text), strict=False)
        except ValueError:
            return None
    if isinstance(obj, dict) and xbpq_fields(obj) is not None:
        return obj
    if isinstance(obj, list):
        for node in obj:
            if isinstance(node, dict) and xbpq_fields(node) is not None:
                return node
    return None


# --------------------------------------------------------------------------- 网址 / 名称规整

def _is_spliced(base: str) -> bool:
    if base.count("://") < 2:
        return False
    try:
        p = urlsplit(base)
    except ValueError:
        return False
    rest = p.path.lstrip("/")
    return bool(p.scheme) and rest.startswith(("http://", "https://"))


def is_maccms_endpoint(base: str) -> bool:
    path = base.split("?", 1)[0]
    return "/api.php" in path or "/provide/vod" in path


def import_base(api: str) -> str:
    """customSourceImportBase（无规则分支），并去掉参数/锚点使其能通过注册表校验。"""
    base = api.strip()
    if not base:
        return ""
    if "://" not in base:
        base = ("https:" + base) if base.startswith("//") else ("https://" + base)
    if _is_spliced(base):
        return base.rstrip("/")
    try:
        p = urlsplit(base)
    except ValueError:
        return ""
    if not p.netloc:
        return ""
    if is_maccms_endpoint(base):
        # Go 保留完整端点；注册表又拒绝 ?query/#fragment —— 采集参数（ac=list 等）由全剧视界自行拼接
        return urlunsplit((p.scheme, p.netloc, p.path, "", "")).rstrip("/")
    return f"{p.scheme}://{p.netloc}"


def registry_accepts(base: str) -> bool:
    """customMaccmsRegistry.add 的网址校验。"""
    base = base.rstrip("/")
    try:
        p = urlsplit(base)
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    if p.username is not None or p.password is not None or p.query or p.fragment or "#" in base or "?" in base:
        return False
    return "." in p.netloc


def clean_name(name: str) -> str:
    """customXBPQCleanName + 40 字符截断（customSourceImportName）。"""
    name = name.strip().replace("\t", " ")
    name = "".join(ch for ch in name
                   if unicodedata.category(ch)[0] in ("L", "N") or ch in "-_·. ")
    name = " ".join(name.split())
    while True:
        trimmed = name.strip()
        idx = max(trimmed.rfind("("), trimmed.rfind("["))
        close = min([i for i in (trimmed.find(")"), trimmed.find("]")) if i >= 0], default=-1)
        if idx > 0 and close == len(trimmed) - 1:
            trimmed = trimmed[:idx].strip()
        if trimmed == name:
            break
        name = trimmed
    return name[:NAME_MAX_RUNES].strip()


def _fallback_name(base: str) -> str:
    host = urlsplit(base).hostname or ""
    return host[4:] if host.startswith("www.") else (host or "自定义源")


# --------------------------------------------------------------------------- 解析期：从 TVBox 单仓提取候选站点

def _num_type(v: Any) -> int:
    if isinstance(v, bool):
        return -1
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return -1


def _looks_like_ext_ref(ext: str) -> bool:
    e = ext.strip()
    return (e.startswith(("http://", "https://", "./", "../", "/"))
            or e.lower().split("?")[0].endswith((".json", ".txt")))


def extract_quanju_sites(config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """把一份 TVBox 单仓配置的 ``sites`` 压缩成全剧视界可导入的候选，返回 (候选, 跳过计数)。

    候选形态：``{"name", "api"}``（MacCMS）/ ``{"name", "rule"}``（内联 XBPQ）/ ``{"name", "extUrl"}``
    （XBPQ 外链规则，导出时再抓取）。判定顺序与全剧视界 customSourceDraftFromEntry 一致：先规则，后 jar 跳过。
    """
    out: List[Dict[str, Any]] = []
    skipped: Dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    sites = config.get("sites")
    if not isinstance(sites, list):
        return out, skipped
    for site in sites:
        if not isinstance(site, dict):
            continue
        name = next((_string_value(site.get(k)).strip() for k in ("name", "名称", "站点名称", "title", "key", "cn")
                     if _string_value(site.get(k)).strip()), "")
        rule = rule_from_entry(site)
        if rule is not None:
            out.append({"name": name, "rule": rule})
            continue
        api = _string_value(site.get("api")).strip()
        typ = _num_type(site.get("type"))
        ext = site.get("ext")
        low_api = api.lower()
        if "xbpq" in low_api and isinstance(ext, str) and _looks_like_ext_ref(ext):
            out.append({"name": name, "extUrl": ext.strip()})
            continue
        if low_api.split("?")[0].endswith((".js", ".py")) or "drpy" in low_api:
            skip("script")
            continue
        if typ == 3 or _string_value(site.get("spider")).strip() or _string_value(site.get("jar")).strip():
            skip("jar")
            continue
        if typ == 4:
            skip("remote")
            continue
        if typ in (-1, 0, 1, 2) and low_api.startswith(("http://", "https://")):
            out.append({"name": name, "api": api})
            continue
        skip("no_url")
    return out, skipped


# --------------------------------------------------------------------------- 导出期

@dataclass
class QuanjuExportReport:
    exported: int = 0
    api_sites: int = 0
    rule_sites: int = 0
    repos_used: int = 0
    repos_total: int = 0
    repos_without_detail: int = 0        # 旧版快照没有站点明细
    lives_ignored: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)

    def add_skip(self, reason: str, n: int = 1) -> None:
        if n:
            self.skipped[reason] = self.skipped.get(reason, 0) + n

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())

    def summary_text(self) -> str:
        parts = [f"全剧视界：导出 {self.exported} 个站点（MacCMS {self.api_sites} · XBPQ 规则 {self.rule_sites}），"
                 f"来自 {self.repos_used}/{self.repos_total} 条可用单仓"]
        if self.skipped:
            detail = "、".join(f"{SKIP_REASONS.get(k, k)} {v}" for k, v in
                              sorted(self.skipped.items(), key=lambda kv: -kv[1]))
            parts.append(f"跳过 {self.skipped_total}：{detail}")
        if self.repos_without_detail:
            parts.append(f"{self.repos_without_detail} 条线路来自旧快照、缺少站点明细，请重新检测")
        return "；".join(parts)


def _estimate_record_bytes(name: str, base: str, rule: Optional[Dict[str, Any]]) -> int:
    """估算一条记录在全剧视界 custom_sources.json 中的体积（含 id/createdAt 等固定开销）。"""
    size = 140 + len(json.dumps(name, ensure_ascii=False).encode()) + len(base)
    if rule is not None:
        rule_text = json.dumps(rule, ensure_ascii=False, separators=(",", ":"))
        escaped = json.dumps(rule_text, ensure_ascii=False)
        size += len(escaped.encode()) + 5 * sum(rule_text.count(c) for c in "<>&")
    return size


def _collect_repos(roots: List[Any]) -> List[Any]:
    """与 TVBox 导出相同：可用单仓叶子，清洗代理前缀后去重，按真实延迟升序。"""
    from ..models import RepoKind
    from ..security import clean_source_url, normalize_url
    seen: set = set()
    cands: List[Tuple[float, int, Any]] = []

    def walk(n: Any) -> None:
        if n.kind.is_multi:
            for c in n.children:
                walk(c)
        elif n.available and n.kind == RepoKind.SINGLE:
            norm = normalize_url(clean_source_url(n.url))
            if norm and norm not in seen:
                seen.add(norm)
                cands.append((n.total_ms if n.total_ms is not None else float("inf"), len(cands), n))

    for r in roots:
        walk(r)
    cands.sort(key=lambda c: (c[0], c[1]))
    return [c[2] for c in cands]


def build_quanju_export(roots: List[Any], fetch_text: Optional[Callable[[str], Optional[str]]] = None,
                        max_sites: int = IMPORT_LIMIT, registry_budget: int = DEFAULT_REGISTRY_BUDGET,
                        fetch_workers: int = 8, max_ext_fetches: int = 300,
                        ) -> Tuple[Dict[str, Any], QuanjuExportReport]:
    """把扫描结果转换为全剧视界自定义源导入 JSON（dict）与统计报告。

    ``fetch_text(url) -> str|None`` 用于抓取 XBPQ 外链规则文件；为 None 时外链站点计入跳过。
    """
    from ..security import clean_source_url
    report = QuanjuExportReport()
    repos = _collect_repos(roots)
    report.repos_total = len(repos)

    # 1) 解析外链规则（并发、去重、限量）
    ext_jobs: Dict[str, None] = {}
    for n in repos:
        for c in n.quanju_sites or []:
            if "extUrl" in c:
                ext_jobs.setdefault(urljoin(clean_source_url(n.url), c["extUrl"]), None)
    ext_rules: Dict[str, Optional[Dict[str, Any]]] = {}
    if fetch_text is not None and ext_jobs:
        urls = list(ext_jobs)[:max_ext_fetches]

        def job(u: str) -> Tuple[str, Optional[Dict[str, Any]]]:
            try:
                text = fetch_text(u)
            except Exception:
                text = None
            return u, (_rule_from_text(text) if text else None)

        with ThreadPoolExecutor(max_workers=max(1, fetch_workers), thread_name_prefix="quanju-ext") as ex:
            for u, rule in ex.map(job, urls):
                ext_rules[u] = rule

    # 2) 按线路延迟顺序展开站点
    sites: List[Dict[str, Any]] = []
    seen_bases: set = set()
    used_bytes = 0
    for n in repos:
        if n.quanju_sites is None:
            report.repos_without_detail += 1
            continue
        for reason, cnt in (n.quanju_skipped or {}).items():
            report.add_skip(reason, int(cnt))
        contributed = False
        for c in n.quanju_sites:
            rule: Optional[Dict[str, Any]] = None
            if "rule" in c:
                rule = c["rule"]
            elif "extUrl" in c:
                rule = ext_rules.get(urljoin(clean_source_url(n.url), c["extUrl"]))
                if rule is None:
                    report.add_skip("ext_unresolved")
                    continue
            if rule is not None:
                fields = xbpq_fields(rule)
                base = xbpq_home_url(fields) if fields else ""
                if len(json.dumps(rule, ensure_ascii=False).encode()) > RULE_MAX_BYTES:
                    report.add_skip("rule_too_large")
                    continue
            else:
                base = import_base(str(c.get("api", "")))
            if not base or not registry_accepts(base):
                report.add_skip("invalid_url")
                continue
            key = base.rstrip("/").lower()
            if key in seen_bases:
                report.add_skip("duplicate")
                continue
            if len(sites) >= max_sites:
                report.add_skip("over_limit")
                continue
            name = clean_name(str(c.get("name", ""))) or _fallback_name(base)
            cost = _estimate_record_bytes(name, base, rule)
            if used_bytes + cost > registry_budget:
                report.add_skip("over_budget")
                continue
            used_bytes += cost
            seen_bases.add(key)
            item: Dict[str, Any] = {"name": name, "url": base}
            if rule is not None:
                item["rule"] = rule
                report.rule_sites += 1
            else:
                report.api_sites += 1
            sites.append(item)
            contributed = True
        if contributed:
            report.repos_used += 1
    report.exported = len(sites)
    payload = {"format": QUANJU_FORMAT, "version": QUANJU_VERSION, "count": len(sites), "sites": sites}
    return payload, report


def format_quanju_json(payload: Dict[str, Any]) -> str:
    """与全剧视界导出一致：2 空格缩进、不转义 HTML 字符与中文。"""
    return json.dumps(payload, ensure_ascii=False, indent=2)


def quanju_default_filename(today: Optional[date] = None) -> str:
    """沿用全剧视界自身的导出文件名样式（customSourceExportFilename）。"""
    d = today or date.today()
    return f"duanju-custom-sources-{d.isoformat()}.json"
