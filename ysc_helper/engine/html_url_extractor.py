"""预编译正则 + 有序集合去重的聚合页 URL 提取器（对应 Android 版 engine/HtmlUrlExtractor.kt）。"""
from __future__ import annotations

import re
from typing import List

_URL_RE = re.compile(r"""https?://[^\s"'<>()\[\]\\]+""")
_TRAILING_PUNCT_RE = re.compile(r"[.,;:，。；：、)\]}>]+$")
_SKIP_SUFFIX_RE = re.compile(
    r"\.(?:png|jpe?g|gif|webp|svg|ico|css|js|woff2?|ttf|eot|mp4|mp3|apk|ipa|exe|zip|rar|7z)$", re.IGNORECASE)

SKIP_KEYWORDS = (
    "github.com/", "gitlab.com/", "gitee.com/", "zhihu.com", "bilibili", "baidu.com",
    "google.com", "bing.com", "youtube", "douyin", "weibo", "taobao", "jd.com",
    "xiaomi.com", "360.cn", "sohu", "163.com", "sina.com", "cloudflare",
    "jsdelivr.net", "unpkg.com", "gravatar", "w3.org", "mozilla.org", "jquery",
    "bootstrap", "miit.gov.cn", "flowus.cn", "allflow.cn", "staticfile.org", "cdn.staticfile",
)
LINE_KEYWORDS = (
    "/tv", "/box", "/api", ".json", ".txt", "/dc", "kstore", "jundie", "catvod",
    "tvbox", "yingm", "liucn", "nxog", "tspider", "/dm/", "dcang", "仓库", "线路", "接口", "配置",
)


def extract_candidate_urls(text: str, max_per_page: int = 60) -> List[str]:
    if not text or not text.strip():
        return []
    unescaped = text.replace("&amp;", "&")
    seen: dict = {}
    for m in _URL_RE.finditer(unescaped):
        cand = _TRAILING_PUNCT_RE.sub("", m.group(0))
        low = cand.lower()
        if not low.startswith("http"):
            continue
        if _SKIP_SUFFIX_RE.search(low):
            continue
        if ".html" in low or "/app/" in low or "/soft" in low or "/zt" in low:
            continue
        if any(k in low for k in SKIP_KEYWORDS):
            continue
        if not any(k in low for k in LINE_KEYWORDS):
            continue
        seen[cand] = None
        if len(seen) >= max_per_page:
            break
    return list(seen)
