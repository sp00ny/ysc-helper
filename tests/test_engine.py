"""探测引擎：Single-Flight、断环、空多仓、导出排序、取消与小并发深嵌套无死锁。"""
import json
import threading
import time


from ysc_helper.demo import DEMO_SEEDS, MockHttpClient, build_demo_routes
from ysc_helper.engine.probe_engine import MultiThreadProbeEngine, ScanCancelled
from ysc_helper.engine.thread_pool_manager import ThreadPoolManager
from ysc_helper.models import ProbeSettings, RepoKind, SeedLine

H = "https://t.example"


def _routes(d):
    return {f"{H}{k}": (200, json.dumps(v).encode(), 5) for k, v in d.items()}


def _single(n=1):
    return {"sites": [{"key": str(i)} for i in range(n)], "spider": "https://t.example/a.jar"}


def run(routes, seeds, conc=4, speed=1.0, **kw):
    http = MockHttpClient(routes, speed=speed)
    eng = MultiThreadProbeEngine(ThreadPoolManager(conc), http)
    out = eng.execute_full_scan(seeds, False, False, ProbeSettings(max_concurrency=conc, **kw), lambda p: None)
    return out, eng, http


def test_single_flight_shares_results_and_requests_once():
    routes = _routes({
        "/m1": {"urls": [{"name": "A", "url": f"{H}/a"}]},
        "/m2": {"urls": [{"name": "A2", "url": f"{H}/a"}]},
        "/a": _single(3),
    })
    calls = []
    http = MockHttpClient(routes)
    orig = http.fetch
    http.fetch = lambda u, **k: (calls.append(u), orig(u, **k))[1]
    eng = MultiThreadProbeEngine(ThreadPoolManager(4), http)
    out = eng.execute_full_scan([SeedLine("m1", f"{H}/m1"), SeedLine("m2", f"{H}/m2")], False, False,
                                ProbeSettings(max_concurrency=4), lambda p: None)
    assert calls.count(f"{H}/a") == 1
    assert all(r.available for r in out.roots)
    assert eng.current_single_flight_hits() == 1
    assert len(out.exported_config.urls) == 1


def test_loop_is_broken_and_empty_multi_unavailable():
    routes = _routes({
        "/loop": {"urls": [{"name": "self", "url": f"{H}/loop"}]},
        "/dead": {"urls": [{"name": "x", "url": f"{H}/missing"}]},
    })
    out, _, _ = run(routes, [SeedLine("loop", f"{H}/loop"), SeedLine("dead", f"{H}/dead")])
    loop, dead = out.roots
    assert not loop.available and "循环引用" in loop.children[0].error
    assert not dead.available and dead.error == "多仓内无可用子线路"
    assert out.exported_config.urls == []


def test_export_sorted_by_latency_and_proxy_cleaned():
    routes = {
        f"{H}/slow": (200, json.dumps(_single()).encode(), 80),
        f"{H}/fast": (200, json.dumps(_single()).encode(), 5),
    }
    out, _, _ = run(routes, [SeedLine("slow", f"{H}/slow"), SeedLine("fast", f"{H}/fast")])
    assert [u.name for u in out.exported_config.urls] == ["fast", "slow"]


def test_deep_nesting_with_min_concurrency_does_not_deadlock():
    d = {}
    # 宽 6 × 深 3 的多仓树，并发仅 4
    for i in range(6):
        d[f"/l0_{i}"] = {"urls": [{"name": f"l1_{i}_{j}", "url": f"{H}/l1_{i}_{j}"} for j in range(6)]}
        for j in range(6):
            d[f"/l1_{i}_{j}"] = {"storeHouse": [{"sourceName": "s", "sourceUrl": f"{H}/l2_{i}_{j}_{k}"}
                                               for k in range(4)]}
            for k in range(4):
                d[f"/l2_{i}_{j}_{k}"] = _single()
    seeds = [SeedLine(f"r{i}", f"{H}/l0_{i}") for i in range(6)]
    t0 = time.time()
    out, _, _ = run(_routes(d), seeds, conc=4, speed=0.1)
    assert time.time() - t0 < 30
    assert out.statistics.total_nodes_scanned == 6 + 36 + 144
    assert out.statistics.exportable_unique_count == 144
    assert all(r.kind == RepoKind.MULTI_URLS for r in out.roots)


def test_max_depth_enforced():
    d = {f"/d{i}": {"urls": [{"name": f"d{i+1}", "url": f"{H}/d{i+1}"}]} for i in range(6)}
    out, _, _ = run(_routes(d), [SeedLine("d0", f"{H}/d0")], max_depth=2)
    n = out.roots[0]
    while n.children:
        n = n.children[0]
    assert "超过最大嵌套深度" in n.error


def test_cancellation_returns_promptly():
    routes = {f"{H}/s{i}": (200, json.dumps(_single()).encode(), 3000) for i in range(40)}
    http = MockHttpClient(routes)
    eng = MultiThreadProbeEngine(ThreadPoolManager(4), http)
    err = []

    def go():
        try:
            eng.execute_full_scan([SeedLine(str(i), f"{H}/s{i}") for i in range(40)], False, False,
                                  ProbeSettings(max_concurrency=4), lambda p: None)
        except ScanCancelled:
            err.append("cancelled")

    th = threading.Thread(target=go)
    th.start()
    time.sleep(0.3)
    t0 = time.time()
    eng.cancel()
    th.join(5)
    assert not th.is_alive() and err == ["cancelled"]
    assert time.time() - t0 < 2


def test_demo_dataset_end_to_end():
    out, eng, _ = run(build_demo_routes(), DEMO_SEEDS, conc=8, speed=0.05)
    names = {u.name for u in out.exported_config.urls}
    assert {"饭太硬", "肥猫", "多多聚合", "摸鱼儿隐写", "蚂蚁 AES", "慢速线路"} <= names
    assert out.statistics.available_multis >= 2
    assert eng.current_single_flight_hits() >= 1


def test_https_only_filters_http_seeds():
    routes = {"http://h.example/a": (200, json.dumps(_single()).encode(), 1)}
    out, _, _ = run(routes, [SeedLine("h", "http://h.example/a")], https_only=True)
    assert out.roots == []
