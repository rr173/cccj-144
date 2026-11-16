"""共享边界协调核心：匹配、基准形状选取、环替换、逐组校验。

只处理清单声明相邻的地块对；不在清单中的近邻边、洞边和外边界
不因距离接近而被吸附。每组独立判定：不合格组保留原始地块并记录
可定位的冲突原因，其余彼此无关的合格组照常处理。

本模块为纯函数式实现：相同输入（与文件/记录顺序无关）必得相同结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import geo
from .model import InputData, Parcel

# 冲突代码
UNKNOWN_PARCEL = "UNKNOWN_PARCEL"                    # 清单引用不存在的地块
SELF_ADJACENCY = "SELF_ADJACENCY"                    # 相邻对为同一地块
BOUNDARY_ID_MULTIPLE_PAIRS = "BOUNDARY_ID_MULTIPLE_PAIRS"  # 一个边界标识指向多对地块
NO_SHARED_GEOMETRY = "NO_SHARED_GEOMETRY"            # 容差内找不到重合边界（如超容差窄缝）
ENDPOINT_MISMATCH = "ENDPOINT_MISMATCH"              # 边界不能沿地块环闭合（端点对不上）
AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"                  # 配对不唯一
DEGENERATE_CHAIN = "DEGENERATE_CHAIN"                # 共享链退化（整条环都贴合/点接触）
OFFSET_BEYOND_TOLERANCE = "OFFSET_BEYOND_TOLERANCE"  # 链中部偏移超过容差
AREA_CHANGE_EXCEEDED = "AREA_CHANGE_EXCEEDED"        # 面积变化超过上限
INVALID_RESULT = "INVALID_RESULT"                    # 整理后多边形无效


@dataclass
class Conflict:
    boundary: str
    parcels: List[str]
    code: str
    message: str
    details: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "boundary_id": self.boundary,
            "parcels": sorted(self.parcels),
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class SharedEdge:
    boundary: str
    parcels: Tuple[str, str]          # 排序后的两个地块标识
    coordinates: geo.Chain            # 目录方向（起点为字典序较小端点）
    shape_source: Optional[str]       # 提供基准形状的来源标识；同优先级合并时为 None
    priority_basis: str               # "SOURCE_PRIORITY" | "EQUAL_PRIORITY_MERGE"
    parcel_orientations: Dict[str, str]  # 各地块环相对目录方向 "forward"/"backward"

    def to_json(self) -> dict:
        return {
            "boundary_id": self.boundary,
            "parcels": list(self.parcels),
            "direction": {"from": list(self.coordinates[0]),
                          "to": list(self.coordinates[-1])},
            "coordinates": [list(p) for p in self.coordinates],
            "point_count": len(self.coordinates),
            "length": geo.chain_length(self.coordinates),
            "shape_source": self.shape_source if self.shape_source else "MERGED_EQUAL_PRIORITY",
            "priority_basis": self.priority_basis,
            "parcel_orientations": dict(sorted(self.parcel_orientations.items())),
        }


@dataclass
class Edit:
    boundary: str
    ring: str                 # 目前只处理外环："exterior"
    old_chain: geo.Chain      # 被替换的原始链（含端点）
    new_chain: geo.Chain      # 写入的共享链（沿本地块环方向）

    def to_json(self) -> dict:
        return {
            "boundary_id": self.boundary,
            "ring": self.ring,
            "span": {
                "start_vertex": list(self.old_chain[0]),
                "end_vertex": list(self.old_chain[-1]),
                "old_point_count": len(self.old_chain),
                "new_point_count": len(self.new_chain),
            },
            "old_chain": [list(p) for p in self.old_chain],
            "new_chain": [list(p) for p in self.new_chain],
        }


@dataclass
class ParcelResult:
    parcel: Parcel                       # 整理后的地块（几何可能被改写）
    original_area: float
    edits: List[Edit] = field(default_factory=list)

    @property
    def final_area(self) -> float:
        return geo.polygon_area(self.parcel.exterior, self.parcel.holes)


@dataclass
class ReconcileResult:
    parcels: Dict[str, ParcelResult]     # 全部地块（含未改动的）
    shared_edges: List[SharedEdge]       # 已接受的共享边（按边界标识排序）
    conflicts: List[Conflict]            # 按 (边界标识, 代码) 排序

    def shared_edges_json(self) -> list:
        return [e.to_json() for e in self.shared_edges]

    def conflicts_json(self) -> list:
        return [c.to_json() for c in self.conflicts]

    def provenance_json(self) -> list:
        out = []
        for pid in sorted(self.parcels):
            pr = self.parcels[pid]
            out.append({
                "parcel_id": pid,
                "source": pr.parcel.source,
                "attributes": pr.parcel.attributes,
                "original_area": pr.original_area,
                "final_area": pr.final_area,
                "area_change": abs(pr.final_area - pr.original_area),
                "edits": [e.to_json() for e in pr.edits],
            })
        return out

    def parcels_json(self) -> list:
        out = []
        for pid in sorted(self.parcels):
            p = self.parcels[pid].parcel
            out.append({
                "id": p.id,
                "source": p.source,
                "attributes": p.attributes,
                "exterior": [list(pt) for pt in p.exterior],
                "holes": [[list(pt) for pt in h] for h in p.holes],
            })
        return out


# ---------------------------------------------------------------- 匹配

def _maximal_runs(flags: Sequence[bool]) -> List[List[int]]:
    """环上 True 的极大连续段（下标按环序）。全 True 返回单段含全部下标。"""
    n = len(flags)
    if all(flags):
        return [list(range(n))]
    if not any(flags):
        return []
    start = flags.index(False)
    runs: List[List[int]] = []
    cur: List[int] = []
    for k in range(1, n + 1):
        i = (start + k) % n
        if flags[i]:
            cur.append(i)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def _chain_of(ring: geo.Ring, run: Sequence[int]) -> geo.Chain:
    return [ring[i] for i in run]


def _find_candidate(ring_p: geo.Ring, ring_q: geo.Ring, tol: float):
    """在两个外环上寻找唯一的共享链配对。

    返回 (run_p, run_q, orientation)；orientation='fwd' 表示两链同向对应，
    'rev' 表示反向对应。找不到/多解时返回 (None, None, 错误代码)。
    """
    near_p = [geo.dist_point_ring(p, ring_q) <= tol for p in ring_p]
    near_q = [geo.dist_point_ring(q, ring_p) <= tol for q in ring_q]
    runs_p = _maximal_runs(near_p)
    runs_q = _maximal_runs(near_q)

    if not runs_p or not runs_q:
        return None, None, NO_SHARED_GEOMETRY
    if len(runs_p[0]) == len(ring_p) or len(runs_q[0]) == len(ring_q):
        return None, None, DEGENERATE_CHAIN

    usable_p = [r for r in runs_p if len(r) >= 2]
    usable_q = [r for r in runs_q if len(r) >= 2]
    if not usable_p or not usable_q:
        return None, None, NO_SHARED_GEOMETRY

    candidates = []
    for rp in usable_p:
        for rq in usable_q:
            sp, ep = ring_p[rp[0]], ring_p[rp[-1]]
            sq, eq = ring_q[rq[0]], ring_q[rq[-1]]
            if geo.dist(sp, sq) <= tol and geo.dist(ep, eq) <= tol:
                candidates.append((rp, rq, "fwd"))
            if geo.dist(sp, eq) <= tol and geo.dist(ep, sq) <= tol:
                candidates.append((rp, rq, "rev"))

    if not candidates:
        return None, None, ENDPOINT_MISMATCH
    if len(candidates) > 1:
        return None, None, AMBIGUOUS_MATCH
    rp, rq, orient = candidates[0]
    if geo.chain_length(_chain_of(ring_p, rp)) == 0.0 or \
       geo.chain_length(_chain_of(ring_q, rq)) == 0.0:
        return None, None, DEGENERATE_CHAIN
    return rp, rq, orient


def _replace_span(ring: geo.Ring, i0: int, i1: int, new: geo.Chain) -> geo.Ring:
    """把环上 i0..i1（含，按环序前进）替换为 new 点列。"""
    n = len(ring)
    if i0 <= i1:
        return ring[:i0] + list(new) + ring[i1 + 1:]
    return list(new) + ring[i1 + 1:i0]


def _catalog_orientation(chain: geo.Chain) -> geo.Chain:
    """目录方向规范化：起点取两端点中 (x, y) 字典序较小者。"""
    if chain[-1] < chain[0]:
        return list(reversed(chain))
    return list(chain)


# ---------------------------------------------------------------- 主流程

def reconcile(data: InputData, tol: float, max_area_change: float) -> ReconcileResult:
    if tol < 0.0 or max_area_change < 0.0:
        raise ValueError("容差与面积上限必须非负")

    parcels: Dict[str, ParcelResult] = {
        pid: ParcelResult(
            parcel=Parcel(id=p.id, source=p.source, attributes=dict(p.attributes),
                          exterior=list(p.exterior), holes=[list(h) for h in p.holes]),
            original_area=geo.polygon_area(p.exterior, p.holes),
        )
        for pid, p in data.parcels.items()
    }
    conflicts: List[Conflict] = []
    shared_edges: List[SharedEdge] = []

    # 按边界标识分组；一个边界标识只允许指向一对不同地块
    by_boundary: Dict[str, List[Tuple[str, str]]] = {}
    for a in data.adjacencies:
        by_boundary.setdefault(a.boundary, []).append((a.parcel_a, a.parcel_b))

    processable: List[Tuple[str, str, str]] = []  # (boundary, pa, pb) pa<pb
    for bid in sorted(by_boundary):
        pairs = {frozenset(pair) for pair in by_boundary[bid]}
        if len(pairs) > 1:
            conflicts.append(Conflict(
                boundary=bid,
                parcels=sorted({pid for pair in pairs for pid in pair}),
                code=BOUNDARY_ID_MULTIPLE_PAIRS,
                message=f"边界标识 {bid!r} 指向了多对地块，只允许一对不同地块",
                details={"pairs": sorted([sorted(p) for p in pairs])},
            ))
            continue
        pair = next(iter(pairs))
        if len(pair) == 1:  # 相邻对为同一地块
            only = next(iter(pair))
            conflicts.append(Conflict(
                boundary=bid, parcels=[only], code=SELF_ADJACENCY,
                message=f"边界 {bid!r} 的相邻对是同一地块 {only!r}，必须为一对不同地块",
            ))
            continue
        pa, pb = sorted(pair)
        missing = [pid for pid in (pa, pb) if pid not in parcels]
        if missing:
            conflicts.append(Conflict(
                boundary=bid, parcels=[pa, pb], code=UNKNOWN_PARCEL,
                message=f"边界 {bid!r} 的清单引用了不存在的地块：{missing}",
                details={"missing": missing},
            ))
            continue
        processable.append((bid, pa, pb))

    for bid, pa, pb in processable:
        res_a, res_b = parcels[pa], parcels[pb]
        ring_a = res_a.parcel.exterior
        ring_b = res_b.parcel.exterior

        run_a, run_b, orient = _find_candidate(ring_a, ring_b, tol)
        if run_a is None:
            conflicts.append(Conflict(
                boundary=bid, parcels=[pa, pb], code=orient,
                message=_conflict_message(bid, pa, pb, orient, tol),
                details={"tolerance": tol},
            ))
            continue

        chain_a = _chain_of(ring_a, run_a)
        chain_b_raw = _chain_of(ring_b, run_b)
        chain_b = chain_b_raw if orient == "fwd" else list(reversed(chain_b_raw))
        # 此时 chain_a 与 chain_b 同向（起点互相靠近）

        deviation = geo.chain_deviation(chain_a, chain_b)
        if deviation > tol:
            conflicts.append(Conflict(
                boundary=bid, parcels=[pa, pb], code=OFFSET_BEYOND_TOLERANCE,
                message=(f"边界 {bid!r}：两侧共享链中部最大偏差 {deviation:.6g} "
                         f"超过容差 {tol:.6g}，保留原始地块"),
                details={"tolerance": tol, "max_deviation": deviation},
            ))
            continue

        prio_a = data.sources[res_a.parcel.source].priority
        prio_b = data.sources[res_b.parcel.source].priority
        if prio_a > prio_b:
            canonical = list(chain_a)
            basis, shape_source = "SOURCE_PRIORITY", res_a.parcel.source
        elif prio_b > prio_a:
            canonical = list(chain_b)
            basis, shape_source = "SOURCE_PRIORITY", res_b.parcel.source
        else:
            if chain_a == chain_b:
                canonical = list(chain_a)
            else:
                try:
                    canonical = geo.merge_chains_equal_priority(chain_a, chain_b)
                except geo.GeometryError as e:
                    conflicts.append(Conflict(
                        boundary=bid, parcels=[pa, pb], code=DEGENERATE_CHAIN,
                        message=f"边界 {bid!r}：共享链退化，无法合并（{e}），保留原始地块",
                    ))
                    continue
            basis, shape_source = "EQUAL_PRIORITY_MERGE", None

        insert_b = canonical if orient == "fwd" else list(reversed(canonical))
        new_ring_a = _replace_span(ring_a, run_a[0], run_a[-1], canonical)
        new_ring_b = _replace_span(ring_b, run_b[0], run_b[-1], insert_b)

        # 整理后多边形必须有效
        invalid_msg = None
        for pid, new_ring, res in ((pa, new_ring_a, res_a), (pb, new_ring_b, res_b)):
            try:
                geo.validate_polygon(new_ring, res.parcel.holes, label=f"地块 {pid!r}")
            except geo.GeometryError as e:
                invalid_msg = str(e)
                break
        if invalid_msg is not None:
            conflicts.append(Conflict(
                boundary=bid, parcels=[pa, pb], code=INVALID_RESULT,
                message=f"边界 {bid!r}：整理后多边形无效（{invalid_msg}），保留原始地块",
                details={"reason": invalid_msg},
            ))
            continue

        # 每个地块的累计面积变化不得超过上限
        new_area_a = geo.polygon_area(new_ring_a, res_a.parcel.holes)
        new_area_b = geo.polygon_area(new_ring_b, res_b.parcel.holes)
        change_a = abs(new_area_a - res_a.original_area)
        change_b = abs(new_area_b - res_b.original_area)
        if change_a > max_area_change or change_b > max_area_change:
            over = pa if change_a > max_area_change else pb
            change = max(change_a, change_b)
            conflicts.append(Conflict(
                boundary=bid, parcels=[pa, pb], code=AREA_CHANGE_EXCEEDED,
                message=(f"边界 {bid!r}：地块 {over!r} 的累计面积变化 {change:.6g} "
                         f"超过上限 {max_area_change:.6g}，保留原始地块"),
                details={
                    "max_area_change": max_area_change,
                    "area_changes": {pa: change_a, pb: change_b},
                },
            ))
            continue

        # 接受：提交新环，登记共享边与来源说明
        res_a.parcel.exterior = new_ring_a
        res_b.parcel.exterior = new_ring_b
        res_a.edits.append(Edit(boundary=bid, ring="exterior",
                                old_chain=chain_a, new_chain=list(canonical)))
        res_b.edits.append(Edit(boundary=bid, ring="exterior",
                                old_chain=chain_b_raw, new_chain=list(insert_b)))

        catalog = _catalog_orientation(canonical)
        orientations = {
            pa: "forward" if list(canonical) == catalog else "backward",
            pb: "forward" if list(insert_b) == catalog else "backward",
        }
        shared_edges.append(SharedEdge(
            boundary=bid, parcels=(pa, pb), coordinates=catalog,
            shape_source=shape_source, priority_basis=basis,
            parcel_orientations=orientations,
        ))

    shared_edges.sort(key=lambda e: e.boundary)
    conflicts.sort(key=lambda c: (c.boundary, c.code))
    return ReconcileResult(parcels=parcels, shared_edges=shared_edges, conflicts=conflicts)


def _conflict_message(bid: str, pa: str, pb: str, code: str, tol: float) -> str:
    if code == NO_SHARED_GEOMETRY:
        return (f"边界 {bid!r}：地块 {pa!r} 与 {pb!r} 在容差 {tol:.6g} 内"
                f"找不到可配对的共享边界（可能存在超容差缝隙），保留原始地块")
    if code == ENDPOINT_MISMATCH:
        return (f"边界 {bid!r}：两侧候选链的端点在容差 {tol:.6g} 内无法沿地块环"
                f"闭合对应，保留原始地块")
    if code == AMBIGUOUS_MATCH:
        return f"边界 {bid!r}：共享链配对不唯一，无法确定唯一对应关系，保留原始地块"
    if code == DEGENERATE_CHAIN:
        return f"边界 {bid!r}：候选共享链退化（整条环贴合或零长度），保留原始地块"
    return f"边界 {bid!r}：匹配失败（{code}），保留原始地块"
