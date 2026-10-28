"""防 DNS Rebinding 的 urllib3 连接层（对应 Android 版 SafeSsrfDns）。

在 Socket 建立之前对域名解析结果逐一做 SSRF 校验，并且直接连接到已校验过的 IP，
避免「校验时解析一次、连接时再解析一次」的 TOCTOU 重绑定绕过。
"""
from __future__ import annotations

import socket

from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NameResolutionError, NewConnectionError

from ..security import resolve_safe_addresses


def _safe_connect(conn) -> socket.socket:
    host = conn._dns_host
    try:
        infos = resolve_safe_addresses(host, conn.port)
    except socket.gaierror as e:
        raise NameResolutionError(conn.host, conn, e) from e
    except OSError as e:  # 安全拦截
        raise NewConnectionError(conn, str(e)) from e

    last_err: Exception | None = None
    for family, socktype, proto, _canon, sockaddr in infos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            for opt in conn.socket_options or []:
                sock.setsockopt(*opt)
            t = conn.timeout
            if isinstance(t, (int, float)):
                sock.settimeout(t)
            if conn.source_address:
                sock.bind(conn.source_address)
            sock.connect(sockaddr)
            return sock
        except socket.timeout as e:
            if sock is not None:
                sock.close()
            from urllib3.exceptions import ConnectTimeoutError
            raise ConnectTimeoutError(conn, f"连接超时 {conn.host}") from e
        except OSError as e:
            last_err = e
            if sock is not None:
                sock.close()
    raise NewConnectionError(conn, f"无法连接 {conn.host}: {last_err}")


class SafeHTTPConnection(HTTPConnection):
    def _new_conn(self) -> socket.socket:  # type: ignore[override]
        return _safe_connect(self)


class SafeHTTPSConnection(HTTPSConnection):
    def _new_conn(self) -> socket.socket:  # type: ignore[override]
        return _safe_connect(self)


class SafeHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = SafeHTTPConnection


class SafeHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = SafeHTTPSConnection


class SafeHTTPAdapter(HTTPAdapter):
    """所有连接都经过 SSRF 安全 DNS 的 requests 适配器。"""

    def init_poolmanager(self, *args, **kwargs):  # noqa: D401
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": SafeHTTPConnectionPool,
            "https": SafeHTTPSConnectionPool,
        }
