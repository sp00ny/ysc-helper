import base64
import json

import pytest

from ysc_helper.models import PayloadFormat, RepoKind
from ysc_helper.parser import decrypt_catvod_aes_hex, parse_payload, strip_comments_and_trailing_commas
from ysc_helper.parser.tvbox_payload_parser import encrypt_catvod_aes_hex

SINGLE = {"spider": "https://a.com/s.jar", "sites": [{"key": "a"}, {"key": "b"}], "lives": [], "parses": []}


def test_relaxed_json_with_comments_and_trailing_commas_keeps_urls():
    raw = """
        /* 头部块注释 {fake: 1} */
        // 行注释 {urls: []}
        {
          "spider": "http://example.com/spider.jar;md5;abc", // 行尾注释
          "sites": [
            {"key": "s1", "name": "站点1", "api": "https://api.example.com/vod"},
          ],
        }
    """.encode()
    p = parse_payload(raw)
    assert p is not None
    assert p.kind == RepoKind.SINGLE
    assert p.format == PayloadFormat.JSON_RELAXED
    assert p.site_count == 1
    assert p.has_cleartext_spider_jar


def test_strip_does_not_touch_comment_like_sequences_inside_strings():
    src = '{"a": "http://x.com/*not*/a//b", "b": "q\\"//still", "c": [1, 2, ], }'
    out = strip_comments_and_trailing_commas(src)
    assert json.loads(out) == {"a": "http://x.com/*not*/a//b", "b": 'q"//still', "c": [1, 2]}


def test_strip_preserves_commas_inside_strings():
    out = strip_comments_and_trailing_commas('{"k": "a, }", }')
    assert json.loads(out) == {"k": "a, }"}


def test_plain_json_and_bom():
    p = parse_payload(("\ufeff" + json.dumps(SINGLE)).encode())
    assert p.kind == RepoKind.SINGLE and p.format == PayloadFormat.PLAIN_JSON and p.site_count == 2
    assert not p.has_cleartext_spider_jar


def test_image_steganography_after_double_star():
    js = json.dumps({"urls": [{"name": "子仓A", "url": "https://example.com/a.json"}]}, ensure_ascii=False)
    png = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"\x00\xff**junk\x01"
    raw = png + b"**" + base64.b64encode(js.encode())
    p = parse_payload(raw)
    assert p is not None
    assert p.kind == RepoKind.MULTI_URLS
    assert p.format == PayloadFormat.IMAGE_STEGO
    assert [(s.name, s.url) for s in p.sub_entries] == [("子仓A", "https://example.com/a.json")]


def test_storehouse_multi_repo():
    raw = json.dumps({"storeHouse": [{"sourceName": "仓1", "sourceUrl": "https://a.com/1.json"},
                                     {"sourceName": "空", "sourceUrl": ""}]}).encode()
    p = parse_payload(raw)
    assert p.kind == RepoKind.MULTI_STOREHOUSE
    assert len(p.sub_entries) == 1


def test_pure_base64_json():
    raw = base64.b64encode(json.dumps(SINGLE).encode())
    p = parse_payload(raw)
    assert p.kind == RepoKind.SINGLE and p.format == PayloadFormat.BASE64_JSON


def test_urlsafe_base64_without_padding():
    data = json.dumps({"sites": [{"key": "~~~?>>>"}], "spider": "https://x.y/a.jar"}).encode()
    raw = base64.urlsafe_b64encode(data).rstrip(b"=")
    p = parse_payload(raw)
    assert p is not None and p.kind == RepoKind.SINGLE


@pytest.mark.parametrize("iv", ["1700000000000", "0123456789abcdef"])
def test_catvod_2423_aes_cbc_both_iv_styles(iv):
    plain = json.dumps(SINGLE)
    hex_text = encrypt_catvod_aes_hex(plain, "mykey", iv)
    rest = len(hex_text) - (hex_text.find("2324", 4) + 4)
    assert rest % 32 == (26 if len(iv) == 13 else 0)
    assert decrypt_catvod_aes_hex(hex_text) == plain
    p = parse_payload(hex_text.upper().encode())
    assert p.kind == RepoKind.SINGLE and p.format == PayloadFormat.AES_HEX


def test_catvod_aes_known_vector():
    # 用 cryptography 独立计算的固定向量：key="abc"(补0到16位)，IV="1234567890123"(补0到16位)
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key, iv = b"abc0000000000000", b"1234567890123000"
    p = padding.PKCS7(128).padder()
    body = p.update(b'{"sites":[1]}') + p.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(body) + enc.finalize()
    hex_text = "2423" + b"abc".hex() + "2324" + ct.hex() + b"1234567890123".hex()
    assert decrypt_catvod_aes_hex(hex_text) == '{"sites":[1]}'


def test_aes_rejects_bad_lengths_and_garbage():
    assert decrypt_catvod_aes_hex("2423") is None
    assert decrypt_catvod_aes_hex("2423" + "61" * 40) is None
    assert decrypt_catvod_aes_hex("2423616263" + "2324" + "zz" * 30) is None


def test_unknown_and_invalid_payloads():
    assert parse_payload(b"") is None
    assert parse_payload(b"<html>404</html>") is None
    p = parse_payload(b'{"hello": "world"}')
    assert p.kind == RepoKind.UNKNOWN
    p = parse_payload(b'{"sites": [], "spider": ""}')
    assert p.kind == RepoKind.UNKNOWN


def test_comment_header_containing_braces_is_not_used_as_json():
    raw = b'// demo {"urls":[]}\n{"sites":[{"key":"x"}]}'
    p = parse_payload(raw)
    assert p.kind == RepoKind.SINGLE and p.site_count == 1
