"""输入数据模型：结构校验、规范化、合并多个输入文件。

结构错误（JSON 畸形、缺字段、非有限坐标、多边形无效、标识重复等）
导致整次运行失败：抛 StructuralError，不生成任何看似完整的正式输出。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List

from . import geo


class StructuralError(ValueError):
    """输入结构错误：整次运行失败。"""


@dataclass(frozen=True)
class Source:
    id: str
    priority: float


@dataclass
class Parcel:
    id: str
    source: str
    attributes: Dict[str, Any]
    exterior: geo.Ring
    holes: List[geo.Ring] = field(default_factory=list)


@dataclass(frozen=True)
class Adjacency:
    boundary: str
    parcel_a: str
    parcel_b: str


@dataclass
class InputData:
    crs: str | None
    sources: Dict[str, Source]
    parcels: Dict[str, Parcel]
    adjacencies: List[Adjacency]


def _fail(path: str, msg: str) -> None:
    raise StructuralError(f"{path}: {msg}")


def _reject_constant(s: str) -> None:
    raise StructuralError(f"JSON 中出现非法数值字面量 {s}（仅允许有限十进制数）")


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _load_json_file(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f, parse_constant=_reject_constant)
    except json.JSONDecodeError as e:
        raise StructuralError(f"文件 {path}: JSON 解析失败：{e}") from e
    except OSError as e:
        raise StructuralError(f"文件 {path}: 读取失败：{e}") from e


def _as_str(v: Any, path: str) -> str:
    if not isinstance(v, str) or not v:
        _fail(path, f"应为非空字符串，得到 {v!r}")
    return v


def _as_point(v: Any, path: str) -> geo.Point:
    if (not isinstance(v, (list, tuple))) or len(v) != 2:
        _fail(path, f"坐标应为 [x, y] 二元组，得到 {v!r}")
    x, y = v
    if not _is_number(x) or not _is_number(y):
        _fail(path, f"坐标必须为有限十进制数，得到 {v!r}")
    return (float(x), float(y))


def _as_ring(v: Any, path: str) -> geo.Ring:
    if not isinstance(v, list):
        _fail(path, "环应为坐标数组")
    pts = [_as_point(p, f"{path}[{i}]") for i, p in enumerate(v)]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]  # 规范化：去掉重复的闭合点
    if len(pts) < 3:
        _fail(path, f"环至少需要 3 个不同顶点，得到 {len(pts)} 个")
    return pts


def _normalize_orientation(ring: geo.Ring, want_ccw: bool) -> geo.Ring:
    """统一环走向（外环逆时针、洞顺时针）。

    仅反转顶点顺序、保持首顶点不变；几何形状与面积完全不变。
    规范化后，相邻地块（均为有效简单多边形）的环必然以相反方向
    引用同一条共享边。
    """
    signed = geo.shoelace_signed(ring)
    if signed == 0.0:
        return ring  # 退化环由后续有效性检查报错
    if (signed > 0) != want_ccw:
        return [ring[0]] + list(reversed(ring[1:]))
    return list(ring)


def load_inputs(paths: List[str]) -> InputData:
    """读取并合并若干输入文件，做完整结构校验。

    每个文件是一个 JSON 对象，可含键：
      "crs"         字符串（可选；多文件不一致视为结构错误）
      "sources"     [{"id": str, "priority": number}]
      "parcels"     [{"id","source","attributes"?,"exterior","holes"?}]
      "adjacencies" [{"boundary": str, "parcels": [id, id]}]
    """
    crs: str | None = None
    sources: Dict[str, Source] = {}
    parcels: Dict[str, Parcel] = {}
    adjacencies: List[Adjacency] = []
    seen_adj_records: set = set()

    for path in paths:
        doc = _load_json_file(path)
        if not isinstance(doc, dict):
            _fail(f"文件 {path}", "顶层应为 JSON 对象")
        unknown = set(doc) - {"crs", "sources", "parcels", "adjacencies"}
        if unknown:
            _fail(f"文件 {path}", f"含未知键 {sorted(unknown)}")

        if "crs" in doc:
            c = _as_str(doc["crs"], f"文件 {path} 的 crs")
            if crs is not None and crs != c:
                _fail(f"文件 {path}", f"坐标系声明不一致：{crs!r} 与 {c!r}")
            crs = c

        for key in ("sources", "parcels", "adjacencies"):
            if key in doc and not isinstance(doc[key], list):
                _fail(f"文件 {path}", f"{key} 应为数组")

        for i, s in enumerate(doc.get("sources") or []):
            sp = f"文件 {path} sources[{i}]"
            if not isinstance(s, dict):
                _fail(sp, "应为对象")
            sid = _as_str(s.get("id"), f"{sp}.id")
            pr = s.get("priority")
            if not _is_number(pr):
                _fail(f"{sp}.priority", f"优先级应为有限数值，得到 {pr!r}")
            if sid in sources:
                _fail(sp, f"来源标识重复：{sid!r}")
            sources[sid] = Source(id=sid, priority=float(pr))

        for i, p in enumerate(doc.get("parcels") or []):
            pp = f"文件 {path} parcels[{i}]"
            if not isinstance(p, dict):
                _fail(pp, "应为对象")
            pid = _as_str(p.get("id"), f"{pp}.id")
            src = _as_str(p.get("source"), f"{pp}.source")
            attrs = p.get("attributes", {})
            if not isinstance(attrs, dict):
                _fail(f"{pp}.attributes", "应为对象")
            if "exterior" not in p:
                _fail(pp, "缺少 exterior")
            exterior = _normalize_orientation(
                _as_ring(p["exterior"], f"{pp}.exterior"), want_ccw=True)
            holes_raw = p.get("holes", [])
            if not isinstance(holes_raw, list):
                _fail(f"{pp}.holes", "应为环数组")
            holes = [_normalize_orientation(_as_ring(h, f"{pp}.holes[{k}]"), want_ccw=False)
                     for k, h in enumerate(holes_raw)]
            if pid in parcels:
                _fail(pp, f"地块标识重复：{pid!r}")
            parcels[pid] = Parcel(id=pid, source=src, attributes=dict(attrs),
                                  exterior=exterior, holes=holes)

        for i, a in enumerate(doc.get("adjacencies") or []):
            ap = f"文件 {path} adjacencies[{i}]"
            if not isinstance(a, dict):
                _fail(ap, "应为对象")
            bid = _as_str(a.get("boundary"), f"{ap}.boundary")
            pair = a.get("parcels")
            if (not isinstance(pair, list)) or len(pair) != 2:
                _fail(f"{ap}.parcels", "应为两个地块标识的数组")
            pa = _as_str(pair[0], f"{ap}.parcels[0]")
            pb = _as_str(pair[1], f"{ap}.parcels[1]")
            key = (bid, frozenset((pa, pb)))
            if key in seen_adj_records:
                _fail(ap, f"相邻记录重复声明：边界 {bid!r} 地块对 ({pa!r}, {pb!r})")
            seen_adj_records.add(key)
            adjacencies.append(Adjacency(boundary=bid, parcel_a=pa, parcel_b=pb))

    if not parcels:
        raise StructuralError("输入不含任何地块")
    for pid, p in parcels.items():
        if p.source not in sources:
            raise StructuralError(f"地块 {pid!r} 引用了未声明的来源 {p.source!r}")
    # 输入多边形必须有效，否则整次运行失败
    for pid in sorted(parcels):
        p = parcels[pid]
        try:
            geo.validate_polygon(p.exterior, p.holes, label=f"地块 {pid!r}")
        except geo.GeometryError as e:
            raise StructuralError(str(e)) from e

    return InputData(crs=crs, sources=sources, parcels=parcels, adjacencies=adjacencies)


def from_canonical_json(obj: dict) -> InputData:
    """从规范化表示重建 InputData（用于读取暂存输入）。"""
    sources = {s["id"]: Source(id=s["id"], priority=float(s["priority"]))
               for s in obj.get("sources", [])}
    parcels = {}
    for p in obj.get("parcels", []):
        parcels[p["id"]] = Parcel(
            id=p["id"], source=p["source"], attributes=dict(p.get("attributes", {})),
            exterior=[tuple(pt) for pt in p["exterior"]],
            holes=[[tuple(pt) for pt in h] for h in p.get("holes", [])],
        )
    adjacencies = [Adjacency(boundary=a["boundary"],
                             parcel_a=a["parcels"][0], parcel_b=a["parcels"][1])
                   for a in obj.get("adjacencies", [])]
    return InputData(crs=obj.get("crs"), sources=sources,
                     parcels=parcels, adjacencies=adjacencies)


def to_canonical_json(data: InputData) -> dict:
    """规范化表示：排序后用于存档与哈希，保证与文件/记录顺序无关。"""
    return {
        "crs": data.crs,
        "sources": [
            {"id": s.id, "priority": s.priority}
            for s in sorted(data.sources.values(), key=lambda s: s.id)
        ],
        "parcels": [
            {
                "id": p.id,
                "source": p.source,
                "attributes": p.attributes,
                "exterior": [list(pt) for pt in p.exterior],
                "holes": [[list(pt) for pt in h] for h in p.holes],
            }
            for p in sorted(data.parcels.values(), key=lambda p: p.id)
        ],
        "adjacencies": [
            {"boundary": a.boundary, "parcels": sorted([a.parcel_a, a.parcel_b])}
            for a in sorted(data.adjacencies,
                            key=lambda a: (a.boundary, min(a.parcel_a, a.parcel_b),
                                           max(a.parcel_a, a.parcel_b)))
        ],
    }
