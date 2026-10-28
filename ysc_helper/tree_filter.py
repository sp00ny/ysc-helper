"""正确的父子树形过滤（对应 MainViewModel.computeFilteredTree）。

子节点命中时自动保留并展开父多仓容器，修复原版「筛选单线路导致多仓内匹配项全部消失」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set

from .models import FilterState, RepoNode


@dataclass
class FilteredTreeData:
    visible_roots: List[RepoNode] = field(default_factory=list)
    matched_node_count: int = 0
    auto_expanded_ids: Set[str] = field(default_factory=set)


def compute_filtered_tree(roots: List[RepoNode], f: FilterState) -> FilteredTreeData:
    q = f.keyword.strip().lower()
    has_filter = bool(q) or f.kind is not None or f.available_only is not None or f.speed_grade is not None
    if not has_filter:
        cnt = 0
        stack = list(roots)
        while stack:
            n = stack.pop()
            cnt += 1
            stack.extend(n.children)
        return FilteredTreeData(list(roots), cnt)

    matched = [0]
    auto: Set[str] = set()

    def matches(n: RepoNode) -> bool:
        if q and q not in f"{n.name} {n.url}".lower():
            return False
        if f.kind is not None and n.kind != f.kind:
            return False
        if f.available_only is not None and n.available != f.available_only:
            return False
        if f.speed_grade is not None and n.speed_grade != f.speed_grade:
            return False
        return True

    def filt(n: RepoNode) -> Optional[RepoNode]:
        kids = [k for k in (filt(c) for c in n.children) if k is not None]
        ok = matches(n)
        if ok:
            matched[0] += 1
        if kids:
            auto.add(n.id)
            return n.copy_with(children=kids)
        if ok:
            return n.copy_with(children=[])
        return None

    visible = [v for v in (filt(r) for r in roots) if v is not None]
    return FilteredTreeData(visible, matched[0], auto)
