import ipaddress
import socket

import pytest

from ysc_helper.security import (Invalid, Valid, clean_source_url, is_blocked_address, normalize_url,
                                 resolve_safe_addresses, validate_and_prepare_url)


def test_normalize_preserves_case_sensitive_path():
    assert normalize_url("HTTPS://Pastebin.COM:443/raw/5NHaxyGR/") == "https://pastebin.com/raw/5NHaxyGR"
    assert normalize_url("http://A.com:80/XC.json?Key=Ab") == "http://a.com/XC.json?Key=Ab"
    assert normalize_url("http://a.com:8080/") == "http://a.com:8080"
    assert normalize_url("http://a.com/tv#frag") == "http://a.com/tv"


def test_idn_chinese_domain_to_punycode():
    r = validate_and_prepare_url("http://www.饭太硬.com/tv")
    assert isinstance(r, Valid)
    assert r.ascii_url.startswith("http://www.xn--") and r.ascii_url.endswith("/tv")
    assert not r.is_https


def test_non_ascii_path_is_percent_encoded():
    r = validate_and_prepare_url("https://play.iptv365.org/欧歌/api.json")
    assert isinstance(r, Valid)
    assert r.ascii_url == "https://play.iptv365.org/%E6%AC%A7%E6%AD%8C/api.json"


def test_clean_source_url_restores_scheme():
    assert clean_source_url("https://ghproxy.com/raw.githubusercontent.com/FongMi/CatVodSpider/main/json/config.json") \
        == "https://raw.githubusercontent.com/FongMi/CatVodSpider/main/json/config.json"
    assert clean_source_url("https://gh-proxy.com/https://raw.githubusercontent.com/a/b/main/tv.json") \
        == "https://raw.githubusercontent.com/a/b/main/tv.json"
    assert clean_source_url("https://https://a.com/x.json") == "https://a.com/x.json"
    assert clean_source_url("https://a.com/x.json") == "https://a.com/x.json"


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.8", "172.16.5.4", "172.31.255.1", "192.168.1.1",
                                "169.254.169.254", "100.64.1.1", "100.127.0.1", "0.0.0.0", "198.18.0.1",
                                "224.0.0.1", "255.255.255.255", "::1", "::", "::ffff:192.168.1.1",
                                "::ffff:127.0.0.1", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1"])
def test_blocked_addresses(ip):
    assert is_blocked_address(ipaddress.ip_address(ip))
    assert is_blocked_address(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "100.63.255.255", "100.128.0.1", "172.32.0.1",
                                "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_allowed(ip):
    assert not is_blocked_address(ip)


@pytest.mark.parametrize("url,reason_part", [
    ("", "空地址"),
    ("a.com/tv.json", "缺少协议头"),
    ("ftp://a.com/x", "不支持的协议"),
    ("file:///etc/passwd", "不支持的协议"),
    ("http://", "缺少主机名"),
    ("https://github.com@192.168.1.1/x", "认证伪装"),
    ("http://192.168.1.1/dc.json", "内网"),
    ("http://169.254.169.254/latest/meta-data", "内网"),
    ("http://[::1]:8080/", "内网"),
    ("http://[::ffff:10.0.0.1]/", "内网"),
    ("http://localhost:9090/", "非法域名"),
    ("http://router.lan/x", "本地或内网主机名"),
    ("http://nas.local/x", "本地或内网主机名"),
    ("http://a.localhost/x", "本地或内网主机名"),
    ("http://a.com:99999/x", "端口越界"),
    ("http://a.com:abc/x", "无效端口号"),
    ("http://a..com/x", "非法域名"),
    ("http://a_b.com/x", "非法域名"),
    ("http://999.1.1.1/x", "无效 IP"),
])
def test_invalid_urls(url, reason_part):
    r = validate_and_prepare_url(url)
    assert isinstance(r, Invalid), url
    assert reason_part in r.reason


def test_https_only_policy():
    r = validate_and_prepare_url("http://a.com/tv.json", https_only=True)
    assert isinstance(r, Invalid) and "HTTPS" in r.reason
    assert isinstance(validate_and_prepare_url("https://a.com/tv.json", https_only=True), Valid)


def test_public_ipv6_literal_allowed():
    r = validate_and_prepare_url("https://[2606:4700:4700::1111]/x")
    assert isinstance(r, Valid)


def test_dns_resolution_blocks_private_results(monkeypatch):
    fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.10", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: fake)
    with pytest.raises(OSError, match="安全拦截"):
        resolve_safe_addresses("evil-rebind.example", 80)


def test_dns_resolution_filters_mixed_results(monkeypatch):
    fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.1.1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: fake)
    safe = resolve_safe_addresses("mixed.example", 443)
    assert [s[4][0] for s in safe] == ["93.184.216.34"]


def test_fake_ip_blocked_by_default_with_helpful_hint(monkeypatch):
    from ysc_helper.security import set_allow_fake_ip
    set_allow_fake_ip(False)
    fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.7", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: fake)
    with pytest.raises(OSError, match="Fake-IP"):
        resolve_safe_addresses("example.com", 443)


def test_fake_ip_allowed_when_enabled_but_other_private_still_blocked(monkeypatch):
    from ysc_helper.security import set_allow_fake_ip
    fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.7", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: fake)
    set_allow_fake_ip(True)
    try:
        assert [s[4][0] for s in resolve_safe_addresses("example.com", 443)] == ["198.18.0.7"]
        # IP 字面量形式的 198.18.x.x 仍然被静态拦截
        assert isinstance(validate_and_prepare_url("http://198.18.0.7/x"), Invalid)
    finally:
        set_allow_fake_ip(False)
