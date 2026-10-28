"""离线演示模式：内存 Mock HTTP 传输 + 示例配置。

真实的探测引擎、解析器（含图片隐写 / AES / Base64 / 注释 JSON）与 URL 安全校验都会完整运行，
只是网络层被替换为本地内存数据，便于在无网络或演示 / 截图时使用：``python main.py --demo``。
"""
from __future__ import annotations

import base64
import json
import random
import threading
import time
from typing import Dict, List, Optional, Tuple

from .models import SeedLine
from .network import HttpFetchOutcome
from .parser.tvbox_payload_parser import encrypt_catvod_aes_hex
from .security import Invalid, validate_and_prepare_url

H = "https://demo.ysc-helper.example"


def _single(name: str, n_sites: int, spider: str = "https://demo.ysc-helper.example/jar/spider.jar") -> dict:
    return {"spider": spider, "name": name,
            "sites": [{"key": f"s{i}", "name": f"{name}-站点{i}", "type": 3, "api": "csp_Demo"} for i in range(n_sites)],
            "lives": [], "parses": []}


def build_demo_routes() -> Dict[str, Tuple[int, bytes, int]]:
    """url -> (HTTP 状态码, 响应体, 模拟延迟 ms)"""
    routes: Dict[str, Tuple[int, bytes, int]] = {}

    def put(path: str, body, status: int = 200, delay: int = 120) -> None:
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        routes[f"{H}{path}"] = (status, body, delay)

    put("/fty/tv.json", _single("饭太硬(演示)", 42), delay=180)
    put("/fm/api.json", """
        /* 肥猫风格：带块注释、行注释与尾随逗号 */
        {
          "spider": "http://demo.ysc-helper.example/fm/spider.jar;md5;abc", // 明文 JAR 插件
          "sites": [
            {"key": "fm1", "name": "肥猫-1", "api": "https://api.demo.ysc-helper.example/vod"},
            {"key": "fm2", "name": "肥猫-2", "api": "https://api.demo.ysc-helper.example/vod2"},
          ],
        }
    """, delay=420)
    stego_json = json.dumps(_single("摸鱼儿(隐写)", 18), ensure_ascii=False).encode("utf-8")
    put("/myer/cover.png", b"\x89PNG\r\n\x1a\n" + bytes(random.Random(1).randrange(256) for _ in range(256))
        .replace(b"**", b"*_") + b"**" + base64.b64encode(stego_json), delay=650)
    aes_plain = json.dumps(_single("蚂蚁线路(AES)", 25), ensure_ascii=False)
    put("/mayi/0110.txt", encrypt_catvod_aes_hex(aes_plain, "ysc-demo-key", "1700000000000"), delay=900)
    put("/duo/dm.txt", base64.b64encode(json.dumps(_single("多多(Base64)", 12), ensure_ascii=False).encode()),
        delay=300)
    put("/slow/tv.json", _single("慢速线路", 6), delay=2300)
    put("/broken/404.json", "Not Found", status=404, delay=90)
    put("/broken/html.json", "<html><body>维护中</body></html>", delay=140)
    put("/broken/unknown.json", {"hello": "world"}, delay=110)
    # 多仓：urls 结构
    put("/store/urls.json", {"urls": [
        {"name": "饭太硬", "url": f"{H}/fty/tv.json"},
        {"name": "肥猫", "url": f"{H}/fm/api.json"},
        {"name": "多多聚合", "url": f"{H}/duo/dm.txt"},
        {"name": "失效线路", "url": f"{H}/broken/404.json"},
        {"name": "内网地址(被拦截)", "url": "http://192.168.1.1/tv.json"},
    ]}, delay=260)
    # 多仓：storeHouse 结构，嵌套多仓 + 循环引用
    put("/store/house.json", {"storeHouse": [
        {"sourceName": "摸鱼儿隐写", "sourceUrl": f"{H}/myer/cover.png"},
        {"sourceName": "蚂蚁 AES", "sourceUrl": f"{H}/mayi/0110.txt"},
        {"sourceName": "慢速线路", "sourceUrl": f"{H}/slow/tv.json"},
        {"sourceName": "嵌套多仓(urls)", "sourceUrl": f"{H}/store/urls.json"},
        {"sourceName": "循环引用自身", "sourceUrl": f"{H}/store/house.json"},
        {"sourceName": "维护页", "sourceUrl": f"{H}/broken/html.json"},
    ]}, delay=340)
    put("/store/dead.json", {"urls": [
        {"name": "失效A", "url": f"{H}/broken/404.json"},
        {"name": "结构不明", "url": f"{H}/broken/unknown.json"},
    ]}, delay=200)
    put("/agg/page.html", f"""<html><body>
        今日可用线路：<a href="{H}/duo/dm.txt">多多</a>，备用 {H}/fty/tv.json；
        <img src="{H}/static/logo.png"> 下载 APP {H}/app/ysc.apk
        </body></html>""", delay=150)
    return routes


DEMO_SEEDS: List[SeedLine] = [
    SeedLine("月光宝盒多仓(演示)", f"{H}/store/house.json"),
    SeedLine("影视仓聚合(演示)", f"{H}/store/urls.json"),
    SeedLine("饭太硬", f"{H}/fty/tv.json"),
    SeedLine("全失效多仓", f"{H}/store/dead.json"),
    SeedLine("超时线路", f"{H}/timeout/tv.json"),
    SeedLine("宝盒接口(中文域名)", "http://宝盒接口.example/tv"),
]
DEMO_AGGREGATOR_PAGES: List[str] = [f"{H}/agg/page.html"]


class MockHttpClient:
    """与 ProbeHttpClient 接口一致的内存传输（含真实 URL 安全校验与取消语义）。"""

    def __init__(self, routes: Optional[Dict[str, Tuple[int, bytes, int]]] = None, speed: float = 1.0) -> None:
        self.routes = routes if routes is not None else build_demo_routes()
        self.speed = speed
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._in_flight = 0

    def reset_cancel(self) -> None:
        self._cancel.clear()

    def cancel_all_calls(self) -> None:
        self._cancel.set()

    def in_flight_call_count(self) -> int:
        return self._in_flight

    def fetch(self, raw_url: str, connect_timeout_ms: int = 4500, read_timeout_ms: int = 5500,
              https_only: bool = False) -> HttpFetchOutcome:
        v = validate_and_prepare_url(raw_url, https_only=https_only)
        if isinstance(v, Invalid):
            return HttpFetchOutcome(ok=False, error=v.reason)
        with self._lock:
            self._in_flight += 1
        try:
            route = self.routes.get(v.ascii_url)
            delay = route[2] if route else (connect_timeout_ms if "/timeout/" in v.ascii_url else 80)
            jitter = random.uniform(0.85, 1.15)
            ttfb_s = delay * jitter * self.speed / 1000.0
            if self._cancel.wait(ttfb_s):
                return HttpFetchOutcome(ok=False, error="已取消")
            ttfb = max(1, int(delay * jitter))
            if route is None:
                if "/timeout/" in v.ascii_url:
                    return HttpFetchOutcome(ok=False, error="连接/读取超时")
                return HttpFetchOutcome(ok=False, error="域名解析失败")
            status, body, _ = route
            if not 200 <= status <= 299:
                return HttpFetchOutcome(ok=False, status_code=status, ttfb_ms=ttfb, total_ms=ttfb,
                                        is_https=v.is_https, error=f"HTTP {status}")
            dl = max(1, int(len(body) / 400))
            time.sleep(dl * self.speed / 1000.0)
            return HttpFetchOutcome(ok=True, status_code=status, body_bytes=body, ttfb_ms=ttfb,
                                    total_ms=ttfb + dl, size_kb=len(body) / 1024.0, final_url=v.ascii_url,
                                    is_https=v.is_https)
        finally:
            with self._lock:
                self._in_flight -= 1
