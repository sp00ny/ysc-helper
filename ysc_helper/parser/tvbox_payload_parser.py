"""影视仓 / TVBox 全生态配置解码器（对应 Android 版 parser/TvBoxPayloadParser.kt）。

支持：标准 JSON、带注释/尾逗号 JSON、图片隐写（``**`` + Base64）、纯 Base64、
CatVod ``2423``（``$#key#$cipher+iv``）AES-128-CBC Hex 加密格式。
"""
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..models import PayloadFormat, RepoKind, SeedLine

SINGLE_KEYS = ("spider", "sites", "lives", "parses")


@dataclass
class ParsedRepoPayload:
    kind: RepoKind
    format: PayloadFormat
    name: str
    site_count: int
    has_cleartext_spider_jar: bool
    sub_entries: List[SeedLine] = field(default_factory=list)
    # 全剧视界导出用：可导入站点候选与跳过计数（仅单仓）
    quanju_sites: Optional[List[Dict[str, Any]]] = None
    quanju_skipped: Dict[str, int] = field(default_factory=dict)


def parse_payload(raw_bytes: bytes) -> Optional[ParsedRepoPayload]:
    """解析一份原始响应字节；无法识别时返回 ``None``。"""
    if not raw_bytes:
        return None

    raw_text = raw_bytes.decode("utf-8", errors="replace")
    if raw_text.startswith("\ufeff"):
        raw_text = raw_text[1:]
    raw_text = raw_text.strip()

    # 1a. 明文标准 JSON 快速通道
    if raw_text.startswith("{"):
        obj = _try_parse_json_object(raw_text)
        if obj is not None:
            return _classify(obj, PayloadFormat.PLAIN_JSON)

    # 1b. 状态机剥离注释与尾随逗号（JSON5 / 带注释配置）
    if "{" in raw_text:
        stripped = strip_comments_and_trailing_commas(raw_text)
        idx = stripped.find("{")
        if idx >= 0:
            candidate = stripped[idx:]
            obj = _try_parse_json_object(candidate)
            if obj is not None:
                fmt = PayloadFormat.PLAIN_JSON if candidate == raw_text else PayloadFormat.JSON_RELAXED
                return _classify(obj, fmt)

    # 2. 图片隐写：二进制文件末尾拼接 "**" + Base64(JSON)
    decoded = _decode_stego_after_double_star(raw_bytes)
    if decoded is not None:
        obj = _parse_decoded_json_text(decoded)
        if obj is not None:
            return _classify(obj, PayloadFormat.IMAGE_STEGO)

    # 3. CatVod 2423 AES-128-CBC Hex 加密
    if raw_text.lower().startswith("2423"):
        decrypted = decrypt_catvod_aes_hex(raw_text)
        if decrypted is not None:
            obj = _parse_decoded_json_text(decrypted)
            if obj is not None:
                return _classify(obj, PayloadFormat.AES_HEX)

    # 4. 纯 Base64 编码 JSON
    decoded = _decode_pure_base64(raw_text)
    if decoded is not None:
        obj = _parse_decoded_json_text(decoded)
        if obj is not None:
            return _classify(obj, PayloadFormat.BASE64_JSON)

    return None


# --------------------------------------------------------------------------- helpers

def _try_parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    try:
        v = json.loads(text, strict=False)
    except (ValueError, RecursionError):
        return None
    return v if isinstance(v, dict) else None


def _parse_decoded_json_text(text: str) -> Optional[Dict[str, Any]]:
    clean = text[1:] if text.startswith("\ufeff") else text
    clean = clean.strip()
    obj = _try_parse_json_object(clean)
    if obj is not None:
        return obj
    stripped = strip_comments_and_trailing_commas(clean)
    idx = stripped.find("{")
    if idx >= 0:
        return _try_parse_json_object(stripped[idx:])
    return None


def _prim_str(v: Any) -> str:
    """模拟 kotlinx ``jsonPrimitive.content``：仅原始值可取内容，对象/数组返回空串。"""
    if v is None or isinstance(v, (dict, list)):
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _is_meaningful(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str) and v == "":
        return False
    if isinstance(v, list) and len(v) == 0:
        return False
    return True


def _classify(obj: Dict[str, Any], fmt: PayloadFormat) -> ParsedRepoPayload:
    repo_name = _prim_str(obj.get("name"))
    store_arr = obj.get("storeHouse") if isinstance(obj.get("storeHouse"), list) else None
    urls_arr = obj.get("urls") if isinstance(obj.get("urls"), list) else None

    subs: List[SeedLine] = []
    for item in store_arr or []:
        if isinstance(item, dict):
            s_url = _prim_str(item.get("sourceUrl"))
            if s_url.strip():
                subs.append(SeedLine(_prim_str(item.get("sourceName")), s_url))
    for item in urls_arr or []:
        if isinstance(item, dict):
            u_url = _prim_str(item.get("url"))
            if u_url.strip():
                subs.append(SeedLine(_prim_str(item.get("name")), u_url))

    if store_arr is not None and subs and any(isinstance(i, dict) and "sourceUrl" in i for i in store_arr):
        kind = RepoKind.MULTI_STOREHOUSE
    elif urls_arr is not None and subs and any(isinstance(i, dict) and "url" in i for i in urls_arr):
        kind = RepoKind.MULTI_URLS
    elif any(_is_meaningful(obj.get(k)) for k in SINGLE_KEYS):
        kind = RepoKind.SINGLE
    else:
        kind = RepoKind.UNKNOWN

    sites = obj.get("sites")
    site_count = len(sites) if isinstance(sites, list) else 0
    spider = _prim_str(obj.get("spider")).strip()
    qj_sites: Optional[List[Dict[str, Any]]] = None
    qj_skipped: Dict[str, int] = {}
    if kind == RepoKind.SINGLE:
        from ..export.quanju import extract_quanju_sites
        qj_sites, qj_skipped = extract_quanju_sites(obj)
    return ParsedRepoPayload(
        kind=kind, format=fmt, name=repo_name, site_count=site_count,
        has_cleartext_spider_jar=spider.lower().startswith("http://"), sub_entries=subs,
        quanju_sites=qj_sites, quanju_skipped=qj_skipped,
    )


def strip_comments_and_trailing_commas(text: str) -> str:
    """词法状态机：安全剥离 ``//`` 行注释、``/* */`` 块注释以及尾随逗号，
    绝不误伤双引号字符串内部的 ``http://`` 或 ``/*`` 序列。"""
    out: List[str] = []
    i, n = 0, len(text)
    in_str = False
    escaped = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1

    clean = "".join(out)
    res: List[str] = []
    in_str = False
    escaped = False
    i, m = 0, len(clean)
    while i < m:
        c = clean[i]
        if in_str:
            res.append(c)
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            res.append(c)
            i += 1
            continue
        if c == ",":
            j = i + 1
            while j < m and clean[j].isspace():
                j += 1
            if j < m and clean[j] in "}]":
                i += 1
                continue
        res.append(c)
        i += 1
    return "".join(res)


def _decode_stego_after_double_star(data: bytes) -> Optional[str]:
    # 从后向前查找最后一个 "**"，避免 PNG/JPG 前半段二进制偶然出现 "**"
    pos = data.rfind(b"**")
    if pos < 0:
        return None
    pos += 2
    if pos >= len(data):
        return None
    tail = data[pos:].decode("ascii", errors="replace").strip().replace("\n", "").replace("\r", "")
    if len(tail) < 8:
        return None
    return _decode_pure_base64(tail)


def _b64(candidate: str, altchars: Optional[bytes]) -> bytes:
    s = candidate.rstrip("=")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s, altchars=altchars, validate=True)


def _decode_pure_base64(candidate: str) -> Optional[str]:
    trimmed = candidate.strip().replace("\n", "").replace("\r", "")
    if len(trimmed) < 16 or trimmed.startswith("{") or trimmed.startswith("<"):
        return None
    for alt in (None, b"-_"):
        try:
            return _b64(trimmed, alt).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            continue
    return None


def _pad16(s: str) -> bytes:
    return s.ljust(16, "0")[:16].encode("utf-8")


def decrypt_catvod_aes_hex(hex_input: str) -> Optional[str]:
    """解密 CatVod / 影视仓标准 2423（``$#key#$cipher+iv``）AES-128-CBC Hex 密文。

    兼容 13 位时间戳 IV（hex 长度余 26）与 16 字节完整 IV（hex 长度余 0）。
    """
    try:
        h = hex_input.strip().lower()
        if not h.startswith("2423") or len(h) < 40:
            return None
        key_end = h.find("2324", 4)
        if key_end < 4:
            return None
        key_str = bytes.fromhex(h[4:key_end]).decode("utf-8")
        rest_len = len(h) - (key_end + 4)
        if rest_len > 26 and rest_len % 32 == 26:
            iv_len = 26
        elif rest_len > 32 and rest_len % 32 == 0:
            iv_len = 32
        else:
            return None
        iv_str = bytes.fromhex(h[-iv_len:]).decode("utf-8")
        cipher_bytes = bytes.fromhex(h[key_end + 4: len(h) - iv_len])
        key, iv = _pad16(key_str), _pad16(iv_str)
        if len(key) != 16 or len(iv) != 16:
            return None
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = dec.update(cipher_bytes) + dec.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plain = unpadder.update(padded) + unpadder.finalize()
        return plain.decode("utf-8", errors="replace")
    except Exception:
        return None


def encrypt_catvod_aes_hex(plain: str, key: str, iv: str) -> str:
    """测试 / 演示用：生成 2423 格式密文（与 decrypt 互逆）。"""
    k, v = _pad16(key), _pad16(iv)
    padder = padding.PKCS7(128).padder()
    data = padder.update(plain.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(k), modes.CBC(v)).encryptor()
    ct = enc.update(data) + enc.finalize()
    return "2423" + key.encode("utf-8").hex() + "2324" + ct.hex() + iv.encode("utf-8").hex()
