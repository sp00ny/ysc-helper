"""URL 规范化、IDN Punycode 转换、GitHub 代理清洗与 SSRF 防护
（对应 Android 版 security/UrlSecurityValidator.kt + network/SafeSsrfDns）。"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

_PROXY_PREFIX_RE = re.compile(
    r"^https?://[^/]*?(?:gh-proxy\.com|ghproxy\.net|ghproxy\.com|ghfast\.top|gh\.proxy\.ustclug\.org|mirror\.ghproxy\.com)/+",
    re.IGNORECASE,
)
_DOUBLE_SCHEME_RE = re.compile(r"^(https?)://(https?://)", re.IGNORECASE)
_FORBIDDEN_HOST_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".localhost")
_HOST_CHARS_RE = re.compile(r"^[a-z0-9.\-]+$")
_PORT_RE = re.compile(r"^[0-9]{1,5}$")

# Clash / Surge 等代理的 Fake-IP（TUN）模式会把所有域名解析到 198.18.0.0/15。
# 默认与 Android 版一致予以拦截；用户可在设置中显式开启兼容（仅放行 DNS 解析结果，IP 字面量仍拦截）。
_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")
_allow_fake_ip = False


def set_allow_fake_ip(allow: bool) -> None:
    global _allow_fake_ip
    _allow_fake_ip = bool(allow)


def is_fake_ip(address: Union[str, "ipaddress.IPv4Address", "ipaddress.IPv6Address"]) -> bool:
    if isinstance(address, str):
        try:
            address = ipaddress.ip_address(address.strip("[]").split("%", 1)[0])
        except ValueError:
            return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return isinstance(address, ipaddress.IPv4Address) and address in _FAKE_IP_NET


@dataclass(frozen=True)
class Valid:
    ascii_url: str
    normalized_key: str
    is_https: bool


@dataclass(frozen=True)
class Invalid:
    reason: str


ValidationResult = Union[Valid, Invalid]


def validate_and_prepare_url(raw_url: Optional[str], https_only: bool = False) -> ValidationResult:
    """校验并将含中文域名（IDN）或非 ASCII 路径的 URL 转为可请求的标准 URL，同时做 SSRF 静态主机拦截。"""
    trimmed = (raw_url or "").strip()
    if not trimmed:
        return Invalid("空地址")

    sep = trimmed.find("://")
    if sep <= 0:
        return Invalid("缺少协议头(http/https)")
    scheme = trimmed[:sep].lower()
    if scheme not in ("http", "https"):
        return Invalid(f"不支持的协议: {scheme}")
    if https_only and scheme != "https":
        return Invalid("已开启仅 HTTPS 安全策略，拦截明文 HTTP")

    without_fragment = trimmed.split("#", 1)[0]
    rest = without_fragment[sep + 3:]
    if not rest:
        return Invalid("缺少主机名")

    auth_end = -1
    for idx, ch in enumerate(rest):
        if ch in "/?":
            auth_end = idx
            break
    authority = rest[:auth_end] if auth_end >= 0 else rest
    path_and_query = rest[auth_end:] if auth_end >= 0 else ""

    # 禁止 userinfo（如 https://github.com@192.168.1.1）防钓鱼绕过
    if "@" in authority:
        return Invalid("禁止包含认证伪装信息(@)")

    raw_host, port_part = _split_host_port(authority)
    if not raw_host.strip():
        return Invalid("无效主机名")

    try:
        ascii_host = _to_ascii_host(raw_host)
    except Exception:
        return Invalid("域名 Punycode 转换失败")

    if not _is_valid_host_syntax(ascii_host):
        return Invalid("非法域名或主机格式")
    if _is_forbidden_hostname(ascii_host):
        return Invalid("安全拦截：禁止访问本地或内网主机名")

    if _looks_like_ip_literal(ascii_host):
        try:
            addr = ipaddress.ip_address(ascii_host.strip("[]"))
        except ValueError:
            return Invalid("无效 IP 地址")
        if is_blocked_address(addr):
            return Invalid(f"安全拦截：禁止访问内网/回环/保留 IP ({addr})")

    port_num: Optional[int] = None
    if port_part is not None:
        if not _PORT_RE.match(port_part):
            return Invalid("无效端口号")
        port_num = int(port_part)
        if not 1 <= port_num <= 65535:
            return Invalid(f"端口越界: {port_num}")

    if port_num is None or (scheme == "http" and port_num == 80) or (scheme == "https" and port_num == 443):
        norm_port = ""
    else:
        norm_port = f":{port_num}"

    encoded = _encode_non_ascii(path_and_query)
    ascii_url = f"{scheme}://{ascii_host}{norm_port}{encoded}"
    return Valid(ascii_url=ascii_url,
                 normalized_key=_build_normalized_key(scheme, ascii_host, norm_port, encoded),
                 is_https=scheme == "https")


def normalize_url(raw_url: Optional[str]) -> str:
    """仅小写化 scheme 与 host、消除默认端口，严格保留 Path 与 Query 大小写（修复原版 BUG-01）。"""
    res = validate_and_prepare_url(raw_url, https_only=False)
    if isinstance(res, Valid):
        return res.normalized_key
    return (raw_url or "").strip()


def clean_source_url(raw_url: Optional[str]) -> str:
    """剥离 GitHub 加速代理前缀，必要时补齐 https://（修复原版 BUG-07）。"""
    u = (raw_url or "").strip()
    if not u:
        return u
    if _PROXY_PREFIX_RE.search(u):
        u = _PROXY_PREFIX_RE.sub("", u, count=1)
        if not u.lower().startswith(("http://", "https://")):
            u = "https://" + u
    return _DOUBLE_SCHEME_RE.sub(r"\2", u, count=1)


IPAddr = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


def is_blocked_address(address: Union[IPAddr, str]) -> bool:
    """SSRF 核心防线：拦截回环、私网、链路本地（含云元数据 169.254.169.254）、CGNAT、组播、
    保留地址以及 IPv4-mapped IPv6。"""
    if isinstance(address, str):
        address = ipaddress.ip_address(address.strip("[]").split("%", 1)[0])

    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return is_blocked_address(mapped)
        if address.is_unspecified or address.is_loopback or address.is_link_local or address.is_multicast:
            return True
        if address.is_site_local:  # fec0::/10
            return True
        b = address.packed
        if (b[0] & 0xFE) == 0xFC:  # ULA fc00::/7
            return True
        if b[0] == 0xFE and (b[1] & 0xC0) == 0x80:  # fe80::/10
            return True
        return False

    b = address.packed
    b0, b1 = b[0], b[1]
    if b0 in (0, 127, 10):
        return True
    if b0 == 100 and 64 <= b1 <= 127:  # CGNAT 100.64.0.0/10
        return True
    if b0 == 169 and b1 == 254:  # 链路本地 / 云元数据
        return True
    if b0 == 172 and 16 <= b1 <= 31:
        return True
    if b0 == 192 and b1 == 168:
        return True
    if b0 == 198 and b1 in (18, 19):  # 基准测试网段
        return True
    if b0 >= 224:  # 组播 224/4 + 保留 240/4
        return True
    return False


def resolve_safe_addresses(host: str, port: int) -> List[Tuple]:
    """防 DNS Rebinding：解析主机名并过滤所有内网/保留地址，全部被拦截时抛出 OSError。

    返回 ``socket.getaddrinfo`` 结果中安全的条目。"""
    infos = socket.getaddrinfo(host.strip("[]"), port, type=socket.SOCK_STREAM)
    safe = [i for i in infos
            if not is_blocked_address(i[4][0]) or (_allow_fake_ip and is_fake_ip(i[4][0]))]
    if not safe:
        if infos and all(is_fake_ip(i[4][0]) for i in infos):
            raise OSError(f"安全拦截：域名 {host} 解析为代理 Fake-IP(198.18.x.x)，"
                          f"如正在使用 Clash/TUN 代理请在设置中开启「兼容代理 Fake-IP 模式」")
        raise OSError(f"安全拦截：域名 {host} 解析结果指向内网或保留地址")
    return safe


# --------------------------------------------------------------------------- internals

def _to_ascii_host(raw_host: str) -> str:
    if raw_host.startswith("["):
        return raw_host.lower()
    if raw_host.isascii():
        return raw_host.lower()
    labels = []
    for label in raw_host.split("."):
        if label.isascii():
            labels.append(label.lower())
        else:
            labels.append(label.encode("idna").decode("ascii").lower())
    return ".".join(labels)


def _split_host_port(authority: str) -> Tuple[str, Optional[str]]:
    if authority.startswith("["):
        close = authority.find("]")
        if close < 0:
            return "", None
        host = authority[:close + 1]
        rem = authority[close + 1:]
        if rem.startswith(":"):
            return host, rem[1:]
        return (host, None) if rem == "" else ("", None)
    idx = authority.rfind(":")
    if idx >= 0:
        return authority[:idx], authority[idx + 1:]
    return authority, None


def _is_valid_host_syntax(host: str) -> bool:
    if host.startswith("[") and host.endswith("]"):
        return len(host) > 2
    if host.startswith(".") or host.endswith(".") or ".." in host:
        return False
    if "." not in host:
        return False
    return bool(_HOST_CHARS_RE.match(host))


def _is_forbidden_hostname(host: str) -> bool:
    clean = host.strip("[]")
    if clean in ("localhost", "ip6-localhost", "ip6-loopback"):
        return True
    return clean.endswith(_FORBIDDEN_HOST_SUFFIXES)


def _looks_like_ip_literal(host: str) -> bool:
    if host.startswith("[") and host.endswith("]"):
        return True
    parts = host.split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)


def _encode_non_ascii(path_and_query: str) -> str:
    if not path_and_query:
        return ""
    out = []
    for ch in path_and_query:
        code = ord(ch)
        if 0x21 <= code <= 0x7E:
            out.append(ch)
        elif ch == " ":
            out.append("%20")
        else:
            out.append("".join(f"%{b:02X}" for b in ch.encode("utf-8")))
    return "".join(out)


def _build_normalized_key(scheme: str, host: str, port: str, pq: str) -> str:
    if not pq or pq == "/":
        cleaned = ""
    elif "?" in pq:
        p, q = pq.split("?", 1)
        p = p.rstrip("/")
        cleaned = p if not q else f"{p}?{q}"
    else:
        cleaned = pq.rstrip("/")
    return f"{scheme}://{host}{port}{cleaned}"
