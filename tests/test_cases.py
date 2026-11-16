"""手算核对测试：每个用例的预期坐标都来自手工折线推导与鞋带公式。

覆盖：反向描述且折点不对齐的直边；一侧长边对应另一侧多段边；
三块地共点；容差内窄偏移修正且面积可复算；超容差窄缝隔离；
同优先级不随输入顺序选边；未声明相邻的平行边不合并；
洞与未声明外边界不动；超面积上限/无效几何的组不进入已修复层；
结构错误整次失败；重跑与乱序确定性；拓扑不变量独立确认。
"""

from __future__ import annotations

import json
import os

from helpers import (approx, artifact_bytes, check_ring_basics,
                     check_shared_edge_topology, conflicts_by_id, edges_by_id,
                     find_chain_span, load_artifact, parcels_by_id, polygon_area,
                     ring_approx_eq, run_cli, scenario, write_json)

TOL = 0.05
MAX_AREA = 0.5


def _srcs(*pairs):
    return [{"id": s, "priority": p} for s, p in pairs]


def _parcel(pid, src, exterior, holes=None, attrs=None):
    return {"id": pid, "source": src, "attributes": attrs or {},
            "exterior": exterior, "holes": holes or []}


def _adj(bid, a, b):
    return {"boundary": bid, "parcels": [a, b]}


def _input(sources, parcels, adjacencies):
    return {"crs": "LOCAL", "sources": sources,
            "parcels": parcels, "adjacencies": adjacencies}


# ---------------------------------------------------------------- 用例 1
# 两块地反向描述同一条直边，且一侧折点不对齐（多一个中间折点）。
def test_case1_reversed_misaligned_straight_edge(tmp):
    store = os.path.join(tmp, "s1")
    p1 = _parcel("P1", "SA", [[0, 0], [10, 0], [10, 4], [0, 4]])
    p2 = _parcel("P2", "SB", [[14, 0], [14, 4], [10, 4], [10, 2], [10, 0]])
    files = {"in.json": _input(_srcs(("SA", 10), ("SB", 5)), [p1, p2],
                               [_adj("B1", "P1", "P2")])}
    r = scenario(store, files, TOL, MAX_AREA)
    assert r == {"import": 0, "check": 0, "publish": 0}, r

    parcels = parcels_by_id(load_artifact(store, "parcels.json"))
    # 高优先级侧（SA）提供基准形状：P1 不动；P2 的多余折点 (10,2) 被移除
    assert ring_approx_eq(parcels["P1"]["exterior"],
                          [[0, 0], [10, 0], [10, 4], [0, 4]])
    assert ring_approx_eq(parcels["P2"]["exterior"],
                          [[14, 0], [14, 4], [10, 4], [10, 0]])
    edges = edges_by_id(load_artifact(store, "shared_boundaries.json"))
    e = edges["B1"]
    assert e["parcels"] == ["P1", "P2"]
    assert ring_approx_eq(e["coordinates"], [[10, 0], [10, 4]])
    assert e["direction"] == {"from": [10.0, 0.0], "to": [10.0, 4.0]}
    assert e["shape_source"] == "SA"
    assert e["parcel_orientations"] == {"P1": "forward", "P2": "backward"}
    assert approx(e["length"], 4.0)
    # 面积不变（手算：P1=40，P2=16）
    prov = {p["parcel_id"]: p for p in load_artifact(store, "provenance.json")}
    assert approx(prov["P1"]["original_area"], 40.0)
    assert approx(prov["P2"]["original_area"], 16.0)
    assert approx(prov["P1"]["area_change"], 0.0)
    assert approx(prov["P2"]["area_change"], 0.0)
    assert load_artifact(store, "conflicts.json") == []


# ---------------------------------------------------------------- 用例 2
# 一侧一个长线段对应另一侧多个短线段（折点数量不同不能漏掉后半段）。
def test_case2_long_edge_vs_multi_segment(tmp):
    store = os.path.join(tmp, "s2")
    p1 = _parcel("P1", "SA", [[0, 0], [4, 0], [4, 2], [0, 2]])
    # 低优先级侧底边由 4 个短段组成，带 ±0.01 的抖动（CCW 输入）
    p2 = _parcel("P2", "SB", [[0, 0], [0, -2], [4, -2], [4, 0],
                              [3, 0.01], [2, -0.01], [1, 0.01]])
    files = {"in.json": _input(_srcs(("SA", 10), ("SB", 20)), [p1, p2],
                               [_adj("B1", "P1", "P2")])}
    r = scenario(store, files, TOL, MAX_AREA)
    assert r == {"import": 0, "check": 0, "publish": 0}, r

    parcels = parcels_by_id(load_artifact(store, "parcels.json"))
    # 基准形状来自 SB：P1 的长边被替换为 5 点链
    assert ring_approx_eq(parcels["P1"]["exterior"],
                          [[0, 0], [1, 0.01], [2, -0.01], [3, 0.01], [4, 0],
                           [4, 2], [0, 2]])
    assert ring_approx_eq(parcels["P2"]["exterior"],
                          [[4, 0], [3, 0.01], [2, -0.01], [1, 0.01], [0, 0],
                           [0, -2], [4, -2]])
    edges = edges_by_id(load_artifact(store, "shared_boundaries.json"))
    assert ring_approx_eq(edges["B1"]["coordinates"],
                          [[0, 0], [1, 0.01], [2, -0.01], [3, 0.01], [4, 0]])
    # 手算面积：P1 原为 8，替换后 7.99（鞋带公式），变化 0.01
    prov = {p["parcel_id"]: p for p in load_artifact(store, "provenance.json")}
    assert approx(prov["P1"]["original_area"], 8.0)
    assert approx(prov["P1"]["final_area"], 7.99)
    assert approx(prov["P1"]["area_change"], 0.01)
    assert approx(prov["P2"]["original_area"], 8.01)
    assert approx(prov["P2"]["area_change"], 0.0)
    # 独立复算发布后的面积
    assert approx(polygon_area(parcels["P1"]["exterior"]), 7.99)
    assert approx(polygon_area(parcels["P2"]["exterior"]), 8.01)


# ---------------------------------------------------------------- 用例 3
# 三块地在同一点汇合：两条共享边各自整理后，共点坐标一致。
def test_case3_three_parcels_meet_at_point(tmp):
    store = os.path.join(tmp, "s3")
    a = _parcel("A", "SA", [[0, 0], [1, 0], [1, 1], [0, 1]])
    b = _parcel("B", "SB", [[0.002, 0], [0.002, 1], [-1, 1], [-1, 0]])
    c = _parcel("C", "SC", [[0, 0.003], [0, -1], [1, -1], [1, 0.003]])
    files = {"in.json": _input(_srcs(("SA", 30), ("SB", 20), ("SC", 10)),
                               [a, b, c],
                               [_adj("B1", "A", "B"), _adj("B2", "A", "C")])}
    r = scenario(store, files, TOL, MAX_AREA)
    assert r == {"import": 0, "check": 0, "publish": 0}, r

    parcels = parcels_by_id(load_artifact(store, "parcels.json"))
    assert ring_approx_eq(parcels["A"]["exterior"],
                          [[0, 1], [0, 0], [1, 0], [1, 1]])
    assert ring_approx_eq(parcels["B"]["exterior"],
                          [[0, 0], [0, 1], [-1, 1], [-1, 0]])
    assert ring_approx_eq(parcels["C"]["exterior"],
                          [[1, 0], [0, 0], [0, -1], [1, -1]])
    # 三块地外环都精确经过共点 (0,0)
    for pid in ("A", "B", "C"):
        assert [0.0, 0.0] in parcels[pid]["exterior"], pid
    edges = edges_by_id(load_artifact(store, "shared_boundaries.json"))
    assert ring_approx_eq(edges["B1"]["coordinates"], [[0, 0], [0, 1]])
    assert ring_approx_eq(edges["B2"]["coordinates"], [[0, 0], [1, 0]])
    assert edges["B1"]["parcel_orientations"] == {"A": "backward", "B": "forward"}
    assert edges["B2"]["parcel_orientations"] == {"A": "forward", "C": "backward"}
    # 手算面积：B 1.002->1.0，C 1.003->1.0，A 不变
    prov = {p["parcel_id"]: p for p in load_artifact(store, "provenance.json")}
    assert approx(prov["A"]["area_change"], 0.0)
    assert approx(prov["B"]["original_area"], 1.002)
    assert approx(prov["B"]["area_change"], 0.002)
    assert approx(prov["C"]["original_area"], 1.003)
    assert approx(prov["C"]["area_change"], 0.003)


# ---------------------------------------------------------------- 用例 4
# 容差内窄偏移被修正，面积变化可用鞋带公式复算。
def test_case4_narrow_offset_corrected_area_recomputable(tmp):
    store = os.path.join(tmp, "s4")
    p1 = _parcel("P1", "SA", [[0, 0], [2, 0], [2, 2], [0, 2]])
    p2 = _parcel("P2", "SB", [[2, 0], [4, 0], [4, 2], [2, 2], [2.04, 1]])
    files = {"in.json": _input(_srcs(("SA", 10), ("SB", 5)), [p1, p2],
                               [_adj("B1", "P1", "P2")])}
    r = scenario(store, files, TOL, MAX_AREA)
    assert r == {"import": 0, "check": 0, "publish": 0}, r

    parcels = parcels_by_id(load_artifact(store, "parcels.json"))
    assert ring_approx_eq(parcels["P2"]["exterior"],
                          [[2, 2], [2, 0], [4, 0], [4, 2]])
    # 手算：P2 原面积 3.96（4 - 三角形 0.5*2*0.04），修复后 4.0，变化 0.04
    prov = {p["parcel_id"]: p for p in load_artifact(store, "provenance.json")}
    assert approx(prov["P2"]["original_area"], 3.96)
    assert approx(prov["P2"]["final_area"], 4.0)
    assert approx(prov["P2"]["area_change"], 0.04)
    # 独立复算：用测试自己的鞋带公式验证发布面积与来源说明一致
    assert approx(polygon_area(parcels["P2"]["exterior"]), 4.0)
    assert approx(polygon_area(parcels["P1"]["exterior"]), 4.0)


# ---------------------------------------------------------------- 用例 5
# 超容差窄缝：保留原状并隔离，冲突原因可定位。
def test_case5_beyond_tolerance_gap_isolated(tmp):
    store = os.path.join(tmp, "s5")
    p1 = _parcel("P1", "SA", [[0, 0], [2, 0], [2, 2], [0, 2]])
    p2 = _parcel("P2", "SB", [[2.2, 0], [4, 0], [4, 2], [2.2, 2]])
    files = {"in.json": _input(_srcs(("SA", 10), ("SB", 5)), [p1, p2],
                               [_adj("B1", "P1", "P2")])}
    r = scenario(store, files, TOL, MAX_AREA)
    assert r == {"import": 0, "check": 0, "publish": 0}, r

    parcels = parcels_by_id(load_artifact(store, "parcels.json"))
    # 原始地块原样保留
    assert ring_approx_eq(parcels["P1"]["exterior"],
                          [[0, 0], [2, 0], [2, 2], [0, 2]])
    assert ring_approx_eq(parcels["P2"]["exterior"],
                          [[2.2, 0], [4, 0], [4, 2], [2.2, 2]])
    assert load_artifact(store, "shared_boundaries.json") == []
    conflicts = conflicts_by_id(load_artifact(store, "conflicts.json"))
    assert conflicts["B1"][0]["code"] == "NO_SHARED_GEOMETRY"
    assert conflicts["B1"][0]["parcels"] == ["P1", "P2"]
    assert conflicts["B1"][0]["details"]["tolerance"] == TOL


# ---------------------------------------------------------------- 用例 6
# 同优先级且两侧坐标不一致：按内容确定性合并，不随输入顺序挑边。
def _case6_parcels():
    p1 = _parcel("P1", "SA", [[0, 0], [2, 0], [2, 1], [0, 1]])
    p2 = _parcel("P2", "SD", [[0, 0.02], [0, -1], [2, -1], [2, 0.02], [1, 0.04]])
    return p1, p2


def test_case6_equal_priority_not_input_order(tmp):
    p1, p2 = _case6_parcels()
    # 两种输入顺序：地块记录、相邻记录、来源记录全部颠倒
    files_a = {"in.json": _input(_srcs(("SA", 10), ("SD", 10)), [p1, p2],
                                 [_adj("B1", "P1", "P2")])}
    files_b = {"in.json": _input(_srcs(("SD", 10), ("SA", 10)), [p2, p1],
                                 [_adj("B1", "P2", "P1")])}
    ra = scenario(os.path.join(tmp, "s6a"), files_a, TOL, MAX_AREA)
    rb = scenario(os.path.join(tmp, "s6b"), files_b, TOL, MAX_AREA)
    assert ra == rb == {"import": 0, "check": 0, "publish": 0}

    # 手算合并结果：参数并集 {0, 0.5, 1} 上逐点取中点
    merged = [[0, 0.01], [1, 0.02], [2, 0.01]]
    for s in ("s6a", "s6b"):
        store = os.path.join(tmp, s)
        parcels = parcels_by_id(load_artifact(store, "parcels.json"))
        assert ring_approx_eq(parcels["P1"]["exterior"],
                              merged + [[2, 1], [0, 1]]), s
        assert ring_approx_eq(parcels["P2"]["exterior"],
                              [[2, 0.01], [1, 0.02], [0, 0.01], [0, -1], [2, -1]]), s
        edges = edges_by_id(load_artifact(store, "shared_boundaries.json"))
        assert ring_approx_eq(edges["B1"]["coordinates"], merged), s
        assert edges["B1"]["shape_source"] == "MERGED_EQUAL_PRIORITY"
        # 手算面积：P1 2.0->1.97，P2 2.06->2.03，各变化 0.03
        prov = {p["parcel_id"]: p for p in load_artifact(store, "provenance.json")}
        assert approx(prov["P1"]["area_change"], 0.03)
        assert approx(prov["P2"]["area_change"], 0.03)
    # 两种输入顺序的发布物逐字节一致
    for name in ("parcels.json", "shared_boundaries.json", "conflicts.json",
                 "provenance.json", "manifest.json"):
        assert artifact_bytes(os.path.join(tmp, "s6a"), name) == \
               artifact_bytes(os.path.join(tmp, "s6b"), name), name


# ---------------------------------------------------------------- 用例 7
# 附近平行但未声明相邻的边：即使距离小于容差也不合并。
def test_case7_undeclared_parallel_edges_untouched(tmp):
    store = os.path.join(tmp, "s7")
    p1 = _parcel("P1", "SA", [[0, 0], [2, 0], [2, 2], [0, 2]])
    p2 = _parcel("P2", "SB", [[2.03, 0], [4.03, 0], [4.03, 2], [2.03, 2]])
    files = {"in.json": _input(_srcs(("SA", 10), ("SB", 5)), [p1, p2], [])}
    r = scenario(store, files, TOL, MAX_AREA)
    assert r == {"import": 0, "check": 0, "publish": 0}, r
    parcels = parcels_by_id(load_artifact(store, "parcels.json"))
    assert ring_approx_eq(parcels["P1"]["exterior"], p1["exterior"])
    assert ring_approx_eq(parcels["P2"]["exterior"], p2["exterior"])
    assert load_artifact(store, "shared_boundaries.json") == []
    assert load_artifact(store, "conflicts.json") == []


# ---------------------------------------------------------------- 用例 8
# 地块原有洞和未声明的外边界保持不动；只改声明的共享部分。
def test_case8_holes_and_outer_boundary_untouched(tmp):
    store = os.path.join(tmp, "s8")
    hole = [[2, 2], [2, 4], [4, 4], [4, 2]]  # 顺时针
    p1 = _parcel("P1", "SA", [[0, 0], [6, 0], [6, 6], [0, 6]], holes=[hole],
                 attrs={"lot": "A-17"})
    p2 = _parcel("P2", "SB", [[6.02, 0], [9, 0], [9, 6], [6.02, 6]])
    files = {"in.json": _input(_srcs(("SA", 10), ("SB", 5)), [p1, p2],
                               [_adj("B1", "P1", "P2")])}
    r = scenario(store, files, TOL, 1.0)
    assert r == {"import": 0, "check": 0, "publish": 0}, r

    parcels = parcels_by_id(load_artifact(store, "parcels.json"))
    # P1 外环与洞逐点不动，属性保留
    assert ring_approx_eq(parcels["P1"]["exterior"], p1["exterior"])
    assert ring_approx_eq(parcels["P1"]["holes"][0], hole)
    assert parcels["P1"]["attributes"] == {"lot": "A-17"}
    assert parcels["P1"]["source"] == "SA"
    # P2 的共享边被校到 x=6（手算：面积 17.88 -> 18.0，变化 0.12）
    assert ring_approx_eq(parcels["P2"]["exterior"],
                          [[6, 6], [6, 0], [9, 0], [9, 6]])
    prov = {p["parcel_id"]: p for p in load_artifact(store, "provenance.json")}
    assert approx(prov["P2"]["original_area"], 17.88)
    assert approx(prov["P2"]["area_change"], 0.12)
    assert approx(prov["P1"]["original_area"], 32.0)  # 36 - 洞 4
    assert approx(prov["P1"]["area_change"], 0.0)
    edges = edges_by_id(load_artifact(store, "shared_boundaries.json"))
    assert ring_approx_eq(edges["B1"]["coordinates"], [[6, 0], [6, 6]])


# ---------------------------------------------------------------- 用例 9
# 面积变化超上限的组不进入已修复层。
def test_case9a_area_limit_exceeded_excluded(tmp):
    store = os.path.join(tmp, "s9a")
    p1 = _parcel("P1", "SA", [[0, 0], [4, 0], [4, 1], [0, 1]])
    p2 = _parcel("P2", "SB", [[0, 0], [0, -1], [4, -1], [4, 0], [2, 0.3]])
    files = {"in.json": _input(_srcs(("SA", 5), ("SB", 10)), [p1, p2],
                               [_adj("B1", "P1", "P2")])}
    r = scenario(store, files, 0.5, 0.5)
    assert r == {"import": 0, "check": 0, "publish": 0}, r
    parcels = parcels_by_id(load_artifact(store, "parcels.json"))
    # 手算：若接受，P1 面积 4.0 -> 3.4，变化 0.6 > 0.5，整组拒绝
    assert ring_approx_eq(parcels["P1"]["exterior"], p1["exterior"])
    assert ring_approx_eq(parcels["P2"]["exterior"], p2["exterior"])
    assert load_artifact(store, "shared_boundaries.json") == []
    conflicts = conflicts_by_id(load_artifact(store, "conflicts.json"))
    c = conflicts["B1"][0]
    assert c["code"] == "AREA_CHANGE_EXCEEDED"
    assert approx(c["details"]["area_changes"]["P1"], 0.6)
    assert approx(c["details"]["max_area_change"], 0.5)


# 整理后多边形无效的组不进入已修复层。
def test_case9b_invalid_result_excluded(tmp):
    store = os.path.join(tmp, "s9b")
    # A 的洞底边 y=0.3；B 的共享链鼓包高 0.5（容差 0.5 内）。
    # 若接受，A 外环鼓包边 (0,0)->(2,0.5) 在 x=1.2 处穿过洞底边 -> 多边形无效。
    hole = [[1, 0.3], [1, 1], [3, 1], [3, 0.3]]  # 顺时针
    a = _parcel("A", "SA", [[0, 0], [4, 0], [4, 2], [0, 2]], holes=[hole])
    b = _parcel("B", "SB", [[0, 0], [0, -1], [4, -1], [4, 0], [2, 0.5]])
    files = {"in.json": _input(_srcs(("SA", 5), ("SB", 10)), [a, b],
                               [_adj("B1", "A", "B")])}
    r = scenario(store, files, 0.5, 10.0)
    assert r == {"import": 0, "check": 0, "publish": 0}, r
    parcels = parcels_by_id(load_artifact(store, "parcels.json"))
    assert ring_approx_eq(parcels["A"]["exterior"], a["exterior"])
    assert ring_approx_eq(parcels["A"]["holes"][0], hole)
    assert ring_approx_eq(parcels["B"]["exterior"], b["exterior"])
    assert load_artifact(store, "shared_boundaries.json") == []
    conflicts = conflicts_by_id(load_artifact(store, "conflicts.json"))
    assert conflicts["B1"][0]["code"] == "INVALID_RESULT"


# ---------------------------------------------------------------- 用例 10
# 清单引用不存在的地块 / 自相邻：冲突隔离，其余组照常处理。
def test_case10_unknown_parcel_and_self_adjacency(tmp):
    store = os.path.join(tmp, "s10")
    p1 = _parcel("P1", "SA", [[0, 0], [10, 0], [10, 4], [0, 4]])
    p2 = _parcel("P2", "SB", [[14, 0], [14, 4], [10, 4], [10, 2], [10, 0]])
    files = {"in.json": _input(_srcs(("SA", 10), ("SB", 5)), [p1, p2],
                               [_adj("B1", "P1", "P2"),
                                _adj("B9", "P2", "P999"),
                                _adj("B10", "P1", "P1")])}
    r = scenario(store, files, TOL, MAX_AREA)
    assert r == {"import": 0, "check": 0, "publish": 0}, r
    edges = edges_by_id(load_artifact(store, "shared_boundaries.json"))
    assert set(edges) == {"B1"}  # 合格组照常修复
    conflicts = conflicts_by_id(load_artifact(store, "conflicts.json"))
    assert conflicts["B9"][0]["code"] == "UNKNOWN_PARCEL"
    assert conflicts["B9"][0]["details"]["missing"] == ["P999"]
    assert conflicts["B10"][0]["code"] == "SELF_ADJACENCY"


# ---------------------------------------------------------------- 用例 11
# 一个边界标识只允许指向一对不同地块。
def test_case11_boundary_id_multiple_pairs(tmp):
    store = os.path.join(tmp, "s11")
    p1 = _parcel("P1", "SA", [[0, 0], [2, 0], [2, 2], [0, 2]])
    p2 = _parcel("P2", "SA", [[2, 0], [4, 0], [4, 2], [2, 2]])
    p3 = _parcel("P3", "SB", [[4, 0], [6, 0], [6, 2], [4, 2]])
    files = {"in.json": _input(_srcs(("SA", 10), ("SB", 20)), [p1, p2, p3],
                               [_adj("B1", "P1", "P2"), _adj("B1", "P2", "P3"),
                                _adj("B2", "P2", "P3")])}
    r = scenario(store, files, TOL, MAX_AREA)
    assert r == {"import": 0, "check": 0, "publish": 0}, r
    conflicts = conflicts_by_id(load_artifact(store, "conflicts.json"))
    assert conflicts["B1"][0]["code"] == "BOUNDARY_ID_MULTIPLE_PAIRS"
    assert conflicts["B1"][0]["parcels"] == ["P1", "P2", "P3"]
    edges = edges_by_id(load_artifact(store, "shared_boundaries.json"))
    assert set(edges) == {"B2"}
    assert ring_approx_eq(edges["B2"]["coordinates"], [[4, 0], [4, 2]])
    parcels = parcels_by_id(load_artifact(store, "parcels.json"))
    assert ring_approx_eq(parcels["P1"]["exterior"], p1["exterior"])


# ---------------------------------------------------------------- 用例 12
# 输入结构错误：整次运行失败，不生成看似完整的正式输出；旧版本保持可读。
def test_case12_structural_error_fails_run(tmp):
    store = os.path.join(tmp, "s12")
    good = {"in.json": _input(_srcs(("SA", 10), ("SB", 5)),
                              [_parcel("P1", "SA", [[0, 0], [2, 0], [2, 2], [0, 2]]),
                               _parcel("P2", "SB", [[2, 0], [4, 0], [4, 2], [2, 2]])],
                              [_adj("B1", "P1", "P2")])}
    r = scenario(store, good, TOL, MAX_AREA)
    assert r == {"import": 0, "check": 0, "publish": 0}, r

    # (a) 自相交蝴蝶结多边形
    bad_dir = os.path.join(tmp, "bad1")
    os.makedirs(bad_dir)
    write_json(os.path.join(bad_dir, "bad.json"),
               _input(_srcs(("SA", 10)),
                      [_parcel("PX", "SA", [[0, 0], [2, 2], [2, 0], [0, 2]])], []))
    code, out = run_cli(["import", "--store", store,
                         os.path.join(bad_dir, "bad.json")])
    assert code == 2 and "外环无效" in out, (code, out)

    # (b) NaN 坐标（非有限十进制数）
    bad2 = os.path.join(tmp, "bad2.json")
    with open(bad2, "w") as f:
        f.write('{"sources":[{"id":"SA","priority":1}],'
                '"parcels":[{"id":"PX","source":"SA",'
                '"exterior":[[0,0],[NaN,0],[1,1]]}],"adjacencies":[]}')
    code, out = run_cli(["import", "--store", store, bad2])
    assert code == 2 and "非法数值" in out, (code, out)

    # (c) 缺少 exterior 字段
    bad3 = os.path.join(tmp, "bad3.json")
    write_json(bad3, {"sources": [{"id": "SA", "priority": 1}],
                      "parcels": [{"id": "PX", "source": "SA"}]})
    code, out = run_cli(["import", "--store", store, bad3])
    assert code == 2 and "exterior" in out, (code, out)

    # (d) 相邻记录重复声明
    bad4 = os.path.join(tmp, "bad4.json")
    write_json(bad4, _input(_srcs(("SA", 10)),
                            [_parcel("P1", "SA", [[0, 0], [1, 0], [1, 1], [0, 1]]),
                             _parcel("P2", "SA", [[1, 0], [2, 0], [2, 1], [1, 1]])],
                            [_adj("B1", "P1", "P2"), _adj("B1", "P2", "P1")]))
    code, out = run_cli(["import", "--store", store, bad4])
    assert code == 2 and "重复声明" in out, (code, out)

    # 失败的导入不破坏已发布版本：v1 仍可读
    code, out = run_cli(["query", "--store", store, "parcel", "P1"])
    assert code == 0, (code, out)
    got = json.loads(out)
    assert got["parcel"]["id"] == "P1"
    assert got["provenance"]["source"] == "SA"
    # 没有产生新版本
    assert len(os.listdir(os.path.join(store, "versions"))) == 1


# ---------------------------------------------------------------- 用例 13
# 相同输入重跑、打乱文件与记录顺序：发布物与冲突清单逐项一致。
def _combined_files_layout_a():
    sources = _srcs(("SA", 10), ("SB", 20), ("SD", 10))
    parcels = [
        _parcel("PA1", "SA", [[0, 0], [2, 0], [2, 2], [0, 2]]),
        _parcel("PA2", "SB", [[2, 0], [4, 0], [4, 2], [2, 2], [2.02, 1]]),
        _parcel("PB1", "SA", [[10, 0], [12, 0], [12, 2], [10, 2]]),
        _parcel("PB2", "SB", [[12.2, 0], [14, 0], [14, 2], [12.2, 2]]),
        _parcel("PC1", "SA", [[20, 0], [22, 0], [22, 1], [20, 1]]),
        _parcel("PC2", "SD", [[20, 0.02], [20, -1], [22, -1], [22, 0.02],
                                [21, 0.04]]),
    ]
    adj = [_adj("BA1", "PA1", "PA2"), _adj("BB1", "PB1", "PB2"),
           _adj("BC1", "PC1", "PC2"), _adj("BD1", "PA1", "PX"),
           _adj("BE1", "PB1", "PB2"), _adj("BE1", "PA1", "PA2")]
    return {"all.json": _input(sources, parcels, adj)}


def _combined_files_layout_b():
    a = _combined_files_layout_a()["all.json"]
    # 同样的内容拆成三个文件，记录顺序全部颠倒
    f1 = {"crs": "LOCAL", "sources": list(reversed(a["sources"])),
          "parcels": list(reversed(a["parcels"][:3]))}
    f2 = {"parcels": list(reversed(a["parcels"][3:])),
          "adjacencies": list(reversed(a["adjacencies"][:3]))}
    f3 = {"adjacencies": list(reversed(a["adjacencies"][3:]))}
    return {"part1.json": f1, "part2.json": f2, "part3.json": f3}


def test_case13_shuffle_and_rerun_determinism(tmp):
    ra = scenario(os.path.join(tmp, "sa"), _combined_files_layout_a(), TOL, MAX_AREA)
    rb = scenario(os.path.join(tmp, "sb"), _combined_files_layout_b(), TOL, MAX_AREA)
    assert ra == rb == {"import": 0, "check": 0, "publish": 0}

    names = ("parcels.json", "shared_boundaries.json", "conflicts.json",
             "provenance.json", "manifest.json")
    # 打乱文件与记录顺序：发布物逐字节一致
    for name in names:
        assert artifact_bytes(os.path.join(tmp, "sa"), name) == \
               artifact_bytes(os.path.join(tmp, "sb"), name), name

    # 相同输入重跑：v2 与 v1 的数据载荷逐项一致
    code, _ = run_cli(["publish", "--store", os.path.join(tmp, "sa"),
                       "--tolerance", str(TOL), "--max-area-change", str(MAX_AREA)])
    assert code == 0
    for name in ("parcels.json", "shared_boundaries.json", "conflicts.json",
                 "provenance.json"):
        d1 = load_artifact(os.path.join(tmp, "sa"), name, version=1)
        d2 = load_artifact(os.path.join(tmp, "sa"), name, version=2)
        assert d1 == d2, name
    # manifest 的确定性字段一致（版本号与产物哈希随版本不同，属预期）
    m1 = load_artifact(os.path.join(tmp, "sa"), "manifest.json", version=1)
    m2 = load_artifact(os.path.join(tmp, "sa"), "manifest.json", version=2)
    for key in ("tolerance", "max_area_change", "input_sha256", "counts"):
        assert m1[key] == m2[key], key

    # 冲突分类逐项一致（手算预期：BB1 超容差、BD1 未知地块、BE1 一对多）
    conflicts = conflicts_by_id(load_artifact(os.path.join(tmp, "sa"), "conflicts.json"))
    assert conflicts["BB1"][0]["code"] == "NO_SHARED_GEOMETRY"
    assert conflicts["BD1"][0]["code"] == "UNKNOWN_PARCEL"
    assert conflicts["BE1"][0]["code"] == "BOUNDARY_ID_MULTIPLE_PAIRS"
    edges = edges_by_id(load_artifact(os.path.join(tmp, "sa"), "shared_boundaries.json"))
    assert set(edges) == {"BA1", "BC1"}
    assert ring_approx_eq(edges["BC1"]["coordinates"],
                          [[20, 0.01], [21, 0.02], [22, 0.01]])


# ---------------------------------------------------------------- 用例 14
# 拓扑不变量独立确认：共享段逐点反向相等、环闭合、面积变化在界内。
def test_case14_topology_invariants(tmp):
    store = os.path.join(tmp, "s14")
    r = scenario(store, _combined_files_layout_a(), TOL, MAX_AREA)
    assert r == {"import": 0, "check": 0, "publish": 0}, r
    parcels = load_artifact(store, "parcels.json")
    edges = load_artifact(store, "shared_boundaries.json")
    prov = {p["parcel_id"]: p for p in load_artifact(store, "provenance.json")}

    for parcel in parcels:
        check_ring_basics(parcel)
    for edge in edges:
        check_shared_edge_topology(parcels, edge, MAX_AREA, prov)
    for parcel in parcels:
        pid = parcel["id"]
        pr = prov[pid]
        # 面积变化在界内，且发布面积可用独立鞋带公式复算
        assert pr["area_change"] <= MAX_AREA + 1e-9, pid
        recomputed = polygon_area(parcel["exterior"], parcel.get("holes", []))
        assert approx(recomputed, pr["final_area"], 1e-9), pid
        assert approx(pr["area_change"],
                      abs(pr["final_area"] - pr["original_area"]), 1e-9), pid


# ---------------------------------------------------------------- 用例 15
# 配对不唯一（尖刺形共享链两种方向都能配对）：冲突隔离。
def test_case15_ambiguous_match(tmp):
    store = os.path.join(tmp, "s15")
    p1 = _parcel("P1", "SA", [[0, 0], [0.05, 0], [2, 1], [0, 1]])
    # P2 的共享链是尖刺 (0,0)->(0.025,0.1)->(0.05,0)：两端点与 P1 的短边
    # 在容差内双向都能配对（正/反两种配法），配对不唯一。
    p2 = _parcel("P2", "SB", [[0, 0], [0.025, 0.1], [0.05, 0], [2, -1], [0, -1]])
    files = {"in.json": _input(_srcs(("SA", 10), ("SB", 5)), [p1, p2],
                               [_adj("B1", "P1", "P2")])}
    r = scenario(store, files, 0.15, MAX_AREA)
    assert r == {"import": 0, "check": 0, "publish": 0}, r
    conflicts = conflicts_by_id(load_artifact(store, "conflicts.json"))
    assert conflicts["B1"][0]["code"] == "AMBIGUOUS_MATCH"
    assert load_artifact(store, "shared_boundaries.json") == []
    parcels = parcels_by_id(load_artifact(store, "parcels.json"))
    assert ring_approx_eq(parcels["P1"]["exterior"], p1["exterior"])
    # P2 输入为顺时针，导入时规范化为逆时针（首顶点不变），几何不变
    assert ring_approx_eq(parcels["P2"]["exterior"],
                          [[0, 0], [0, -1], [2, -1], [0.05, 0], [0.025, 0.1]])


# ---------------------------------------------------------------- 用例 16
# 边界不能沿地块环闭合（两侧共享链端点对不上）：冲突隔离。
def test_case16_endpoint_mismatch(tmp):
    store = os.path.join(tmp, "s16")
    p1 = _parcel("P1", "SA", [[0, 0], [2, 0], [4, 0], [4, 2], [0, 2]])
    p2 = _parcel("P2", "SB", [[0, 0], [0, -2], [3, -2], [3, 0]])
    files = {"in.json": _input(_srcs(("SA", 10), ("SB", 5)), [p1, p2],
                               [_adj("B1", "P1", "P2")])}
    r = scenario(store, files, 0.1, MAX_AREA)
    assert r == {"import": 0, "check": 0, "publish": 0}, r
    conflicts = conflicts_by_id(load_artifact(store, "conflicts.json"))
    assert conflicts["B1"][0]["code"] == "ENDPOINT_MISMATCH"
    parcels = parcels_by_id(load_artifact(store, "parcels.json"))
    assert ring_approx_eq(parcels["P1"]["exterior"], p1["exterior"])
    assert ring_approx_eq(parcels["P2"]["exterior"], p2["exterior"])


# ---------------------------------------------------------------- 用例 17
# 链中部偏移超过容差（顶点被附近平行边"拉住"但链间偏差超容差）。
def test_case17_offset_beyond_tolerance(tmp):
    store = os.path.join(tmp, "s17")
    p1 = _parcel("P1", "SA", [[0, 0], [4, 0], [4, 0.2], [0, 0.2]])
    p2 = _parcel("P2", "SB", [[0, 0], [0, -1], [4, -1], [4, 0], [2, 0.15]])
    files = {"in.json": _input(_srcs(("SA", 10), ("SB", 5)), [p1, p2],
                               [_adj("B1", "P1", "P2")])}
    r = scenario(store, files, 0.1, MAX_AREA)
    assert r == {"import": 0, "check": 0, "publish": 0}, r
    conflicts = conflicts_by_id(load_artifact(store, "conflicts.json"))
    c = conflicts["B1"][0]
    assert c["code"] == "OFFSET_BEYOND_TOLERANCE"
    assert approx(c["details"]["max_deviation"], 0.15)
    parcels = parcels_by_id(load_artifact(store, "parcels.json"))
    assert ring_approx_eq(parcels["P1"]["exterior"], p1["exterior"])
    assert ring_approx_eq(parcels["P2"]["exterior"], p2["exterior"])


# ---------------------------------------------------------------- 用例 18
# 输入环走向不同（顺时针/逆时针）不影响输出：导入时统一规范化。
def test_case18_ring_orientation_normalized(tmp):
    p1 = _parcel("P1", "SA", [[0, 0], [10, 0], [10, 4], [0, 4]])
    p2_ccw = _parcel("P2", "SB", [[14, 0], [14, 4], [10, 4], [10, 2], [10, 0]])
    # 同一几何的顺时针描述（首顶点相同）
    p2_cw = _parcel("P2", "SB", [[14, 0], [10, 0], [10, 2], [10, 4], [14, 4]])
    fa = {"in.json": _input(_srcs(("SA", 10), ("SB", 5)), [p1, p2_ccw],
                            [_adj("B1", "P1", "P2")])}
    fb = {"in.json": _input(_srcs(("SA", 10), ("SB", 5)), [p1, p2_cw],
                            [_adj("B1", "P1", "P2")])}
    ra = scenario(os.path.join(tmp, "s18a"), fa, TOL, MAX_AREA)
    rb = scenario(os.path.join(tmp, "s18b"), fb, TOL, MAX_AREA)
    assert ra == rb == {"import": 0, "check": 0, "publish": 0}
    for name in ("parcels.json", "shared_boundaries.json", "provenance.json"):
        assert artifact_bytes(os.path.join(tmp, "s18a"), name) == \
               artifact_bytes(os.path.join(tmp, "s18b"), name), name


# ---------------------------------------------------------------- 用例 19
# 查询入口：指定地块与共享边；被隔离边界给出冲突记录。
def test_case19_query_entries(tmp):
    store = os.path.join(tmp, "s19")
    r = scenario(store, _combined_files_layout_a(), TOL, MAX_AREA)
    assert r == {"import": 0, "check": 0, "publish": 0}, r

    code, out = run_cli(["query", "--store", store, "parcel", "PA1"])
    assert code == 0
    got = json.loads(out)
    assert got["parcel"]["id"] == "PA1"
    assert got["provenance"]["edits"][0]["boundary_id"] == "BA1"

    code, out = run_cli(["query", "--store", store, "boundary", "BA1"])
    assert code == 0
    got = json.loads(out)
    assert got["status"] == "published"
    assert got["shared_edge"]["boundary_id"] == "BA1"

    code, out = run_cli(["query", "--store", store, "boundary", "BB1"])
    assert code == 0
    got = json.loads(out)
    assert got["status"] == "conflicted"
    assert got["conflicts"][0]["code"] == "NO_SHARED_GEOMETRY"

    code, _ = run_cli(["query", "--store", store, "boundary", "NOPE"])
    assert code == 1
    code, _ = run_cli(["query", "--store", store, "parcel", "NOPE"])
    assert code == 1


# ---------------------------------------------------------------- 用例 21
# check 只写非正式暂存报告；正式输出只能在 publish 后读取。
def test_case21_check_report_is_unofficial(tmp):
    store = os.path.join(tmp, "s21")
    p1 = _parcel("P1", "SA", [[0, 0], [2, 0], [2, 2], [0, 2]])
    p2 = _parcel("P2", "SB", [[2.2, 0], [4, 0], [4, 2], [2.2, 2]])
    files = {"in.json": _input(_srcs(("SA", 10), ("SB", 5)), [p1, p2],
                               [_adj("B1", "P1", "P2")])}
    r = scenario(store, files, TOL, MAX_AREA, do_publish=False)
    assert r == {"import": 0, "check": 0}, r
    # 没有正式发布：query 报错，versions 目录不存在
    code, _ = run_cli(["query", "--store", store, "parcel", "P1"])
    assert code == 2
    assert not os.path.exists(os.path.join(store, "versions"))
    with open(os.path.join(store, "staging", "check_report.json"),
              encoding="utf-8") as f:
        report = json.load(f)
    assert report["official"] is False
    assert report["conflicts"][0]["code"] == "NO_SHARED_GEOMETRY"
    assert report["summary"] == {"accepted": 0, "conflicts": 1}
    # 未导入直接 check：整次失败
    code, _ = run_cli(["check", "--store", os.path.join(tmp, "empty"),
                       "--tolerance", "0.05", "--max-area-change", "1"])
    assert code == 2


# ---------------------------------------------------------------- 用例 22
# 子进程端到端：python -m parcel_boundary 各入口可用。
def test_case22_subprocess_entry(tmp):
    import subprocess
    import sys as _sys
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env = dict(os.environ, PYTHONPATH=root)
    store = os.path.join(tmp, "s22")
    p1 = _parcel("P1", "SA", [[0, 0], [2, 0], [2, 2], [0, 2]])
    p2 = _parcel("P2", "SB", [[2, 0], [4, 0], [4, 2], [2, 2], [2.04, 1]])
    infile = os.path.join(tmp, "in.json")
    write_json(infile, _input(_srcs(("SA", 10), ("SB", 5)), [p1, p2],
                              [_adj("B1", "P1", "P2")]))

    def run(*argv):
        return subprocess.run([_sys.executable, "-m", "parcel_boundary"] + list(argv),
                              capture_output=True, text=True, env=env)

    r = run("import", "--store", store, infile)
    assert r.returncode == 0, r.stderr
    r = run("check", "--store", store, "--tolerance", "0.05",
            "--max-area-change", "0.5")
    assert r.returncode == 0, r.stderr
    r = run("publish", "--store", store, "--tolerance", "0.05",
            "--max-area-change", "0.5")
    assert r.returncode == 0 and "v1" in r.stdout, r.stderr
    r = run("query", "--store", store, "boundary", "B1")
    assert r.returncode == 0, r.stderr
    edge = json.loads(r.stdout)["shared_edge"]
    assert edge["coordinates"] == [[2.0, 0.0], [2.0, 2.0]]
    r = run("query", "--store", store, "parcel", "P2")
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["parcel"]["exterior"] == [[2.0, 2.0], [2.0, 0.0],
                                         [4.0, 0.0], [4.0, 2.0]]
# 之字形共享边界（一条连续弯折链）整体校正，不漏段。
def test_case20_zigzag_shared_chain(tmp):
    store = os.path.join(tmp, "s20")
    p1 = _parcel("P1", "SA", [[0, 0], [2, 0], [2, 2], [0, 2]])
    # L 形地块，与 P1 共享 (2,0)->(2,2)->(0,2) 的弯折链
    p2 = _parcel("P2", "SB", [[2, 0], [3, 0], [3, 3], [0, 3], [0, 2], [2, 2]])
    files = {"in.json": _input(_srcs(("SA", 10), ("SB", 5)), [p1, p2],
                               [_adj("B1", "P1", "P2")])}
    r = scenario(store, files, TOL, MAX_AREA)
    assert r == {"import": 0, "check": 0, "publish": 0}, r
    edges = edges_by_id(load_artifact(store, "shared_boundaries.json"))
    # 目录方向：起点为字典序较小端点 (0,2)
    assert ring_approx_eq(edges["B1"]["coordinates"],
                          [[0, 2], [2, 2], [2, 0]])
    parcels = parcels_by_id(load_artifact(store, "parcels.json"))
    # 几何不变（两侧原本一致），面积不变
    prov = {p["parcel_id"]: p for p in load_artifact(store, "provenance.json")}
    assert approx(prov["P1"]["area_change"], 0.0)
    assert approx(prov["P2"]["area_change"], 0.0)
    check_shared_edge_topology(
        load_artifact(store, "parcels.json"), edges["B1"], MAX_AREA,
        prov)
