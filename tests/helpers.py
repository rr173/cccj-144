"""测试辅助：独立于生产代码的核对工具。

这里的鞋带公式、链查找、拓扑检查都是为测试单独写的简单实现，
不用生产修复函数生成或验证预期结果。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from parcel_boundary import cli  # noqa: E402


# ------------------------------------------------------------ 独立几何核对

def shoelace_signed(ring) -> float:
    """测试自写的鞋带公式（独立实现）。"""
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def polygon_area(exterior, holes=()) -> float:
    a = abs(shoelace_signed(exterior))
    for h in holes:
        a -= abs(shoelace_signed(h))
    return a


def approx(a: float, b: float, eps: float = 1e-9) -> bool:
    return abs(a - b) <= eps


def ring_approx_eq(r1, r2, eps: float = 1e-9) -> bool:
    if len(r1) != len(r2):
        return False
    return all(approx(p[0], q[0], eps) and approx(p[1], q[1], eps)
               for p, q in zip(r1, r2))


def find_chain_span(ring, chain):
    """在环（循环理解）中精确查找链作为连续段出现的起点下标列表。

    精确相等（坐标来自同一份发布物，JSON 往返后 float 不变）。
    """
    n = len(ring)
    m = len(chain)
    hits = []
    if m > n:
        return hits
    for s in range(n):
        if all(ring[(s + k) % n] == chain[k] for k in range(m)):
            hits.append(s)
    return hits


def check_shared_edge_topology(parcels_json, edge, max_area_change,
                               provenance_by_id, eps=1e-9):
    """独立拓扑确认：共享段在两侧环中逐点反向相等、方向相反。"""
    coords = [tuple(p) for p in edge["coordinates"]]
    pa, pb = edge["parcels"]
    rings = {p["id"]: [tuple(pt) for pt in p["exterior"]] for p in parcels_json}
    orientations = {}
    for pid in (pa, pb):
        ring = rings[pid]
        fwd = find_chain_span(ring, coords)
        rev = find_chain_span(ring, list(reversed(coords)))
        assert len(fwd) + len(rev) == 1, \
            f"共享边 {edge['boundary_id']} 在地块 {pid} 的环中应唯一出现（正/反向）"
        orientations[pid] = "forward" if fwd else "backward"
    assert orientations[pa] != orientations[pb], \
        f"共享边 {edge['boundary_id']} 两侧环引用方向必须相反：{orientations}"
    assert edge["parcel_orientations"] == orientations, \
        f"共享边 {edge['boundary_id']} 目录登记的方向与实际环不一致"


def check_ring_basics(parcel, eps=1e-9):
    """环闭合（循环理解）、顶点数、面积为正。"""
    ring = parcel["exterior"]
    assert len(ring) >= 3, f"地块 {parcel['id']} 外环顶点不足"
    assert ring[0] != ring[-1], f"地块 {parcel['id']} 外环不应重复末点"
    assert polygon_area(ring, parcel.get("holes", [])) > eps, \
        f"地块 {parcel['id']} 面积应为正"


# ------------------------------------------------------------ 运行入口

def run_cli(argv):
    """进程内调用 CLI，返回退出码（吞掉输出）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def scenario(store, files, tol, max_area, do_publish=True):
    """写输入文件 -> import -> check -> publish，返回各步退出码。"""
    os.makedirs(store, exist_ok=True)
    paths = []
    for name, obj in files.items():
        p = os.path.join(store, name)
        write_json(p, obj)
        paths.append(p)
    code_import, out_import = run_cli(["import", "--store", store] + paths)
    if code_import != 0:
        return {"import": code_import, "import_out": out_import}
    code_check, _ = run_cli(["check", "--store", store,
                             "--tolerance", str(tol),
                             "--max-area-change", str(max_area)])
    result = {"import": code_import, "check": code_check}
    if do_publish:
        code_pub, _ = run_cli(["publish", "--store", store,
                               "--tolerance", str(tol),
                               "--max-area-change", str(max_area)])
        result["publish"] = code_pub
    return result


def load_artifact(store, name, version=1):
    path = os.path.join(store, "versions", f"v{version}", name)
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    return obj.get("data", obj)  # manifest.json 无 data 包装


def artifact_bytes(store, name, version=1):
    path = os.path.join(store, "versions", f"v{version}", name)
    with open(path, "rb") as f:
        return f.read()


def parcels_by_id(parcels_json):
    return {p["id"]: p for p in parcels_json}


def edges_by_id(edges_json):
    return {e["boundary_id"]: e for e in edges_json}


def conflicts_by_id(conflicts_json):
    out = {}
    for c in conflicts_json:
        out.setdefault(c["boundary_id"], []).append(c)
    return out
