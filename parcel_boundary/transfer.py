"""跨期事实转接：把旧编号事实沿发布版本链转接到新编号。

本模块只读取各版本 store 中已正式发布的 parcels.json（按该版本 manifest
记录的规范化 JSON 哈希核对），不修改源 store，不做边界协调、价格计算或
指标预测。

空间语义：一条事实的支持范围是第一层地块的有效面（洞不占面积）；在每一
层把支持范围按“覆盖该片的当前层地块集合”划分：恰一个覆盖则继续，无覆盖
记为 gap，两个以上覆盖记为 ambiguous（记录完整争议集合）。停止以土地片
段为单位，三类终点恰好分割原始有效面。整数只在全部终点桶确定后按面积
比例分配一次。

依赖：Shapely >= 2.0（GEOS）。本模块在函数内部导入 Shapely，原有
import/check/publish/query 命令不依赖它。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import geo, store as _store


FORMAT = "parcel_boundary.transfer/1"
MAX_QUANTITY = 1_000_000_000
MIN_LAYERS = 2
MAX_LAYERS = 8
# 面积/重叠对账容差：普通浮点误差；不用于丢弃任何正面积片段。
AREA_EPS_FACTOR = 1e-8


class TransferError(ValueError):
    """输入或查询输入不合法：整次运行失败，不产生/不返回看似完整的交付。"""


class ComputationError(RuntimeError):
    """几何或对账无法判明守恒：整次运行失败。"""


# ---------------------------------------------------------------- 基础工具

def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _reject_constant(s: str) -> None:
    raise TransferError(f"JSON 中出现非法数值字面量 {s}（仅允许有限十进制数）")


def _load_json_bytes(raw: bytes, label: str) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise TransferError(f"{label}: 不是合法 UTF-8：{e}") from e
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as e:
        raise TransferError(f"{label}: JSON 解析失败：{e}") from e


def _canonical_sha256(obj: Any) -> str:
    """与既有 store.manifest 的 artifact_sha256 相同的规范化 JSON 哈希。"""
    return _store.sha256_of(obj)


def _bytes_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_file(path: str, label: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise TransferError(f"{label}: 读取失败：{e}") from e


def _norm_point(v: Any, path: str) -> Tuple[float, float]:
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise TransferError(f"{path}: 坐标应为 [x, y] 二元组，得到 {v!r}")
    x, y = v
    if not _is_number(x) or not _is_number(y):
        raise TransferError(f"{path}: 坐标必须为有限十进制数，得到 {v!r}")
    return (float(x), float(y))


def _canonical_ring(points: Sequence[Tuple[float, float]], ccw: bool) -> List[Tuple[float, float]]:
    """仅用于确定性计算/输出：旋转到字典序最小顶点并统一走向。几何不变。"""
    pts = list(points)
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        raise TransferError(f"环至少需要 3 个不同顶点，得到 {len(pts)} 个")
    signed = geo.shoelace_signed(pts)
    if signed == 0.0:
        raise TransferError("环的有向面积为零（退化）")
    if (signed > 0.0) != ccw:
        pts = list(reversed(pts))
    k = min(range(len(pts)), key=lambda i: pts[i])
    return pts[k:] + pts[:k]


def _ring_from_raw(raw: Any, path: str) -> List[Tuple[float, float]]:
    if not isinstance(raw, list):
        raise TransferError(f"{path}: 环应为坐标数组")
    pts = [_norm_point(p, f"{path}[{i}]") for i, p in enumerate(raw)]
    return _canonical_ring(pts, ccw=True)


def _load_polygon(entry: dict, label: str):
    try:
        from shapely.geometry import Polygon
    except ImportError as e:  # pragma: no cover - 运行环境缺依赖
        raise TransferError("跨期事实转接需要 Shapely >= 2.0，请先安装（pip install shapely）") from e

    if "exterior" not in entry:
        raise TransferError(f"{label}: 缺少 exterior")
    exterior = _ring_from_raw(entry["exterior"], f"{label}.exterior")
    holes_raw = entry.get("holes", [])
    if not isinstance(holes_raw, list):
        raise TransferError(f"{label}.holes: 应为环数组")
    holes = []
    for k, h in enumerate(holes_raw):
        holes.append(_canonical_ring(_ring_from_raw(h, f"{label}.holes[{k}]"), ccw=False))
    try:
        geo.validate_polygon(exterior, holes, label=label)
    except geo.GeometryError as e:
        raise TransferError(str(e)) from e
    # 洞顺序不影响几何，但影响确定性：按最小顶点排序
    holes.sort(key=lambda h: (h[0], len(h), tuple(h)))
    poly = Polygon(exterior, holes)
    if (not poly.is_valid) or poly.is_empty or poly.area <= 0.0:
        raise TransferError(f"{label}: 多边形无效或面积非正（Shapely is_valid={poly.is_valid}）")
    return poly


def _polygonal_parts(geom) -> List[Any]:
    """提取几何中的正面积多边形部件，丢弃零面积的线/点残渣。"""
    if geom is None or geom.is_empty:
        return []
    gt = geom.geom_type
    if gt == "Polygon":
        return [geom] if geom.area > 0.0 else []
    if gt in ("MultiPolygon", "GeometryCollection"):
        out: List[Any] = []
        for part in geom.geoms:
            out.extend(_polygonal_parts(part))
        return out
    return []


def _polygonal_union(geoms: Iterable[Any]):
    """合并为正面积多边形部件；返回 Polygon/MultiPolygon/None。"""
    from shapely.geometry import MultiPolygon
    from shapely.ops import unary_union

    parts: List[Any] = []
    for g in geoms:
        parts.extend(_polygonal_parts(g))
    if not parts:
        return None
    merged = unary_union(parts)
    parts = _polygonal_parts(merged)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return MultiPolygon(parts)




# ---------------------------------------------------------------- 链与历史版本

@dataclass
class ChainLayer:
    index: int                 # 1-based
    selector_store: str        # chain.json 中的原样字符串
    resolved_store: str        # 相对 chain.json 所在目录解析后的路径
    version: int
    geoms: Dict[str, Any] = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)


def load_chain(path: str) -> Tuple[str, List[ChainLayer], dict]:
    raw = _read_file(path, f"链文件 {path}")
    doc = _load_json_bytes(raw, f"链文件 {path}")
    if not isinstance(doc, dict):
        raise TransferError(f"链文件 {path}: 顶层应为 JSON 对象")
    unknown = set(doc) - {"crs", "layers"}
    if unknown:
        raise TransferError(f"链文件 {path}: 含未知键 {sorted(unknown)}")
    crs = doc.get("crs")
    if not isinstance(crs, str) or not crs.strip():
        raise TransferError(f"链文件 {path}: crs 必须为非空字符串（调用方对全部层采用同一平面坐标系的声明）")
    layers_raw = doc.get("layers")
    if not isinstance(layers_raw, list):
        raise TransferError(f"链文件 {path}: layers 应为数组")
    if not (MIN_LAYERS <= len(layers_raw) <= MAX_LAYERS):
        raise TransferError(
            f"链文件 {path}: layers 含 {len(layers_raw)} 层，要求 {MIN_LAYERS} 至 {MAX_LAYERS} 层")
    chain_dir = os.path.dirname(os.path.abspath(path))
    layers: List[ChainLayer] = []
    for i, item in enumerate(layers_raw):
        lp = f"链文件 {path}: layers[{i}]"
        if not isinstance(item, dict):
            raise TransferError(f"{lp}: 应为对象")
        bad = set(item) - {"store", "version"}
        if bad:
            raise TransferError(f"{lp}: 含未知键 {sorted(bad)}")
        selector = item.get("store")
        if not isinstance(selector, str) or not selector:
            raise TransferError(f"{lp}.store: 必须为非空字符串")
        version = item.get("version")
        if not _is_int(version) or version < 1:
            raise TransferError(f"{lp}.version: 必须显式给出正整数版本号（不能省略或读取 current）")
        resolved = selector if os.path.isabs(selector) else os.path.normpath(
            os.path.join(chain_dir, selector))
        layers.append(ChainLayer(index=i + 1, selector_store=selector,
                                 resolved_store=resolved, version=version))
    chain_evidence = {
        "path": os.path.abspath(path),
        "sha256": _bytes_sha256(raw),
        "canonical_sha256": _canonical_sha256(doc),
        "crs_declaration": {
            "value": crs,
            "basis": "调用方声明所有层采用同一平面坐标系；现有发布物未保存 CRS，"
                     "本功能不声称能从旧文件自动验证或转换坐标系。长度单位为该声明"
                     "的单位，面积为单位的平方。",
        },
        "layer_count": len(layers),
    }
    return crs, layers, chain_evidence


def load_published_layer(layer: ChainLayer) -> None:
    """读取并核对一个正式版本；只读，不触碰 current.json 或暂存区。"""
    store_dir = layer.resolved_store
    vdir = os.path.join(store_dir, "versions", f"v{layer.version}")
    if not os.path.isdir(store_dir):
        raise TransferError(f"第 {layer.index} 层 store 不存在：{store_dir}")
    if not os.path.isdir(vdir):
        raise TransferError(
            f"第 {layer.index} 层版本 v{layer.version} 不存在：{store_dir}（不读取 current.json）")
    manifest_path = os.path.join(vdir, "manifest.json")
    parcels_path = os.path.join(vdir, "parcels.json")
    for p in (manifest_path, parcels_path):
        if not os.path.isfile(p):
            raise TransferError(f"第 {layer.index} 层缺少正式文件：{p}")

    manifest_raw = _read_file(manifest_path, f"第 {layer.index} 层 manifest")
    parcels_raw = _read_file(parcels_path, f"第 {layer.index} 层 parcels.json")
    manifest = _load_json_bytes(manifest_raw, f"第 {layer.index} 层 manifest")
    parcels_payload = _load_json_bytes(parcels_raw, f"第 {layer.index} 层 parcels.json")
    if not isinstance(manifest, dict):
        raise TransferError(f"第 {layer.index} 层 manifest 顶层应为对象")
    if manifest.get("official") is not True:
        raise TransferError(f"第 {layer.index} 层 manifest 未标记 official=true，拒绝使用")
    if manifest.get("version") != layer.version:
        raise TransferError(
            f"第 {layer.index} 层 manifest 版本 {manifest.get('version')!r} "
            f"与选择器 v{layer.version} 不一致")
    if not isinstance(parcels_payload, dict) or parcels_payload.get("official") is not True:
        raise TransferError(f"第 {layer.index} 层 parcels.json 未标记 official=true")
    if parcels_payload.get("version") != layer.version:
        raise TransferError(
            f"第 {layer.index} 层 parcels.json 版本 {parcels_payload.get('version')!r} "
            f"与选择器 v{layer.version} 不一致")
    declared_hashes = manifest.get("artifact_sha256")
    if not isinstance(declared_hashes, dict) or "parcels.json" not in declared_hashes:
        raise TransferError(f"第 {layer.index} 层 manifest 缺少 parcels.json 的摘要")
    declared = declared_hashes["parcels.json"]
    actual = _canonical_sha256(parcels_payload)
    if declared != actual:
        raise TransferError(
            f"第 {layer.index} 层 parcels.json 摘要不符：manifest 记录 {declared}，"
            f"实际规范化哈希 {actual}（文件可能被改动）")
    data = parcels_payload.get("data")
    if not isinstance(data, list):
        raise TransferError(f"第 {layer.index} 层 parcels.json 的 data 应为数组")
    if not data:
        raise TransferError(f"第 {layer.index} 层没有任何地块面")

    geoms: Dict[str, Any] = {}
    for k, entry in enumerate(data):
        label = f"第 {layer.index} 层 parcels.json data[{k}]"
        if not isinstance(entry, dict):
            raise TransferError(f"{label}: 应为对象")
        pid = entry.get("id")
        if not isinstance(pid, str) or not pid:
            raise TransferError(f"{label}.id: 必须为非空字符串")
        if pid in geoms:
            raise TransferError(f"第 {layer.index} 层地块标识重复：{pid!r}")
        geoms[pid] = _load_polygon(entry, f"{label}（地块 {pid!r}）")

    layer.geoms = geoms
    layer.evidence = {
        "index": layer.index,
        "selector": {"store": layer.selector_store, "version": layer.version},
        "resolved_store": os.path.abspath(store_dir),
        "version": layer.version,
        "manifest": {
            "raw_sha256": _bytes_sha256(manifest_raw),
            "canonical_sha256": _canonical_sha256(manifest),
            "official": manifest.get("official"),
            "version": manifest.get("version"),
            "tolerance": manifest.get("tolerance"),
            "max_area_change": manifest.get("max_area_change"),
        },
        "parcels": {
            "raw_sha256": _bytes_sha256(parcels_raw),
            "canonical_sha256": actual,
            "manifest_declared_sha256": declared,
            "digest_verified": True,
            "parcel_count": len(geoms),
            "path": os.path.abspath(parcels_path),
        },
    }


# ---------------------------------------------------------------- 事实文件

@dataclass
class Fact:
    row_id: str
    parcel_id: str
    quantity: int
    attributes: Any
    source_line: int

    def to_json(self) -> dict:
        return {
            "row_id": self.row_id,
            "parcel_id": self.parcel_id,
            "quantity": self.quantity,
            "attributes": self.attributes,
            "source_line": self.source_line,
        }


FACT_KEYS = {"row_id", "parcel_id", "quantity", "attributes"}


def load_facts(path: str) -> Tuple[List[Fact], dict]:
    raw = _read_file(path, f"事实文件 {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise TransferError(f"事实文件 {path}: 不是合法 UTF-8：{e}") from e
    facts: List[Fact] = []
    seen: Dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue  # 空行与末尾换行不算记录
        label = f"事实文件 {path} 第 {lineno} 行"
        try:
            obj = json.loads(line, parse_constant=_reject_constant)
        except json.JSONDecodeError as e:
            raise TransferError(f"{label}: JSON 解析失败：{e}") from e
        if not isinstance(obj, dict):
            raise TransferError(f"{label}: 每行应为 JSON 对象")
        unknown = set(obj) - FACT_KEYS
        if unknown:
            raise TransferError(f"{label}: 含未知字段 {sorted(unknown)}")
        row_id = obj.get("row_id")
        if not isinstance(row_id, str) or not row_id:
            raise TransferError(f"{label}: row_id 必须为非空字符串，得到 {row_id!r}")
        if row_id in seen:
            raise TransferError(
                f"{label}: row_id {row_id!r} 重复（首次出现于第 {seen[row_id]} 行）")
        seen[row_id] = lineno
        parcel_id = obj.get("parcel_id")
        if not isinstance(parcel_id, str) or not parcel_id:
            raise TransferError(f"{label}: parcel_id 必须为非空字符串，得到 {parcel_id!r}")
        quantity = obj.get("quantity")
        if not _is_int(quantity):
            raise TransferError(
                f"{label}: quantity 必须为整数（布尔值不算整数），得到 {quantity!r}")
        if abs(quantity) > MAX_QUANTITY:
            raise TransferError(
                f"{label}: quantity 绝对值不得超过 {MAX_QUANTITY}，得到 {quantity!r}")
        attributes = obj.get("attributes", {})
        if not isinstance(attributes, dict):
            raise TransferError(f"{label}: attributes 若给出必须为对象，得到 {attributes!r}")
        facts.append(Fact(row_id=row_id, parcel_id=parcel_id, quantity=quantity,
                          attributes=attributes, source_line=lineno))
    evidence = {
        "path": os.path.abspath(path),
        "sha256": _bytes_sha256(raw),
        "row_count": len(facts),
        "quantity_total": sum(f.quantity for f in facts),
    }
    return facts, evidence


# ---------------------------------------------------------------- 空间分类

def _claim_partition(geom, parcel_geoms: Dict[str, Any]):
    """把一片支持面按“覆盖它的当前层地块集合”分割。

    返回 [(claim_tuple, sub_geometry), ...]，claim 已排序；空 tuple 为无覆盖。
    claim 长度 1 表示唯一覆盖，>=2 为争议区。仅正面积正面积片段参与；
    边/点接触不构成覆盖。
    """
    from shapely.ops import polygonize, unary_union

    candidates: List[Tuple[str, Any]] = []
    for pid in sorted(parcel_geoms):
        g = parcel_geoms[pid]
        if not geom.intersects(g):
            continue
        inter = geom.intersection(g)
        if inter.area > 0.0:
            candidates.append((pid, g))
    if not candidates:
        return [((), geom)]

    linework_parts = [geom.boundary] + [g.boundary for _, g in candidates]
    linework = unary_union(linework_parts)
    faces = [f for f in polygonize(linework) if f.area > 0.0]
    groups: Dict[Tuple[str, ...], List[Any]] = {}
    for face in faces:
        rp = face.representative_point()
        if not geom.covers(rp):
            continue  # 面在支持面外，或落在支持面的洞里
        claim = tuple(pid for pid, g in candidates if g.covers(rp))
        groups.setdefault(claim, []).append(face)

    out: List[Tuple[Tuple[str, ...], Any]] = []
    covered_parts: List[Any] = []
    for claim in sorted(groups):
        sub = _polygonal_union(groups[claim])
        if sub is None or sub.area <= 0.0:
            continue
        out.append((claim, sub))
        covered_parts.append(sub)
    if not out:
        raise ComputationError("空间分类没有得到任何正面积片段")
    total = sum(s.area for _, s in out)
    union = _polygonal_union(covered_parts)
    union_area = union.area if union is not None else 0.0
    tol = AREA_EPS_FACTOR * max(1.0, geom.area)
    if abs(total - union_area) > tol or abs(union_area - geom.area) > tol:
        raise ComputationError(
            f"空间分类无法判明面积守恒：支持面 {geom.area!r}，"
            f"片段和 {total!r}，并集 {union_area!r}")
    return out


# ---------------------------------------------------------------- 分配与业务键

def _business_key(bucket: dict) -> tuple:
    """业务键排序：path（前缀短者先）、status、layer、claimants。"""
    return (tuple(bucket["path"]), bucket["status"], bucket["layer"],
            tuple(bucket["claimants"]))


def _allocate(quantity: int, buckets: List[dict], original_area: float) -> List[dict]:
    """在全部终点桶确定后分配一次整数；含余数决胜与精确对账。"""
    if not buckets:
        if quantity != 0:
            raise ComputationError(f"数量 {quantity} 无任何终点桶可承载")
        return []
    if not (original_area > 0.0):
        raise ComputationError("原始支持面面积非正，无法按比例分配")
    total = abs(quantity)
    quotas = [total * b["area"] / original_area for b in buckets]
    floors = [int(math.floor(q)) for q in quotas]
    remaining = total - sum(floors)
    if remaining < 0:
        raise ComputationError(f"分配下取整之和超过数量绝对值：{sum(floors)} > {total}")
    if remaining > len(buckets):
        raise ComputationError(
            f"分配剩余单位 {remaining} 超过桶数 {len(buckets)}，无法判明守恒")
    fractions = [quotas[i] - floors[i] for i in range(len(buckets))]
    order = sorted(range(len(buckets)),
                   key=lambda i: (-fractions[i], _business_key(buckets[i])))
    alloc = list(floors)
    for i in order[:remaining]:
        alloc[i] += 1
    sign = -1 if quantity < 0 else 1
    for i, b in enumerate(buckets):
        b["quantity"] = sign * alloc[i]
        b["allocation"] = {
            "method": "largest_remainder_on_absolute_quantity",
            "exact_quota_abs": quotas[i],
            "floor_abs": floors[i],
            "remainder": fractions[i],
            "rounded_up": alloc[i] > floors[i],
        }
    if sum(b["quantity"] for b in buckets) != quantity:
        raise ComputationError(
            f"分配后数量之和 {sum(b['quantity'] for b in buckets)} 不等于输入 {quantity}")
    return buckets


# ---------------------------------------------------------------- GeoJSON

def _ring_coords(ring: Sequence[Tuple[float, float]], ccw: bool) -> List[List[float]]:
    pts = _canonical_ring(ring, ccw)
    signed = geo.shoelace_signed(pts)
    if (signed > 0.0) != ccw:
        pts = list(reversed(pts))
    k = min(range(len(pts)), key=lambda i: pts[i])
    pts = pts[k:] + pts[:k]
    pts = pts + [pts[0]]  # GeoJSON 闭环
    return [[float(x), float(y)] for x, y in pts]


def geojson_geometry(geom) -> dict:
    """确定性的 Polygon/MultiPolygon GeoJSON（顶点顺序不属于业务契约）。"""
    parts = _polygonal_parts(geom)

    def poly_coords(p):
        shell = _ring_coords(list(p.exterior.coords), ccw=True)
        holes = [_ring_coords(list(h.coords), ccw=False) for h in p.interiors]
        holes.sort(key=lambda h: (h[0], len(h), h))
        return [shell] + holes

    polys = [poly_coords(p) for p in parts]
    polys.sort(key=lambda c: (c[0][0], len(c[0]), c))
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


# ---------------------------------------------------------------- 主流程

@dataclass
class RowResult:
    fact: Fact
    original_area: float
    buckets: List[dict]


def transfer_rows(facts: List[Fact], layers: List[ChainLayer]) -> List[RowResult]:
    n = len(layers)
    first = layers[0].geoms
    for fact in facts:
        if fact.parcel_id not in first:
            raise TransferError(
                f"事实文件第 {fact.source_line} 行 row_id {fact.row_id!r}: "
                f"parcel_id {fact.parcel_id!r} 不存在于第一层")
    results: List[RowResult] = []
    for fact in facts:
        support = first[fact.parcel_id]
        # (geometry, path of successful matches)
        active: List[Tuple[Any, Tuple[str, ...]]] = [(support, (fact.parcel_id,))]
        terminal: Dict[tuple, dict] = {}
        for li in range(2, n + 1):
            layer_geoms = layers[li - 1].geoms
            next_active: List[Tuple[Any, Tuple[str, ...]]] = []
            for geom, path in active:
                for claim, sub in _claim_partition(geom, layer_geoms):
                    if len(claim) == 0:
                        key = (path, "gap", li, ())
                        _add_terminal(terminal, key, sub)
                    elif len(claim) == 1:
                        next_active.append((sub, path + claim))
                    else:
                        key = (path, "ambiguous", li, claim)
                        _add_terminal(terminal, key, sub)
            active = next_active
            # 该层已停止的片段不进入后续层
        for geom, path in active:
            key = (path, "arrived", n, ())
            _add_terminal(terminal, key, geom)

        buckets: List[dict] = []
        for key in sorted(terminal):
            path, status, layer_index, claimants = key
            geom = _polygonal_union(terminal[key])
            if geom is None or geom.area <= 0.0:
                continue
            buckets.append({
                "row_id": fact.row_id,
                "attributes": fact.attributes,
                "path": list(path),
                "status": status,
                "layer": layer_index,
                "claimants": list(claimants),
                "original_area": float(support.area),
                "area": float(geom.area),
                "area_ratio": None,  # 填充分配后
                "quantity": 0,
                "geometry": geojson_geometry(geom),
            })
        buckets.sort(key=_business_key)
        original_area = float(support.area)
        sum_area = sum(b["area"] for b in buckets)
        tol = AREA_EPS_FACTOR * max(1.0, original_area)
        if abs(sum_area - original_area) > tol:
            raise ComputationError(
                f"row_id {fact.row_id!r}: 终点桶面积之和 {sum_area!r} "
                f"与原始面积 {original_area!r} 不守恒")
        for b in buckets:
            b["area_ratio"] = b["area"] / original_area
        buckets = _allocate(fact.quantity, buckets, original_area)
        results.append(RowResult(fact=fact, original_area=original_area, buckets=buckets))
    results.sort(key=lambda r: r.fact.row_id)
    return results


def _add_terminal(terminal: Dict[tuple, List[Any]], key: tuple, geom) -> None:
    terminal.setdefault(key, []).append(geom)


def _check_row_geometry(row: RowResult) -> None:
    """每行片段互不重叠、面积守恒（用并集面积对账，不静默补差）。"""
    tol = AREA_EPS_FACTOR * max(1.0, row.original_area)
    geoms = []
    from shapely.geometry import shape
    for b in row.buckets:
        geoms.append(shape(b["geometry"]))
    if not geoms:
        if row.original_area > tol:
            raise ComputationError(f"row_id {row.fact.row_id!r}: 无终点片段但原始面积为正")
        return
    sum_area = sum(g.area for g in geoms)
    union = _polygonal_union(geoms)
    union_area = union.area if union is not None else 0.0
    if abs(sum_area - union_area) > tol:
        raise ComputationError(
            f"row_id {row.fact.row_id!r}: 片段互有重叠，面积和 {sum_area!r} "
            f"不等于并集 {union_area!r}")
    if abs(union_area - row.original_area) > tol:
        raise ComputationError(
            f"row_id {row.fact.row_id!r}: 片段并集 {union_area!r} "
            f"与原始面积 {row.original_area!r} 不守恒")
    if sum(b["quantity"] for b in row.buckets) != row.fact.quantity:
        raise ComputationError(f"row_id {row.fact.row_id!r}: 整数数量未对账")


# ---------------------------------------------------------------- 交付物

def build_outputs(crs: str, chain_evidence: dict, layers: List[ChainLayer],
                  facts: List[Fact], facts_evidence: dict,
                  results: List[RowResult]) -> Dict[str, Any]:
    for row in results:
        _check_row_geometry(row)

    buckets: List[dict] = []
    rows_json: List[dict] = []
    final_map: Dict[str, dict] = {}
    status_totals = {
        "arrived": {"quantity": 0, "area": 0.0, "bucket_count": 0},
        "gap": {"quantity": 0, "area": 0.0, "bucket_count": 0},
        "ambiguous": {"quantity": 0, "area": 0.0, "bucket_count": 0},
    }
    total_area = 0.0
    total_quantity = 0

    for row in results:
        fact = row.fact
        buckets.extend(row.buckets)
        by_status: Dict[str, dict] = {}
        for status in ("arrived", "gap", "ambiguous"):
            sel = [b for b in row.buckets if b["status"] == status]
            by_status[status] = {
                "bucket_count": len(sel),
                "quantity": sum(b["quantity"] for b in sel),
                "area": sum(b["area"] for b in sel),
            }
        rows_json.append({
            "row_id": fact.row_id,
            "parcel_id": fact.parcel_id,
            "attributes": fact.attributes,
            "source_line": fact.source_line,
            "input_quantity": fact.quantity,
            "original_area": row.original_area,
            "bucket_count": len(row.buckets),
            "allocated_quantity": sum(b["quantity"] for b in row.buckets),
            "bucket_area_total": sum(b["area"] for b in row.buckets),
            "area_conserved": True,
            "fragments_non_overlapping": True,
            "by_status": by_status,
            "business_keys": [
                {"path": b["path"], "status": b["status"], "layer": b["layer"],
                 "claimants": b["claimants"]}
                for b in row.buckets
            ],
        })
        total_area += row.original_area
        total_quantity += fact.quantity
        for status in status_totals:
            status_totals[status]["quantity"] += by_status[status]["quantity"]
            status_totals[status]["area"] += by_status[status]["area"]
            status_totals[status]["bucket_count"] += by_status[status]["bucket_count"]

        for b in row.buckets:
            if b["status"] != "arrived":
                continue
            pid = b["path"][-1]
            entry = final_map.setdefault(pid, {
                "parcel_id": pid,
                "quantity": 0,
                "area": 0.0,
                "bucket_count": 0,
                "row_ids": [],
                "sources": [],
            })
            entry["quantity"] += b["quantity"]
            entry["area"] += b["area"]
            entry["bucket_count"] += 1
            if fact.row_id not in entry["row_ids"]:
                entry["row_ids"].append(fact.row_id)
            entry["sources"].append({
                "row_id": fact.row_id,
                "attributes": fact.attributes,
                "path": b["path"],
                "layer": b["layer"],
                "area": b["area"],
                "area_ratio": b["area_ratio"],
                "quantity": b["quantity"],
                "geometry": b["geometry"],
            })

    final_parcels = []
    for pid in sorted(final_map):
        entry = final_map[pid]
        entry["row_ids"].sort()
        entry["sources"].sort(key=lambda s: (s["row_id"], tuple(s["path"])))
        final_parcels.append(entry)

    output_quantity = sum(b["quantity"] for b in buckets)
    if output_quantity != total_quantity:
        raise ComputationError(
            f"汇总数量 {output_quantity} 与输入数量 {total_quantity} 不一致")
    arrived_plus_stopped = (
        status_totals["arrived"]["quantity"] + status_totals["gap"]["quantity"]
        + status_totals["ambiguous"]["quantity"])
    if arrived_plus_stopped != total_quantity:
        raise ComputationError("arrived 与停止数量之和与输入数量不一致")
    sum_bucket_area = sum(b["area"] for b in buckets)
    if abs(sum_bucket_area - total_area) > AREA_EPS_FACTOR * max(1.0, total_area):
        raise ComputationError(
            f"全部桶面积 {sum_bucket_area!r} 与全部原始面积 {total_area!r} 不守恒")

    summary = {
        "row_count": len(results),
        "bucket_count": len(buckets),
        "input_quantity": total_quantity,
        "output_quantity": output_quantity,
        "original_area_total": total_area,
        "bucket_area_total": sum_bucket_area,
        "arrived": status_totals["arrived"],
        "stopped": {
            "gap": status_totals["gap"],
            "ambiguous": status_totals["ambiguous"],
            "total": {
                "quantity": status_totals["gap"]["quantity"] + status_totals["ambiguous"]["quantity"],
                "area": status_totals["gap"]["area"] + status_totals["ambiguous"]["area"],
                "bucket_count": status_totals["gap"]["bucket_count"] + status_totals["ambiguous"]["bucket_count"],
            },
        },
        "reconciliation": {
            "input_quantity": total_quantity,
            "bucket_quantity_sum": output_quantity,
            "arrived_plus_stopped_quantity": arrived_plus_stopped,
            "quantity_conserved": output_quantity == total_quantity == arrived_plus_stopped,
            "original_area_total": total_area,
            "bucket_area_total": sum_bucket_area,
            "area_conserved": abs(sum_bucket_area - total_area)
                              <= AREA_EPS_FACTOR * max(1.0, total_area),
            "area_epsilon_factor": AREA_EPS_FACTOR,
        },
        "note": "最终地块汇总只累计 arrived；gap/ambiguous 的整数与面积单列在 stopped，"
                "不得视为成功入仓。零数量但正面积的桶仍保留。",
    }
    return {
        "buckets": {"format": FORMAT, "data": buckets},
        "rows": {"format": FORMAT, "data": rows_json},
        "final_parcels": {"format": FORMAT, "data": final_parcels},
        "summary": {"format": FORMAT, "data": summary},
        "manifest_extra": {
            "crs": crs,
            "chain": chain_evidence,
            "facts": facts_evidence,
            "layers": [layer.evidence for layer in layers],
            "summary": summary,
        },
    }


def _write_json_atomic(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run_transfer(chain_path: str, facts_path: str, out_dir: str) -> dict:
    """执行完整转接并原子创建 OUT；失败时不留下 OUT。"""
    if os.path.lexists(out_dir):
        raise TransferError(f"输出目录已存在，拒绝覆盖：{out_dir}")
    crs, layers, chain_evidence = load_chain(chain_path)
    for layer in layers:
        load_published_layer(layer)
    facts, facts_evidence = load_facts(facts_path)
    results = transfer_rows(facts, layers)
    outputs = build_outputs(crs, chain_evidence, layers, facts, facts_evidence, results)

    parent = os.path.dirname(os.path.abspath(out_dir)) or "."
    if not os.path.isdir(parent):
        raise TransferError(f"输出目录的上级目录不存在：{parent}")
    base = os.path.basename(os.path.abspath(out_dir))
    tmp_dir = tempfile.mkdtemp(dir=parent, prefix=f".{base}.tmp-")
    try:
        artifact_sources = {
            "buckets.json": outputs["buckets"],
            "rows.json": outputs["rows"],
            "final_parcels.json": outputs["final_parcels"],
            "summary.json": outputs["summary"],
        }
        artifacts = {}
        for name, payload in artifact_sources.items():
            path = os.path.join(tmp_dir, name)
            _write_json_atomic(path, payload)
            raw = _read_file(path, name)
            artifacts[name] = {
                "canonical_sha256": _canonical_sha256(payload),
                "raw_sha256": _bytes_sha256(raw),
            }
        manifest = {
            "format": FORMAT,
            "complete": True,
            "tool": {"name": "parcel_boundary", "version": _version()},
            "crs": crs,
            "chain": chain_evidence,
            "facts": facts_evidence,
            "layers": outputs["manifest_extra"]["layers"],
            "summary": outputs["manifest_extra"]["summary"],
            "artifacts": artifacts,
            "dependencies": _dependency_evidence(),
            "note": "查询只依赖本 OUT；不临时重算源版本。源 store 未被修改。",
        }
        _write_json_atomic(os.path.join(tmp_dir, "manifest.json"), manifest)
        if os.path.lexists(out_dir):
            raise TransferError(f"输出目录已存在，拒绝覆盖：{out_dir}")
        os.rename(tmp_dir, out_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return manifest


def _version() -> str:
    from . import __version__
    return __version__


def _dependency_evidence() -> dict:
    try:
        import shapely
        return {"python": ".".join(map(str, __import__("sys").version_info[:3])),
                "shapely": shapely.__version__,
                "geos": ".".join(map(str, shapely.geos_version))}
    except Exception:  # pragma: no cover
        return {"python": ".".join(map(str, __import__("sys").version_info[:3]))}


# ---------------------------------------------------------------- 查询

def _verify_out(out_dir: str) -> dict:
    mpath = os.path.join(out_dir, "manifest.json")
    if not os.path.isfile(mpath):
        raise TransferError(f"不是完整的转接输出（缺少 manifest.json）：{out_dir}")
    raw = _read_file(mpath, "manifest.json")
    manifest = _load_json_bytes(raw, "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("complete") is not True:
        raise TransferError("manifest.json 未标记 complete=true，输出不完整")
    if manifest.get("format") != FORMAT:
        raise TransferError(f"无法识别的输出格式：{manifest.get('format')!r}")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise TransferError("manifest.json 缺少 artifacts 摘要")
    for name in ("buckets.json", "rows.json", "final_parcels.json", "summary.json"):
        apath = os.path.join(out_dir, name)
        if not os.path.isfile(apath) or name not in artifacts:
            raise TransferError(f"输出不完整：缺少 {name}")
        araw = _read_file(apath, name)
        payload = _load_json_bytes(araw, name)
        if artifacts[name].get("canonical_sha256") != _canonical_sha256(payload):
            raise TransferError(f"{name} 的规范化摘要与 manifest 不符")
        if artifacts[name].get("raw_sha256") != _bytes_sha256(araw):
            raise TransferError(f"{name} 的文件摘要与 manifest 不符")
    return manifest


def _load_artifact(out_dir: str, name: str) -> Any:
    raw = _read_file(os.path.join(out_dir, name), name)
    payload = _load_json_bytes(raw, name)
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise TransferError(f"{name} 格式不正确")
    return payload.get("data")


def query_row(out_dir: str, row_id: str) -> Optional[dict]:
    _verify_out(out_dir)
    rows = _load_artifact(out_dir, "rows.json")
    buckets = _load_artifact(out_dir, "buckets.json")
    row = next((r for r in rows if r.get("row_id") == row_id), None)
    if row is None:
        return None
    mine = [b for b in buckets if b.get("row_id") == row_id]
    mine.sort(key=_business_key)
    return {
        "query": "row_id",
        "row_id": row_id,
        "parcel_id": row["parcel_id"],
        "attributes": row["attributes"],
        "input_quantity": row["input_quantity"],
        "original_area": row["original_area"],
        "allocated_quantity": row["allocated_quantity"],
        "bucket_count": row["bucket_count"],
        "by_status": row["by_status"],
        "buckets": mine,
        "explanation": (
            "quantity=0 且 area>0 的桶表示该行在该片有空间去向，但按面积比例"
            "向下取整后未分到整数；allocation 字段给出精确配额、下取整与余数。"
            if any(b.get("quantity") == 0 and b.get("area", 0) > 0 for b in mine)
            else "所有正面积桶的数量见 quantity；分配方法与余数见 allocation。"
        ),
    }


def query_parcel(out_dir: str, parcel_id: str) -> Optional[dict]:
    _verify_out(out_dir)
    final_parcels = _load_artifact(out_dir, "final_parcels.json")
    entry = next((p for p in final_parcels if p.get("parcel_id") == parcel_id), None)
    if entry is None:
        return None
    out = dict(entry)
    out["query"] = "final_parcel"
    out["sources"].sort(key=lambda s: (s["row_id"], tuple(s["path"])))
    out["note"] = "只汇总 arrived 桶；gap/ambiguous 不计入最终地块数量。"
    return out
