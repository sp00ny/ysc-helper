"""流式限流 HTTP 探测客户端（对应 Android 版 network/ProbeHttpClient.kt + SafeRedirectInterceptor）。

- 精确测量 TTFB（响应头到达）与总下载耗时
- 4 MB 解压后流式读取硬上限（防超大流 / Gzip 炸弹）
- 每一跳 30x 重定向都重新做 URL 安全校验（协议、内网、仅 HTTPS 策略）
- 支持一键取消：关闭全部在途响应
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional, Set
from urllib.parse import urljoin

import requests

from ..security import Invalid, validate_and_prepare_url
from .safe_transport import SafeHTTPAdapter

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
BUFFER_SIZE = 8 * 1024
MAX_REDIRECTS = 5
TVBOX_USER_AGENT = "okhttp/3.15"


@dataclass
class HttpFetchOutcome:
    ok: bool
    status_code: int = 0
    body_bytes: bytes = b""
    ttfb_ms: Optional[int] = None
    total_ms: Optional[int] = None
    size_kb: Optional[float] = None
    final_url: str = ""
    is_https: bool = False
    error: str = ""


class ProbeHttpClient:
    def __init__(self, max_per_host: int = 6, pool_connections: int = 32) -> None:
        self._local = threading.local()
        self._max_per_host = max_per_host
        self._pool_connections = pool_connections
        self._lock = threading.Lock()
        self._in_flight: Set[requests.Response] = set()
        self._in_flight_count = 0
        self._cancel_event = threading.Event()

    # ---- 生命周期 / 取消 ----
    def reset_cancel(self) -> None:
        self._cancel_event.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def cancel_all_calls(self) -> None:
        self._cancel_event.set()
        with self._lock:
            victims = list(self._in_flight)
        for r in victims:
            try:
                r.close()
            except Exception:
                pass

    def in_flight_call_count(self) -> int:
        with self._lock:
            return self._in_flight_count

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.trust_env = False  # 忽略环境代理，避免绕过 SSRF 连接层
            adapter = SafeHTTPAdapter(pool_connections=self._pool_connections,
                                      pool_maxsize=self._max_per_host, max_retries=0)
            s.mount("http://", adapter)
            s.mount("https://", adapter)
            s.headers.update({"User-Agent": TVBOX_USER_AGENT,
                              "Accept": "application/json, text/plain, */*"})
            self._local.session = s
        return s

    # ---- 抓取 ----
    def fetch(self, raw_url: str, connect_timeout_ms: int = 4500, read_timeout_ms: int = 5500,
              https_only: bool = False) -> HttpFetchOutcome:
        v = validate_and_prepare_url(raw_url, https_only=https_only)
        if isinstance(v, Invalid):
            return HttpFetchOutcome(ok=False, error=v.reason)
        if self.cancelled:
            return HttpFetchOutcome(ok=False, error="已取消")

        with self._lock:
            self._in_flight_count += 1
        try:
            return self._fetch_inner(v.ascii_url, connect_timeout_ms, read_timeout_ms, https_only)
        finally:
            with self._lock:
                self._in_flight_count -= 1

    def _track(self, resp: requests.Response, add: bool) -> None:
        with self._lock:
            if add:
                self._in_flight.add(resp)
            else:
                self._in_flight.discard(resp)

    def _fetch_inner(self, url: str, cto: int, rto: int, https_only: bool) -> HttpFetchOutcome:
        session = self._session()
        start = time.perf_counter()
        deadline = start + (cto + rto + 1500) / 1000.0
        timeout = (cto / 1000.0, rto / 1000.0)
        current = url
        resp: Optional[requests.Response] = None
        try:
            for _hop in range(MAX_REDIRECTS + 1):
                resp = session.get(current, stream=True, allow_redirects=False, timeout=timeout)
                self._track(resp, True)
                if resp.is_redirect:
                    location = resp.headers.get("Location", "")
                    target = urljoin(current, location)
                    check = validate_and_prepare_url(target, https_only=https_only)
                    self._track(resp, False)
                    resp.close()
                    resp = None
                    if isinstance(check, Invalid):
                        return HttpFetchOutcome(ok=False, error=f"安全拦截重定向: {check.reason}")
                    current = check.ascii_url
                    continue
                break
            else:
                return HttpFetchOutcome(ok=False, error="重定向次数过多")

            assert resp is not None
            ttfb = max(1, int((time.perf_counter() - start) * 1000))
            code = resp.status_code
            is_https = current.lower().startswith("https://")
            if not 200 <= code <= 299:
                return HttpFetchOutcome(ok=False, status_code=code, ttfb_ms=ttfb, total_ms=ttfb,
                                        final_url=current, is_https=is_https, error=f"HTTP {code}")

            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                return HttpFetchOutcome(ok=False, status_code=code,
                                        error=f"响应体积超限 ({int(declared) // 1024}KB > 4096KB)")

            buf = bytearray()
            for chunk in resp.iter_content(BUFFER_SIZE):  # 已自动解压 gzip/deflate
                if self.cancelled:
                    return HttpFetchOutcome(ok=False, status_code=code, error="已取消")
                buf.extend(chunk)
                if len(buf) > MAX_RESPONSE_BYTES:
                    return HttpFetchOutcome(ok=False, status_code=code,
                                            error="流式下载超限 (>4MB)，已阻断防止内存溢出")
                if time.perf_counter() > deadline:
                    return HttpFetchOutcome(ok=False, status_code=code, ttfb_ms=ttfb, error="连接/读取超时")

            total = max(ttfb, int((time.perf_counter() - start) * 1000))
            data = bytes(buf)
            return HttpFetchOutcome(ok=True, status_code=code, body_bytes=data, ttfb_ms=ttfb,
                                    total_ms=total, size_kb=len(data) / 1024.0,
                                    final_url=current, is_https=is_https)
        except requests.exceptions.Timeout:
            return HttpFetchOutcome(ok=False, error="已取消" if self.cancelled else "连接/读取超时")
        except requests.exceptions.RequestException as e:
            if self.cancelled:
                return HttpFetchOutcome(ok=False, error="已取消")
            return HttpFetchOutcome(ok=False, error=_friendly_error(e))
        except Exception as e:  # 读取中断等
            if self.cancelled:
                return HttpFetchOutcome(ok=False, error="已取消")
            return HttpFetchOutcome(ok=False, error=f"读取中断: {e.__class__.__name__}")
        finally:
            if resp is not None:
                self._track(resp, False)
                resp.close()


def _friendly_error(e: Exception) -> str:
    msg = str(e)
    if "安全拦截" in msg:
        start = msg.find("安全拦截")
        end = msg.find("'", start)
        return msg[start:end if end > 0 else None].rstrip(")\"' ")
    if "NameResolution" in msg or "getaddrinfo" in msg or "Name or service not known" in msg:
        return "域名解析失败"
    if "SSLError" in msg or "CERTIFICATE" in msg.upper():
        return "TLS 证书校验失败"
    if "Connection refused" in msg:
        return "连接被拒绝"
    return e.__class__.__name__
