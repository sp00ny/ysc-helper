"""ProbeHttpClient：4MB 上限、重定向安全校验、HTTP 状态判定、DNS 防重绑定。

本地测试服务器在 127.0.0.1 上运行——这本身会被 SSRF 防护拦截，所以测试中只对
「测试服务器的确切地址」放行，其余地址（包括重定向目标）仍走真实校验。
"""
import gzip
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import ysc_helper.network.probe_http_client as phc
import ysc_helper.network.safe_transport as st
from ysc_helper.network import MAX_RESPONSE_BYTES, ProbeHttpClient
from ysc_helper.security import Valid, validate_and_prepare_url

BIG = b"x" * (MAX_RESPONSE_BYTES + 10)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path
        if p == "/ok":
            self._send(200, b'{"sites":[1]}')
        elif p == "/404":
            self._send(404, b"nope")
        elif p == "/big-declared":
            self._send(200, BIG)
        elif p == "/big-chunked":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(BIG), 65536):
                c = BIG[i:i + 65536]
                self.wfile.write(f"{len(c):x}\r\n".encode() + c + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        elif p == "/gzip-bomb":
            body = gzip.compress(b"\0" * (MAX_RESPONSE_BYTES * 3))
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == "/redir-private":
            self.send_response(302)
            self.send_header("Location", "http://192.168.1.1/admin")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif p == "/redir-file":
            self.send_response(301)
            self.send_header("Location", "file:///etc/passwd")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif p == "/redir-ok":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(500, b"err")

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


@pytest.fixture
def client(server, monkeypatch):
    port = server.server_address[1]
    test_prefix = f"http://test.server.example:{port}"
    real_validate = validate_and_prepare_url

    def validate(url, https_only=False):
        if url and url.startswith(test_prefix):
            r = real_validate(url, https_only)
            return r
        return real_validate(url, https_only)

    real_resolve = st.resolve_safe_addresses

    def resolve(host, p):
        if host == "test.server.example":
            import socket
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p))]
        return real_resolve(host, p)

    monkeypatch.setattr(phc, "validate_and_prepare_url", validate)
    monkeypatch.setattr(st, "resolve_safe_addresses", resolve)
    return ProbeHttpClient(), test_prefix


def test_ok_and_ttfb(client):
    c, base = client
    r = c.fetch(base + "/ok")
    assert r.ok and r.body_bytes == b'{"sites":[1]}'
    assert r.ttfb_ms >= 1 and r.total_ms >= r.ttfb_ms
    assert not r.is_https


def test_http_error_status_is_failure(client):
    c, base = client
    r = c.fetch(base + "/404")
    assert not r.ok and r.error == "HTTP 404"


def test_declared_oversize_rejected(client):
    c, base = client
    r = c.fetch(base + "/big-declared")
    assert not r.ok and "超限" in r.error


def test_streamed_oversize_rejected(client):
    c, base = client
    r = c.fetch(base + "/big-chunked")
    assert not r.ok and "4MB" in r.error


def test_gzip_bomb_rejected(client):
    c, base = client
    r = c.fetch(base + "/gzip-bomb")
    assert not r.ok and "4MB" in r.error


def test_redirect_to_private_ip_blocked(client):
    c, base = client
    r = c.fetch(base + "/redir-private")
    assert not r.ok and "安全拦截重定向" in r.error


def test_redirect_to_file_scheme_blocked(client):
    c, base = client
    r = c.fetch(base + "/redir-file")
    assert not r.ok and "不支持的协议" in r.error


def test_safe_relative_redirect_followed(client):
    c, base = client
    r = c.fetch(base + "/redir-ok")
    assert r.ok and r.final_url.endswith("/ok")


def test_real_dns_layer_blocks_loopback_hostname(monkeypatch):
    """不打补丁时，即便 URL 校验被绕过，连接层仍会拒绝解析到回环的域名。"""
    import socket
    monkeypatch.setattr(phc, "validate_and_prepare_url",
                        lambda u, https_only=False: Valid(u, u, False))
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))])
    r = ProbeHttpClient().fetch("http://rebind.attacker.example/x")
    assert not r.ok and "安全拦截" in r.error


def test_static_validation_happens_before_network():
    r = ProbeHttpClient().fetch("http://127.0.0.1:5555/")
    assert not r.ok and "内网" in r.error
