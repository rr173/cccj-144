"""平面几何基础：距离、折线豪斯多夫距离、鞋带公式面积、环有效性检查。

所有坐标为同一平面坐标系下的有限十进制数（float）。
长度单位 = 坐标单位；面积单位 = 坐标单位的平方。
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

Point = Tuple[float, float]
Chain = List[Point]  # 有序折线（开放）
Ring = List[Point]   # 环：不重复末点，按循环理解


class GeometryError(ValueError):
    """多边形几何无效时抛出，消息中带有可定位的原因。"""


# ---------------------------------------------------------------- 基础运算

def sub(a: Point, b: Point) -> Point:
    return (a[0] - b[0], a[1] - b[1])


def cross(o: Point, a: Point, b: Point) -> float:
    """向量 oa 与 ob 的叉积。"""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def dist_point_seg(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0.0 and dy == 0.0:
        return dist(p, a)
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def dist_point_chain(p: Point, chain: Sequence[Point]) -> float:
    """点到开放折线的最短距离。chain 至少 2 点；单点按点处理。"""
    if len(chain) == 1:
        return dist(p, chain[0])
    return min(dist_point_seg(p, chain[i], chain[i + 1]) for i in range(len(chain) - 1))


def dist_point_ring(p: Point, ring: Sequence[Point]) -> float:
    n = len(ring)
    return min(dist_point_seg(p, ring[i], ring[(i + 1) % n]) for i in range(n))


def chain_length(chain: Sequence[Point]) -> float:
    return sum(dist(chain[i], chain[i + 1]) for i in range(len(chain) - 1))


def chain_params(chain: Sequence[Point]) -> List[float]:
    """各折点的归一化弧长参数（0..1）。要求总长 > 0。"""
    total = chain_length(chain)
    if total <= 0.0:
        raise GeometryError("折线总长为零，无法参数化")
    params = [0.0]
    acc = 0.0
    for i in range(len(chain) - 1):
        acc += dist(chain[i], chain[i + 1])
        params.append(acc / total)
    params[-1] = 1.0  # 消除累计误差
    return params


def point_at_param(chain: Sequence[Point], u: float) -> Point:
    """按归一化弧长参数在折线上插值取点。"""
    total = chain_length(chain)
    if total <= 0.0:
        raise GeometryError("折线总长为零，无法插值")
    target = u * total
    acc = 0.0
    for i in range(len(chain) - 1):
        seg = dist(chain[i], chain[i + 1])
        if acc + seg >= target - 1e-15:
            t = 0.0 if seg == 0.0 else (target - acc) / seg
            t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
            return (chain[i][0] + t * (chain[i + 1][0] - chain[i][0]),
                    chain[i][1] + t * (chain[i + 1][1] - chain[i][1]))
        acc += seg
    return chain[-1]


def _sample_points(chain: Sequence[Point]) -> List[Point]:
    """折线顶点加每条边的 1/4、1/2、3/4 处采样点（确定性）。"""
    pts: List[Point] = list(chain)
    for i in range(len(chain) - 1):
        a, b = chain[i], chain[i + 1]
        for k in (1, 2, 3):
            t = k / 4.0
            pts.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
    return pts


def chain_deviation(c1: Sequence[Point], c2: Sequence[Point]) -> float:
    """两条折线间豪斯多夫距离的确定性近似：顶点 + 1/4 采样双向取最大。"""
    return max(
        max(dist_point_chain(p, c2) for p in _sample_points(c1)),
        max(dist_point_chain(p, c1) for p in _sample_points(c2)),
    )


# ---------------------------------------------------------------- 面积

def shoelace_signed(ring: Sequence[Point]) -> float:
    """鞋带公式有向面积（正=逆时针）。"""
    n = len(ring)
    s = 0.0
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def polygon_area(exterior: Sequence[Point], holes: Sequence[Sequence[Point]]) -> float:
    a = abs(shoelace_signed(exterior))
    for h in holes:
        a -= abs(shoelace_signed(h))
    return a


# ---------------------------------------------------------------- 有效性

def _on_seg(a: Point, b: Point, p: Point) -> bool:
    return (min(a[0], b[0]) - 1e-12 <= p[0] <= max(a[0], b[0]) + 1e-12
            and min(a[1], b[1]) - 1e-12 <= p[1] <= max(a[1], b[1]) + 1e-12)


def segments_intersect(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    """两线段是否相交（含端点触碰与共线重叠）。"""
    d1 = cross(p3, p4, p1)
    d2 = cross(p3, p4, p2)
    d3 = cross(p1, p2, p3)
    d4 = cross(p1, p2, p4)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    if d1 == 0.0 and _on_seg(p3, p4, p1):
        return True
    if d2 == 0.0 and _on_seg(p3, p4, p2):
        return True
    if d3 == 0.0 and _on_seg(p1, p2, p3):
        return True
    if d4 == 0.0 and _on_seg(p1, p2, p4):
        return True
    return False


def ring_simplicity_error(ring: Sequence[Point]) -> str | None:
    """返回 None 表示环简单；否则返回可定位的错误描述。"""
    n = len(ring)
    if n < 3:
        return f"环只有 {n} 个顶点（至少 3 个）"
    for i in range(n):
        if ring[i] == ring[(i + 1) % n]:
            return f"边 {i} 为零长边（连续重复顶点 {ring[i]}）"
    if abs(shoelace_signed(ring)) == 0.0:
        return "环的有向面积为零（退化）"
    for i in range(n):
        a1, a2 = ring[i], ring[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue  # 相邻边共享端点，跳过
            b1, b2 = ring[j], ring[(j + 1) % n]
            if segments_intersect(a1, a2, b1, b2):
                return f"边 {i}（{a1}->{a2}）与边 {j}（{b1}->{b2}）相交"
    return None


def point_location(p: Point, ring: Sequence[Point]) -> str:
    """点相对环的位置：'inside' / 'boundary' / 'outside'（射线法）。"""
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        if cross(a, b, p) == 0.0 and _on_seg(a, b, p):
            return "boundary"
    inside = False
    x, y = p
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xin > x:
                inside = not inside
    return "inside" if inside else "outside"


def validate_polygon(exterior: Sequence[Point],
                     holes: Sequence[Sequence[Point]],
                     label: str = "多边形") -> None:
    """校验多边形有效性，无效时抛 GeometryError（消息可定位）。"""
    err = ring_simplicity_error(exterior)
    if err:
        raise GeometryError(f"{label} 外环无效：{err}")
    for k, h in enumerate(holes):
        err = ring_simplicity_error(h)
        if err:
            raise GeometryError(f"{label} 第 {k} 个洞无效：{err}")
        for v in h:
            loc = point_location(v, exterior)
            if loc != "inside":
                raise GeometryError(f"{label} 第 {k} 个洞的顶点 {v} 不在外环内部（{loc}）")
        m = len(h)
        for i in range(m):
            a1, a2 = h[i], h[(i + 1) % m]
            n = len(exterior)
            for j in range(n):
                b1, b2 = exterior[j], exterior[(j + 1) % n]
                if segments_intersect(a1, a2, b1, b2):
                    raise GeometryError(
                        f"{label} 第 {k} 个洞的边 {i} 与外环边 {j} 相交")
        for k2 in range(k + 1, len(holes)):
            h2 = holes[k2]
            if point_location(h2[0], h) == "inside" or point_location(h[0], h2) == "inside":
                raise GeometryError(f"{label} 第 {k} 个洞与第 {k2} 个洞相互包含")
            m2 = len(h2)
            for i in range(m):
                a1, a2 = h[i], h[(i + 1) % m]
                for j in range(m2):
                    b1, b2 = h2[j], h2[(j + 1) % m2]
                    if segments_intersect(a1, a2, b1, b2):
                        raise GeometryError(
                            f"{label} 第 {k} 个洞的边 {i} 与第 {k2} 个洞的边 {j} 相交")


def merge_chains_equal_priority(c1: Chain, c2: Chain) -> Chain:
    """同优先级两侧坐标不一致时的确定性合并。

    两条链按归一化弧长参数化，在双方折点参数的并集上逐点取中点。
    结果只取决于两条链本身，与输入记录顺序无关。
    """
    p1 = chain_params(c1)
    p2 = chain_params(c2)
    us = sorted(set(p1) | set(p2))
    out: Chain = []
    for u in us:
        a = point_at_param(c1, u)
        b = point_at_param(c2, u)
        out.append(((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0))
    return out
