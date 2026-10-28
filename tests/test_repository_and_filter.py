from ysc_helper.data.repository import MAX_HISTORY_KEEP, RepoScanRepository
from ysc_helper.engine.probe_engine import PipelineOutput, build_exported_config
from ysc_helper.models import (FilterState, ProbeSettings, RepoKind, RepoNode, ScanStatistics, SeedLine,
                               SpeedGrade)
from ysc_helper.tree_filter import compute_filtered_tree


def _tree():
    a = RepoNode("n2", "饭太硬", "https://ghproxy.com/raw.githubusercontent.com/a/b.json", "", RepoKind.SINGLE,
                 True, SpeedGrade.FAST, total_ms=100)
    b = RepoNode("n3", "坏线路", "https://b.com/x", "", RepoKind.SINGLE, False)
    m = RepoNode("n1", "多仓", "https://m.com/m", "", RepoKind.MULTI_URLS, True, children=[a, b])
    return [m]


def test_filter_keeps_parent_of_matching_child():
    f = compute_filtered_tree(_tree(), FilterState(kind=RepoKind.SINGLE, available_only=True))
    assert f.matched_node_count == 1
    assert f.visible_roots[0].id == "n1" and [c.id for c in f.visible_roots[0].children] == ["n2"]
    assert "n1" in f.auto_expanded_ids


def test_filter_keyword():
    f = compute_filtered_tree(_tree(), FilterState(keyword="B.COM"))
    assert [c.id for c in f.visible_roots[0].children] == ["n3"]


def test_export_cleans_proxy():
    cfg = build_exported_config(_tree())
    assert cfg.to_dict() == {"urls": [{"name": "饭太硬", "url": "https://raw.githubusercontent.com/a/b.json"}]}


def test_repository_roundtrip_and_pruning(tmp_path):
    repo = RepoScanRepository(tmp_path)
    roots = _tree()
    cfg = build_exported_config(roots)
    out = PipelineOutput(roots, cfg, ScanStatistics.compute(roots, len(cfg.urls)), 1234)
    for _ in range(MAX_HISTORY_KEEP + 3):
        repo.save_scan_output(out)
    hist = repo.list_history()
    assert len(hist) == MAX_HISTORY_KEEP
    assert len(list((tmp_path / "snapshots").glob("*.json.gz"))) == MAX_HISTORY_KEEP
    _, snap = repo.load_latest_snapshot()
    assert snap.roots[0].children[0].name == "饭太硬"
    assert snap.statistics.exportable_unique_count == 1
    repo.delete_history_by_id(hist[0].id)
    assert len(repo.list_history()) == MAX_HISTORY_KEEP - 1

    repo.update_settings(ProbeSettings(max_concurrency=99, speed_samples=2, https_only=True))
    s = repo.load_settings()
    assert (s.max_concurrency, s.speed_samples, s.https_only) == (32, 2, True)
    repo.add_custom_seed(SeedLine("x", "https://x.com/a"))
    repo.add_custom_seed(SeedLine("x2", "HTTPS://x.com/a"))
    assert [s.name for s in repo.load_custom_seeds()] == ["x2"]
    repo.remove_custom_seed("HTTPS://x.com/a")
    assert repo.load_custom_seeds() == []
    repo.close()
