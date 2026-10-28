"""全剧视界「自定义源」导出：结构须符合全剧视界 Go 核心 provider_custom_maccms.go 的导入约束。

用例数据取自全剧视界仓库自身的测试 provider_custom_import_test.go（TestCustomSourceImportTVBoxSites 等）。
设置环境变量 QUANJU_CORE_DIR=<guoappxxx>/native 时，额外调用真实 Go 导入逻辑做端到端校验。
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ysc_helper.demo import DEMO_SEEDS, MockHttpClient, build_demo_routes
from ysc_helper.engine.probe_engine import MultiThreadProbeEngine
from ysc_helper.engine.thread_pool_manager import ThreadPoolManager
from ysc_helper.export import (QUANJU_FORMAT, build_quanju_export, extract_quanju_sites, format_quanju_json,
                               quanju_default_filename)
from ysc_helper.export.quanju import (clean_name, import_base, registry_accepts, xbpq_fields, xbpq_home_url)
from ysc_helper.models import ProbeSettings, RepoKind, RepoNode
from ysc_helper.parser import parse_payload

H = "https://t.example"
ROOT = Path(__file__).resolve().parents[1]

# 全剧视界 provider_custom_import_test.go → TestCustomSourceImportTVBoxSites 的原始输入
GO_FIXTURE_TVBOX = """{
  "sites": [
    {"key": "cntv", "name": "💕推荐采集", "type": 1, "api": "https://collect.example.com/api.php/provide/vod/"},
    {"key": "sp", "name": "爬虫站", "type": 3, "jar": "csp_XBPQ", "api": "https://sp.example.com"},
    {"key": "xbpq", "name": "规则站", "type": 1, "api": "https://x.example.com", "ext": {"主页url": "https://x.example.com", "分类url": "https://x.example.com/{cateId}-{catePg}.html", "分类": "电影$1#电视剧$2", "数组": "<a&&</a>", "标题": "title=\\"&&\\"", "链接": "href=\\"&&\\""}}
  ]
}"""


def _node(url, total_ms, config, name="线路"):
    parsed = parse_payload(json.dumps(config, ensure_ascii=False).encode()
                           if isinstance(config, dict) else config.encode())
    assert parsed is not None and parsed.kind == RepoKind.SINGLE
    return RepoNode(id=url, name=name, url=url, normalized_url=url, kind=RepoKind.SINGLE, available=True,
                    total_ms=total_ms, quanju_sites=parsed.quanju_sites, quanju_skipped=parsed.quanju_skipped)


def assert_valid_quanju_payload(payload):
    """按全剧视界导入/注册表规则校验整个导出结构。"""
    assert list(payload.keys()) == ["format", "version", "count", "sites"]
    assert payload["format"] == QUANJU_FORMAT == "duanju-custom-sources"
    assert payload["version"] == 1
    assert payload["count"] == len(payload["sites"]) <= 200
    seen = set()
    for site in payload["sites"]:
        assert set(site) <= {"name", "url", "rule", "type"}
        assert isinstance(site["name"], str) and 1 <= len(site["name"]) <= 40
        assert clean_name(site["name"]) == site["name"]            # 全剧视界再规整也不会变
        url = site["url"]
        assert registry_accepts(url), url
        assert "?" not in url and "#" not in url and not url.endswith("/")
        key = url.lower()
        assert key not in seen, f"重复网址 {url}"
        seen.add(key)
        if "rule" in site:
            assert isinstance(site["rule"], dict)                  # 嵌套对象，而非转义字符串
            fields = xbpq_fields(site["rule"])
            assert fields is not None
            assert xbpq_home_url(fields) == url                    # 规则源网址 = 规则主页
        if "type" in site:
            assert site["type"] in ("web", "json")
    # 序列化后仍是合法 JSON，且与全剧视界导出一样不转义 & < >
    text = format_quanju_json(payload)
    assert json.loads(text) == payload
    assert "\\u0026" not in text and "\\u003c" not in text


def test_go_fixture_tvbox_sites_matches_quanju_expectations():
    payload, report = build_quanju_export([_node(f"{H}/a.json", 100, GO_FIXTURE_TVBOX)])
    assert_valid_quanju_payload(payload)
    assert payload["count"] == 2                                   # Go 期望：2 条成功
    assert report.skipped == {"jar": 1}                            # Go 期望：爬虫条目跳过 1
    api = [s for s in payload["sites"] if "rule" not in s]
    rule = [s for s in payload["sites"] if "rule" in s]
    assert api[0]["url"] == "https://collect.example.com/api.php/provide/vod"   # 保留完整端点
    assert api[0]["name"] == "推荐采集"                            # 表情噪音去除
    assert rule[0]["url"] == "https://x.example.com"               # 规则源 base = 主页url
    assert rule[0]["rule"]["分类"] == "电影$1#电视剧$2"


def test_extract_classifies_site_types():
    cfg = {"sites": [
        {"name": "jar", "type": 3, "api": "csp_Bili"},
        {"name": "js", "type": 3, "api": "./drpy2.min.js", "ext": "./js/a.js"},
        {"name": "remote", "type": 4, "api": "https://r.example.com/api"},
        {"name": "xml", "type": 0, "api": "https://x.example.com/api.php/provide/vod/at/xml/"},
        {"name": "ext-ref", "type": 3, "api": "csp_XBPQ", "ext": "./xbpq/a.json"},
        {"name": "ext-str", "type": 3, "api": "csp_XBPQ", "ext": '{"主页url":"https://s.example.com","数组":"<li&&</li>"}'},
        {"name": "bad", "type": 1, "api": "csp_nothing"},
    ], "lives": [{"name": "live"}]}
    sites, skipped = extract_quanju_sites(cfg)
    assert skipped == {"jar": 1, "script": 1, "remote": 1, "no_url": 1}
    assert [s["name"] for s in sites] == ["xml", "ext-ref", "ext-str"]
    assert sites[1] == {"name": "ext-ref", "extUrl": "./xbpq/a.json"}
    assert sites[2]["rule"]["主页url"] == "https://s.example.com"


def test_base_normalisation_mirrors_custom_source_import_base():
    assert import_base("https://a.example.com/api.php/provide/vod/") == "https://a.example.com/api.php/provide/vod"
    assert import_base("https://a.example.com/api.php/provide/vod/?ac=list") == \
        "https://a.example.com/api.php/provide/vod"                # 去参数，否则注册表拒收
    assert import_base("https://a.example.com/vod/json?x=1") == "https://a.example.com"
    spliced = "https://cfkua.wokaotianshi.eu.org/https://caiji.maotaizy.cc"
    assert import_base(spliced + "/") == spliced                   # Go 测试：拼接地址整体保留
    assert import_base("a.example.com/api.php/provide/vod") == "https://a.example.com/api.php/provide/vod"
    assert not registry_accepts("http://localhost")
    assert not registry_accepts("https://u:p@a.example.com")
    assert not registry_accepts("ftp://a.example.com")


def test_clean_name_rules():
    assert clean_name("💕推荐采集") == "推荐采集"
    assert clean_name("  🔥 天空 影视 [1.2.3] ") == "天空 影视 1.2.3"   # [ ] 先被正则去掉，「.」保留
    assert len(clean_name("长" * 80)) == 40


def test_latency_order_dedup_and_skip_accounting():
    shared = {"name": "公共", "type": 1, "api": "https://shared.example.com/api.php/provide/vod/"}
    slow = _node(f"{H}/slow.json", 2000, {"sites": [shared, {"name": "慢独有", "type": 1,
                                                            "api": "https://slow.example.com/api.php/provide/vod"}]})
    fast = _node(f"{H}/fast.json", 50, {"sites": [dict(shared, name="快·公共"), {"name": "jar", "type": 3, "api": "csp_A"}]})
    multi = RepoNode(id="m", name="多仓", url=f"{H}/m.json", normalized_url="m", kind=RepoKind.MULTI_URLS,
                     available=True, children=[slow, fast])
    dead = RepoNode(id="d", name="死", url=f"{H}/d.json", normalized_url="d", kind=RepoKind.SINGLE, available=False,
                    quanju_sites=[{"name": "x", "api": "https://dead.example.com/api.php/provide/vod"}])
    payload, report = build_quanju_export([multi, dead])
    assert_valid_quanju_payload(payload)
    assert [s["name"] for s in payload["sites"]] == ["快·公共", "慢独有"]   # 更快线路在前，重复保留快的
    assert report.skipped == {"jar": 1, "duplicate": 1}
    assert report.repos_total == 2 and report.repos_used == 2


def test_import_limit_and_storage_budget():
    many = {"sites": [{"name": f"站{i}", "type": 1, "api": f"https://s{i}.example.com/api.php/provide/vod"}
                      for i in range(250)]}
    payload, report = build_quanju_export([_node(f"{H}/m.json", 10, many)])
    assert payload["count"] == 200 and report.skipped["over_limit"] == 50
    big_rule = {"主页url": "https://r.example.com", "数组": "x" * 9000}
    rules = {"sites": [{"name": f"规则{i}", "type": 3, "api": "csp_XBPQ",
                        "ext": dict(big_rule, 主页url=f"https://r{i}.example.com")} for i in range(40)]}
    payload, report = build_quanju_export([_node(f"{H}/r.json", 10, rules)])
    assert_valid_quanju_payload(payload)
    assert 0 < payload["count"] < 40 and report.skipped["over_budget"] == 40 - payload["count"]
    assert len(format_quanju_json(payload).encode()) < 256 * 1024


def test_external_xbpq_rule_is_fetched_relative_to_repo_url():
    cfg = {"sites": [{"name": "外链", "type": 3, "api": "csp_XBPQ", "ext": "./rules/a.json"},
                     {"name": "坏外链", "type": 3, "api": "csp_XBPQ", "ext": "./rules/missing.json"}]}
    asked = []

    def fetch(url):
        asked.append(url)
        if url == f"{H}/cfg/rules/a.json":
            return '// 注释\n{"首页url":"https://ext.example.com/","搜索url":"https://ext.example.com/s/{wd}",}'
        return None

    payload, report = build_quanju_export([_node(f"{H}/cfg/tv.json", 10, cfg)], fetch_text=fetch)
    assert_valid_quanju_payload(payload)
    assert sorted(asked) == [f"{H}/cfg/rules/a.json", f"{H}/cfg/rules/missing.json"]
    assert payload["sites"][0]["url"] == "https://ext.example.com"
    assert report.skipped == {"ext_unresolved": 1}
    _, report2 = build_quanju_export([_node(f"{H}/cfg/tv.json", 10, cfg)], fetch_text=None)
    assert report2.skipped == {"ext_unresolved": 2}


def test_snapshot_roundtrip_and_legacy_snapshot():
    n = _node(f"{H}/a.json", 100, GO_FIXTURE_TVBOX)
    again = RepoNode.from_dict(json.loads(json.dumps(n.to_dict())))
    assert again.quanju_sites == n.quanju_sites and again.quanju_skipped == {"jar": 1}
    legacy = n.to_dict()
    legacy.pop("quanjuSites")
    legacy.pop("quanjuSkipped")
    old = RepoNode.from_dict(legacy)
    assert old.quanju_sites is None
    payload, report = build_quanju_export([old])
    assert payload["count"] == 0 and report.repos_without_detail == 1
    assert "重新检测" in report.summary_text()


def test_demo_scan_end_to_end_export():
    http = MockHttpClient(build_demo_routes(), speed=0.05)
    eng = MultiThreadProbeEngine(ThreadPoolManager(8), http)
    out = eng.execute_full_scan(DEMO_SEEDS, False, False, ProbeSettings(max_concurrency=8), lambda p: None)

    def fetch(u):
        o = http.fetch(u)
        return o.body_bytes.decode() if o.ok else None

    payload, report = build_quanju_export(out.roots, fetch_text=fetch)
    assert_valid_quanju_payload(payload)
    assert report.api_sites >= 5 and report.rule_sites >= 4
    assert report.skipped["jar"] > 0 and report.skipped["duplicate"] >= 1
    assert any(s["url"] == "https://movie.fty-ys.example" for s in payload["sites"])   # 外链规则已抓取
    assert quanju_default_filename().startswith("duanju-custom-sources-")


@pytest.mark.skipif(not os.environ.get("QUANJU_CORE_DIR") or shutil.which("go") is None,
                    reason="需要 QUANJU_CORE_DIR=<guoappxxx>/native 与 Go 工具链")
def test_real_quanju_go_importer_accepts_export(tmp_path):
    core = Path(os.environ["QUANJU_CORE_DIR"]) / "core"
    harness = ROOT / "tools" / "quanju_import_check" / "ysc_import_check_test.go"
    target = core / harness.name
    shutil.copy(harness, target)
    try:
        tricky = {"sites": [{"name": n, "type": 1, "api": f"https://n{i}.example.com/api.php/provide/vod"}
                            for i, n in enumerate(["🔥天空·影视(1.0)", "  A_B-c 站  ", "长" * 60, "★★"])]}
        nodes = [_node(f"{H}/a.json", 100, GO_FIXTURE_TVBOX), _node(f"{H}/c.json", 150, tricky),
                 _node(f"{H}/b.json", 200, {"sites": [{"name": f"站{i}", "type": 1,
                                                       "api": f"https://s{i}.example.com/api.php/provide/vod/?ac=list"}
                                                      for i in range(30)]})]
        payload, _ = build_quanju_export(nodes)
        export_file = tmp_path / "quanju.json"
        export_file.write_text(format_quanju_json(payload), encoding="utf-8")
        result_file = tmp_path / "result.json"
        env = dict(os.environ, YSC_EXPORT_FILE=str(export_file), YSC_RESULT_FILE=str(result_file))
        proc = subprocess.run(["go", "test", "-count=1", "-run", "TestYscHelperExportImport", "./core/"],
                              cwd=core.parent, env=env, capture_output=True, text=True, timeout=600)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        res = json.loads(result_file.read_text(encoding="utf-8"))
        assert res["failed"] == [] and res["skipped"] == []
        assert res["added"] == payload["count"] == res["persisted"] == res["reExportCount"] == 36
        # 名称/网址与 Go 规整结果逐条一致（全剧视界不会再改动我们给出的值）
        assert sorted((r["name"], r["base"]) for r in res["records"]) == \
            sorted((s["name"], s["url"]) for s in payload["sites"])
        assert all(r["ruleOk"] for r in res["records"] if r["hasRule"])
    finally:
        target.unlink(missing_ok=True)
