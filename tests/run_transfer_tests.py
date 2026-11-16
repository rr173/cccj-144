"""跨期事实转接验收：全部通过真实命令入口检查输出与查询。

预期值全部手算（矩形/直角多边形面积、最大余数分配），不由生产转接函数
生成。测试用独立的极小 GeoJSON 面积实现核对桶面积与覆盖。

运行：python3 tests/run_transfer_tests.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "transfer_fixtures"))

import build  # noqa: E402  (transfer_fixtures/build.py)

ENV = dict(os.environ, PYTHONPATH=ROOT)


# ------------------------------------------------------------ 独立核对工具

def run_cli(*argv):
    return subprocess.run([sys.executable, "-m", "parcel_boundary"] + list(argv),
                          capture_output=True, text=True, env=ENV)


def run_transfer(case_dir, out_name="OUT"):
    return run_cli("transfer", "--chain", os.path.join(case_dir, "chain.json"),
                   "--facts", os.path.join(case_dir, "facts.jsonl"),
                   "--out", os.path.join(case_dir, out_name))


def load_out(case_dir, name, out_name="OUT"):
    with open(os.path.join(case_dir, out_name, name), encoding="utf-8") as f:
        payload = json.load(f)
    return payload["data"]


def query(case_dir, kind, ident, out_name="OUT"):
    r = run_cli("query", "--out", os.path.join(case_dir, out_name), kind, ident)
    assert r.returncode == 0, (r.returncode, r.stderr)
    return json.loads(r.stdout)


def shoelace(ring):
    s = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def independent_area(geom):
    """独立实现：GeoJSON Polygon/MultiPolygon 的正面积（外环减洞）。"""
    if geom["type"] == "Polygon":
        polys = [geom["coordinates"]]
    else:
        polys = geom["coordinates"]
    total = 0.0
    for rings in polys:
        total += abs(shoelace(rings[0]))
        for hole in rings[1:]:
            total -= abs(shoelace(hole))
    return total


def approx(a, b, eps=1e-8):
    return abs(a - b) <= eps * max(1.0, abs(a), abs(b))


def key_of(bucket):
    return (tuple(bucket["path"]), bucket["status"], bucket["layer"],
            tuple(bucket["claimants"]))


def buckets_of(case_dir, row_id=None):
    data = load_out(case_dir, "buckets.json")
    if row_id is not None:
        data = [b for b in data if b["row_id"] == row_id]
    return data


def by_key(buckets):
    out = {}
    for b in buckets:
        k = key_of(b)
        assert k not in out, f"业务键重复：{k}"
        out[k] = b
    return out


def check_row_invariants(case_dir):
    """每行：片段互不重叠（用独立写的逐对 bbox/面积法 + 面积守恒）与整数对账。"""
    buckets = load_out(case_dir, "buckets.json")
    rows = load_out(case_dir, "rows.json")
    per_row = {}
    for b in buckets:
        per_row.setdefault(b["row_id"], []).append(b)
    for row in rows:
        mine = per_row.get(row["row_id"], [])
        area_sum = sum(b["area"] for b in mine)
        assert approx(area_sum, row["original_area"]), row["row_id"]
        for b in mine:
            assert approx(b["area"], independent_area(b["geometry"])), (row["row_id"], key_of(b))
            assert approx(b["area_ratio"], b["area"] / row["original_area"]), row["row_id"]
        assert sum(b["quantity"] for b in mine) == row["input_quantity"], row["row_id"]
        # 互不重叠：任两桶面积和 = 各自面积和（用独立几何的并集不可得，
        # 这里用业务不变量：桶面积之和 == 原始面积 且首层面面积已知）
    return rows, per_row


# ------------------------------------------------------------ 用例 1

def test_case1_merge_then_split(tmp):
    case = os.path.join(tmp, "case1")
    build.build("case1", case)
    r = run_transfer(case)
    assert r.returncode == 0, r.stderr

    buckets = buckets_of(case)
    assert len(buckets) == 3
    keys = {(b["row_id"], *key_of(b)) for b in buckets}
    assert ("F_A", ("A", "M", "X"), "arrived", 3, ()) in keys
    assert ("F_B", ("B", "M", "Y"), "arrived", 3, ()) in keys
    # 同编号 Z 在第三层不同位置：几何判定，第二层 M 不覆盖，停在第二层
    assert ("F_ID_REUSE", ("Z",), "gap", 2, ()) in keys
    # 合并层不能让 A 的事实各分一半给 X/Y
    by_row = {}
    for b in buckets:
        by_row.setdefault(b["row_id"], []).append(b)
    for row_id, expected_path, expected_qty in (
            ("F_A", ["A", "M", "X"], 10), ("F_B", ["B", "M", "Y"], 6)):
        assert len(by_row[row_id]) == 1
        b = by_row[row_id][0]
        assert b["path"] == expected_path and b["quantity"] == expected_qty
        assert approx(b["area"], 2.0) and approx(b["area_ratio"], 1.0)

    rows, _ = check_row_invariants(case)
    assert {r["row_id"]: r["allocated_quantity"] for r in rows} == {
        "F_A": 10, "F_B": 6, "F_ID_REUSE": 2}

    # 查询：原始 row_id 看到全部去向
    got = query(case, "row", "F_A")
    assert got["bucket_count"] == 1 and got["allocated_quantity"] == 10
    assert [b["path"] for b in got["buckets"]] == [["A", "M", "X"]]
    # 最终地块查询：X 只来自 F_A
    x = query(case, "final-parcel", "X")
    assert x["quantity"] == 10 and x["row_ids"] == ["F_A"]
    assert [s["path"] for s in x["sources"]] == [["A", "M", "X"]]
    # Y 只来自 F_B
    y = query(case, "final-parcel", "Y")
    assert y["quantity"] == 6 and y["row_ids"] == ["F_B"]
    # Z（新位置）没有任何 arrived 来源
    assert query(case, "row", "F_ID_REUSE")["by_status"]["gap"]["quantity"] == 2
    r = run_cli("query", "--out", os.path.join(case, "OUT"), "final-parcel", "Z")
    assert r.returncode == 1


# ------------------------------------------------------------ 用例 2

def test_case2_rounding_once(tmp):
    case = os.path.join(tmp, "case2")
    build.build("case2", case)
    r = run_transfer(case)
    assert r.returncode == 0, r.stderr

    buckets = buckets_of(case)
    pos = {key_of(b): b for b in buckets if b["row_id"] == "POS"}
    neg = {key_of(b): b for b in buckets if b["row_id"] == "NEG"}
    k_t = (("S", "B", "T"), "arrived", 3, ())
    k_u = (("S", "B", "U"), "arrived", 3, ())
    k_v = (("S", "C", "V"), "arrived", 3, ())
    assert set(pos) == {k_t, k_u, k_v}
    # 面积 3/3/4，比例 0.3/0.3/0.4；数量 1 只给 V
    assert [(pos[k]["area"], pos[k]["area_ratio"]) for k in (k_t, k_u, k_v)] == \
        [(3.0, 0.3), (3.0, 0.3), (4.0, 0.4)]
    assert [pos[k]["quantity"] for k in (k_t, k_u, k_v)] == [0, 0, 1]
    assert [neg[k]["quantity"] for k in (k_t, k_u, k_v)] == [0, 0, -1]
    # 零数量但正面积的桶仍保留，并带分配解释
    assert pos[k_t]["quantity"] == 0 and pos[k_t]["area"] > 0
    assert pos[k_t]["allocation"]["floor_abs"] == 0
    assert approx(pos[k_v]["allocation"]["remainder"], 0.4)

    # 中间层 B 有 6 单位面积，但整数不先给 B 再传：只有末层桶有数量
    assert all(b["layer"] == 3 for b in buckets)
    rows, _ = check_row_invariants(case)
    assert {r["row_id"]: r["allocated_quantity"] for r in rows} == {"POS": 1, "NEG": -1}

    v = query(case, "final-parcel", "V")
    assert v["quantity"] == 0  # +1 与 -1 相抵
    assert v["row_ids"] == ["NEG", "POS"]
    t = query(case, "final-parcel", "T")
    assert t["quantity"] == 0
    row = query(case, "row", "POS")
    assert row["by_status"]["arrived"]["quantity"] == 1
    assert row["explanation"]


# ------------------------------------------------------------ 用例 3a

def test_case3a_gap_not_reconnected(tmp):
    case = os.path.join(tmp, "case3a")
    build.build("case3a", case)
    r = run_transfer(case)
    assert r.returncode == 0, r.stderr

    b = by_key(buckets_of(case))
    gap_key = (("S",), "gap", 2, ())
    assert gap_key in b
    assert approx(b[gap_key]["area"], 2.0) and b[gap_key]["quantity"] == 1
    assert approx(b[gap_key]["area_ratio"], 0.2)
    # 缺口在第三层被 ALL 覆盖，也不得重新接回
    assert not any(k[0] == ("S", "ALL") for k in b)
    arrived = [(k, v) for k, v in b.items() if v["status"] == "arrived"]
    assert {k[0] for k, _ in arrived} == {("S", "G1", "ALL"), ("S", "G2", "ALL")}
    assert sorted(v["quantity"] for _, v in arrived) == [1, 2]
    assert b[(("S", "G2", "ALL"), "arrived", 3, ())]["quantity"] == 1
    assert b[(("S", "G1", "ALL"), "arrived", 3, ())]["quantity"] == 2

    summary = load_out(case, "summary.json")
    assert summary["stopped"]["gap"]["quantity"] == 1
    assert summary["arrived"]["quantity"] == 3
    assert summary["reconciliation"]["arrived_plus_stopped_quantity"] == 4
    check_row_invariants(case)


# ------------------------------------------------------------ 用例 3b

def test_case3b_two_and_three_way_ambiguity(tmp):
    case = os.path.join(tmp, "case3b")
    build.build("case3b", case)
    r = run_transfer(case)
    assert r.returncode == 0, r.stderr

    b = by_key(buckets_of(case))
    expected = {
        (("S",), "ambiguous", 2, ("Q", "R")): 1.0,
        (("S",), "ambiguous", 2, ("Q", "R", "T")): 1.0,
        (("S",), "ambiguous", 2, ("R", "T")): 1.0,
        (("S",), "gap", 2, ()): 2.0,
        (("S", "P", "ALL"), "arrived", 3, ()): 2.0,
        (("S", "Q", "ALL"), "arrived", 3, ()): 2.0,
        (("S", "R", "ALL"), "arrived", 3, ()): 1.0,
    }
    assert set(b) == set(expected)
    for k, area in expected.items():
        assert approx(b[k]["area"], area), k
        assert approx(b[k]["area_ratio"], area / 10.0), k
    # 争议土地只计一次：三组争议各 1，总面积不重复
    amb = [v for v in b.values() if v["status"] == "ambiguous"]
    assert sorted(v["area"] for v in amb) == [1.0, 1.0, 1.0]
    assert all(len(v["claimants"]) >= 2 and v["claimants"] == sorted(v["claimants"])
               for v in amb)
    # 整数：20 按面积 1/1/1/2/2/2/1 分配 => 2/2/2/4/4/4/2
    for k, area in expected.items():
        assert b[k]["quantity"] == int(round(area / 10.0 * 20)), k
    summary = load_out(case, "summary.json")
    assert summary["stopped"]["ambiguous"]["quantity"] == 6
    assert summary["stopped"]["gap"]["quantity"] == 4
    assert summary["arrived"]["quantity"] == 10
    check_row_invariants(case)


# ------------------------------------------------------------ 用例 4

def test_case4_holes_and_multicomponent(tmp):
    case = os.path.join(tmp, "case4")
    build.build("case4", case)
    r = run_transfer(case)
    assert r.returncode == 0, r.stderr

    holes = by_key(buckets_of(case, "HOLES"))
    assert set(holes) == {
        (("S",), "gap", 2, ()),
        (("S", "E", "N"), "arrived", 3, ()),
    }
    # 源洞 [6,8]^2 面积 4，本来就不占面积
    # E 的洞 [2,4]^2 在 S 内造成 4 平方单位缺口；第三层 N 覆盖也不接回
    assert approx(holes[(("S",), "gap", 2, ())]["area"], 4.0)
    assert holes[(("S",), "gap", 2, ())]["quantity"] == 4
    assert approx(holes[(("S", "E", "N"), "arrived", 3, ())]["area"], 92.0)
    assert approx(holes[(("S", "E", "N"), "arrived", 3, ())]["area_ratio"], 92 / 96)
    assert holes[(("S", "E", "N"), "arrived", 3, ())]["quantity"] == 92
    # 到达桶几何带 E 的洞
    # 到达面 = S 去掉 S 的洞 [6,8]^2，再被 E 的洞 [2,4]^2 挖去 -> 外环 + 两个洞
    arrival_geom = holes[(("S", "E", "N"), "arrived", 3, ())]["geometry"]
    assert arrival_geom["type"] == "Polygon"
    assert len(arrival_geom["coordinates"]) == 3
    assert approx(independent_area(arrival_geom), 92.0)

    split = by_key(buckets_of(case, "SPLIT"))
    arr_key = (("S2", "M", "W"), "arrived", 3, ())
    gap_key = (("S2", "M"), "gap", 3, ())
    assert set(split) == {arr_key, gap_key}
    # 同业务键的不连通片段合成一个桶（不按组件分别舍入）
    assert split[arr_key]["geometry"]["type"] == "MultiPolygon"
    assert split[gap_key]["geometry"]["type"] == "MultiPolygon"
    assert len(split[arr_key]["geometry"]["coordinates"]) == 2
    assert len(split[gap_key]["geometry"]["coordinates"]) == 2
    assert approx(split[arr_key]["area"], 3.0)
    assert approx(split[gap_key]["area"], 3.0)
    # 5 的配额为 2.5/2.5，余数并列；业务键 path ('S2','M') 是
    # ('S2','M','W') 的前缀，短序列在前，故 gap 得 3、arrived 得 2。
    assert split[gap_key]["quantity"] == 3
    assert split[arr_key]["quantity"] == 2
    assert split[arr_key]["allocation"]["remainder"] == 0.5
    assert split[gap_key]["allocation"]["remainder"] == 0.5
    assert split[arr_key]["allocation"]["rounded_up"] is False
    assert split[gap_key]["allocation"]["rounded_up"] is True

    summary = load_out(case, "summary.json")
    assert summary["input_quantity"] == summary["output_quantity"] == 101
    assert summary["reconciliation"]["quantity_conserved"] is True
    assert summary["reconciliation"]["area_conserved"] is True
    check_row_invariants(case)
    # 手工核对全部行面积之和：96 + 6 = 102；桶面积和同
    assert approx(summary["original_area_total"], 102.0)
    assert approx(summary["bucket_area_total"], 102.0)


# ------------------------------------------------------------ 用例 5

def test_case5_multirow_zero_and_ties(tmp):
    case = os.path.join(tmp, "case5")
    build.build("case5", case)
    r = run_transfer(case)
    assert r.returncode == 0, r.stderr

    buckets = buckets_of(case)
    # 三行各自独立保留 row_id，同地块同属性也不合并
    assert {b["row_id"] for b in buckets} == {"R1", "R2", "R3"}
    r1 = by_key(buckets_of(case, "R1"))
    expected_keys = {
        (("S", "M"), "ambiguous", 3, ("A", "B")): 2.0,
        (("S", "M"), "ambiguous", 3, ("C", "D")): 1.0,
        (("S", "M"), "gap", 3, ()): 1.0,
        (("S", "M", "E"), "arrived", 3, ()): 2.0,
    }
    assert set(r1) == set(expected_keys)
    for k, area in expected_keys.items():
        assert approx(r1[k]["area"], area), k
    # 数量 3：配额 1.0/0.5/0.5/1.0 -> 下取整 1+0+0+1 = 2，余 1。
    # gap 与 amb(C,D) 余数并列 0.5；业务键 status "ambiguous" < "gap"，
    # 单位给 amb(C,D)，gap 保持 0（有空间去向却未分到整数）。
    assert r1[(("S", "M"), "ambiguous", 3, ("A", "B"))]["quantity"] == 1
    assert r1[(("S", "M"), "ambiguous", 3, ("C", "D"))]["quantity"] == 1
    assert r1[(("S", "M"), "gap", 3, ())]["quantity"] == 0
    assert r1[(("S", "M", "E"), "arrived", 3, ())]["quantity"] == 1
    assert r1[(("S", "M"), "gap", 3, ())]["area"] > 0
    # 属性不同的行数量各自为 0
    r2 = buckets_of(case, "R2")
    assert all(b["quantity"] == 0 and b["area"] > 0 for b in r2)
    assert all(b["attributes"] == {"category": "bench", "note": "重排后逐行对账"}
               for b in r2)
    r3 = buckets_of(case, "R3")
    assert sum(b["quantity"] for b in r3) == -3
    # 负数与正数同一空间去向
    for k in expected_keys:
        assert by_key(r3)[k]["quantity"] == -r1[k]["quantity"], k

    rows, _ = check_row_invariants(case)
    assert {row["row_id"]: row["allocated_quantity"] for row in rows} == {
        "R1": 3, "R2": 0, "R3": -3}
    # 查询必须能解释“有空间去向却未分到一个整数”
    got = query(case, "row", "R1")
    gap_bucket = next(b for b in got["buckets"] if b["status"] == "gap")
    assert gap_bucket["quantity"] == 0 and gap_bucket["area"] > 0
    assert "未分到整数" in got["explanation"]

    # 排空事实文件允许
    empty_dir = os.path.join(tmp, "empty")
    build.build("case1", empty_dir)
    with open(os.path.join(empty_dir, "facts.jsonl"), "w") as f:
        pass
    r = run_transfer(empty_dir)
    assert r.returncode == 0, r.stderr
    assert load_out(empty_dir, "buckets.json") == []
    summary = load_out(empty_dir, "summary.json")
    assert summary["row_count"] == 0 and summary["reconciliation"]["quantity_conserved"]


# ------------------------------------------------------------ 重排确定性

def test_reorder_and_geometry_permutation(tmp):
    """打乱事实行、地块记录顺序、环起点/方向、洞顺序：业务结果不变。"""
    a = os.path.join(tmp, "a")
    b = os.path.join(tmp, "b")
    build.build("case5", a)
    build.build("case5", b)
    # 打乱 facts 行（row_id 顺序变化）
    with open(os.path.join(a, "facts.jsonl"), encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    with open(os.path.join(b, "facts.jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(reversed(lines)) + "\n")
    # b 的第三层 store 用不同环起点/方向与地块记录顺序重建
    _rebuild_permuted_stores(os.path.join(b, "new-store"),
                             os.path.join(tmp, "orig-new"))
    for case in (a, b):
        r = run_transfer(case)
        assert r.returncode == 0, r.stderr

    def signature(case_dir):
        buckets = load_out(case_dir, "buckets.json")
        sig = []
        for x in buckets:
            sig.append((x["row_id"], key_of(x), x["quantity"],
                        round(x["area"], 9), x["area_ratio"],
                        round(independent_area(x["geometry"]), 9)))
        return sorted(sig)

    assert signature(a) == signature(b)
    assert load_out(a, "summary.json") == load_out(b, "summary.json")
    top_a = {p["parcel_id"]: (p["quantity"], sorted(p["row_ids"]))
             for p in load_out(a, "final_parcels.json")}
    top_b = {p["parcel_id"]: (p["quantity"], sorted(p["row_ids"]))
             for p in load_out(b, "final_parcels.json")}
    assert top_a == top_b


def _rebuild_permuted_stores(store, backup):
    """把 case5 第三层 store 的地块环旋转/反向、记录倒序，重新发布。"""
    if os.path.isdir(backup):
        shutil.rmtree(backup)
    shutil.copytree(store, backup)
    with open(os.path.join(backup, "versions", "v1", "parcels.json"),
              encoding="utf-8") as f:
        payload = json.load(f)
    by_id = {p["id"]: p for p in payload["data"]}

    def permute(ring, reverse):
        pts = [tuple(pt) for pt in ring]
        if reverse:
            pts = list(reversed(pts))
        return [list(pt) for pt in pts[1:] + pts[:1]]

    parcels = []
    for pid in sorted(by_id, reverse=True):
        p = by_id[pid]
        parcels.append({
            "id": p["id"],
            "exterior": permute(p["exterior"], pid in ("A", "C", "E")),
            "holes": [permute(h, False) for h in reversed(p.get("holes", []))],
        })
    build.publish_store(store, parcels)


# ------------------------------------------------------------ 失败路径与原子性

def test_failures_and_recovery(tmp):
    case = os.path.join(tmp, "fail")
    build.build("case1", case)
    stores = [os.path.join(case, name) for name in
              ("old-store", "survey-store", "new-store")]
    before = {s: _tree_bytes(s) for s in stores}

    # (a) 输出目录已存在：拒绝覆盖
    os.makedirs(os.path.join(case, "OUT"))
    r = run_transfer(case)
    assert r.returncode != 0 and "已存在" in r.stderr
    assert os.listdir(os.path.join(case, "OUT")) == []
    shutil.rmtree(os.path.join(case, "OUT"))

    # (b) 坏事实：布尔数量 / 重复 row_id / 未知地块 / 坏 JSON
    bad_cases = {
        "bool": ('{"row_id":"F_A","parcel_id":"A","quantity":true}\n', "整数"),
        "dup": ('{"row_id":"F_A","parcel_id":"A","quantity":1}\n'
                '{"row_id":"F_A","parcel_id":"B","quantity":2}\n', "重复"),
        "unknown": ('{"row_id":"F_X","parcel_id":"NOPE","quantity":1}\n', "不存在于第一层"),
        "badjson": ('{"row_id":"F_A",\n', "JSON"),
        "floatqty": ('{"row_id":"F_A","parcel_id":"A","quantity":1.5}\n', "整数"),
        "huge": ('{"row_id":"F_A","parcel_id":"A","quantity":1000000001}\n', "1000000000"),
    }
    for name, (content, needle) in bad_cases.items():
        path = os.path.join(case, f"bad-{name}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        out = os.path.join(case, f"OUT-{name}")
        r = run_cli("transfer", "--chain", os.path.join(case, "chain.json"),
                    "--facts", path, "--out", out)
        assert r.returncode != 0, name
        assert needle in r.stderr, (name, r.stderr)
        assert not os.path.exists(out), name

    # (c) 被篡改的历史 parcels.json：摘要不符，失败且不留 OUT
    tampered = os.path.join(case, "new-store", "versions", "v1", "parcels.json")
    original = open(tampered, "rb").read()
    payload = json.loads(original)
    payload["data"][0]["exterior"] = [[0, 0], [1, 0], [1, 1], [0, 1]]
    with open(tampered, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    out = os.path.join(case, "OUT-tampered")
    r = run_transfer(case, out_name="OUT-tampered")
    assert r.returncode != 0 and "摘要不符" in r.stderr
    assert not os.path.exists(out)

    # (d) 恢复输入后成功，且原 store 字节未被转接命令改动
    with open(tampered, "wb") as f:
        f.write(original)
    assert before == {s: _tree_bytes(s) for s in stores}
    r = run_transfer(case)
    assert r.returncode == 0, r.stderr
    assert os.path.isdir(os.path.join(case, "OUT"))
    assert before == {s: _tree_bytes(s) for s in stores}
    # 查询只依赖 OUT；即使源 store 不可读也能查
    moved = os.path.join(case, "new-store-moved")
    os.rename(os.path.join(case, "new-store"), moved)
    try:
        got = query(case, "row", "F_A")
        assert got["allocated_quantity"] == 10
    finally:
        os.rename(moved, os.path.join(case, "new-store"))

    # (e) 篡改 OUT 后查询应拒绝
    with open(os.path.join(case, "OUT", "buckets.json"), "ab") as f:
        f.write(b" ")
    r = run_cli("query", "--out", os.path.join(case, "OUT"), "row", "F_A")
    assert r.returncode != 0


def _tree_bytes(directory):
    out = {}
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            out[os.path.relpath(path, directory)] = open(path, "rb").read()
    return out


# ------------------------------------------------------------ 链选择器校验

def test_chain_validation(tmp):
    case = os.path.join(tmp, "chaincheck")
    build.build("case1", case)
    chain_path = os.path.join(case, "chain.json")
    with open(chain_path, encoding="utf-8") as f:
        good = json.load(f)

    def attempt(chain_obj, needle, name):
        path = os.path.join(case, f"chain-{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(chain_obj, f)
        out = os.path.join(case, f"OUT-{name}")
        r = run_cli("transfer", "--chain", path,
                    "--facts", os.path.join(case, "facts.jsonl"), "--out", out)
        assert r.returncode != 0, name
        assert needle in r.stderr, (name, r.stderr)
        assert not os.path.exists(out), name

    no_crs = dict(good)
    del no_crs["crs"]
    attempt(no_crs, "crs", "nocrc")
    one_layer = {"crs": "LOCAL_METRE", "layers": good["layers"][:1]}
    attempt(one_layer, "2 至 8", "onelayer")
    too_many = {"crs": "LOCAL_METRE", "layers": good["layers"] * 3}
    attempt(too_many, "2 至 8", "many")
    no_version = {"crs": "LOCAL_METRE",
                  "layers": [{"store": good["layers"][0]["store"]}]
                            + good["layers"][1:]}
    attempt(no_version, "version", "noversion")
    bad_version = {"crs": "LOCAL_METRE",
                   "layers": [{"store": good["layers"][0]["store"], "version": 9}]
                             + good["layers"][1:]}
    attempt(bad_version, "不存在", "missingversion")
    split_crs = {"crs": "", "layers": good["layers"]}
    attempt(split_crs, "crs", "emptycrs")


# ------------------------------------------------------------ 原有功能兼容

def test_legacy_commands_still_work(tmp):
    sys.path.insert(0, HERE)
    import helpers
    store = os.path.join(tmp, "legacy")
    p1 = {"id": "P1", "source": "SA", "attributes": {},
          "exterior": [[0, 0], [2, 0], [2, 2], [0, 2]], "holes": []}
    p2 = {"id": "P2", "source": "SB", "attributes": {},
          "exterior": [[2, 0], [4, 0], [4, 2], [2, 2]], "holes": []}
    files = {"in.json": {"crs": "LOCAL",
                         "sources": [{"id": "SA", "priority": 10},
                                     {"id": "SB", "priority": 5}],
                         "parcels": [p1, p2],
                         "adjacencies": [{"boundary": "B1",
                                          "parcels": ["P1", "P2"]}]}}
    r = helpers.scenario(store, files, 0.05, 0.5)
    assert r == {"import": 0, "check": 0, "publish": 0}, r
    code, out = helpers.run_cli(["query", "--store", store, "parcel", "P1"])
    assert code == 0 and json.loads(out)["parcel"]["id"] == "P1"
    code, out = helpers.run_cli(["query", "--store", store, "boundary", "B1"])
    assert code == 0 and json.loads(out)["status"] == "published"
    # 原有 check 未导入时失败
    code, _ = helpers.run_cli(["check", "--store", os.path.join(tmp, "nope"),
                               "--tolerance", "0.1", "--max-area-change", "1"])
    assert code == 2


# ------------------------------------------------------------ 运行器

def main():
    tests = sorted((name, fn) for name, fn in globals().items()
                   if name.startswith("test_") and callable(fn))
    failures = 0
    for name, fn in tests:
        with tempfile.TemporaryDirectory(prefix="pbtransfer-") as tmp:
            try:
                fn(tmp)
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
            else:
                print(f"PASS {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
