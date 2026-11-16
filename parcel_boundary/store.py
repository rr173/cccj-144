"""本地存储：导入暂存、冲突检查报告、版本化原子发布、查询。

目录布局：
  <store>/
    input/input.json          导入暂存（规范化后的输入）
    staging/check_report.json 冲突检查报告（非正式输出）
    versions/v<N>/            正式版本（parcels/shared_boundaries/conflicts/
                              provenance/manifest）
    current.json              当前版本指针 {"version": N}

发布协议：先把新版本完整写入临时目录并改名就位，最后才用
os.replace 原子切换 current.json；旧版本在新结果完整之前保持可读。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any, Dict, Optional

from .model import InputData, to_canonical_json
from .reconcile import ReconcileResult


class StoreError(RuntimeError):
    pass


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


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sha256_of(obj: Any) -> str:
    return hashlib.sha256(_canonical_bytes(obj)).hexdigest()


# ---------------------------------------------------------------- 导入暂存

def stage_input(store: str, data: InputData) -> str:
    """把规范化输入写入暂存区，返回其 SHA-256。结构校验在调用前完成。"""
    canonical = to_canonical_json(data)
    digest = sha256_of(canonical)
    _write_json_atomic(os.path.join(store, "input", "input.json"), canonical)
    _write_json_atomic(os.path.join(store, "input", "meta.json"),
                       {"sha256": digest, "official": False,
                        "parcel_count": len(canonical["parcels"]),
                        "adjacency_count": len(canonical["adjacencies"])})
    return digest


def load_staged_input(store: str) -> dict:
    path = os.path.join(store, "input", "input.json")
    if not os.path.exists(path):
        raise StoreError(f"存储区 {store!r} 中没有已导入的输入，请先运行 import")
    return _read_json(path)


# ---------------------------------------------------------------- 检查报告

def write_check_report(store: str, report: dict) -> None:
    """冲突检查报告写入暂存区，明确标记为非正式输出。"""
    report = dict(report)
    report["official"] = False
    _write_json_atomic(os.path.join(store, "staging", "check_report.json"), report)


# ---------------------------------------------------------------- 发布

def _next_version(store: str) -> int:
    vdir = os.path.join(store, "versions")
    if not os.path.isdir(vdir):
        return 1
    nums = []
    for name in os.listdir(vdir):
        if name.startswith("v") and name[1:].isdigit():
            nums.append(int(name[1:]))
    return max(nums, default=0) + 1


def publish(store: str, result: ReconcileResult, *, tol: float,
            max_area_change: float, input_sha256: str) -> int:
    """把协调结果作为新版本原子发布，返回版本号。

    所有候选组检查完成后才调用本函数；本函数先把全部产物写入
    versions/v<N>（经临时目录改名就位），最后原子切换版本指针。
    """
    version = _next_version(store)
    final_dir = os.path.join(store, "versions", f"v{version}")
    tmp_dir = os.path.join(store, "versions", f".tmp-v{version}")
    if os.path.exists(tmp_dir):
        raise StoreError(f"临时目录已存在：{tmp_dir}，请先清理")
    os.makedirs(tmp_dir)

    artifacts: Dict[str, Any] = {
        "parcels.json": result.parcels_json(),
        "shared_boundaries.json": result.shared_edges_json(),
        "conflicts.json": result.conflicts_json(),
        "provenance.json": result.provenance_json(),
    }
    try:
        hashes = {}
        for name, obj in artifacts.items():
            payload = {"official": True, "version": version, "data": obj}
            _write_json_atomic(os.path.join(tmp_dir, name), payload)
            hashes[name] = sha256_of(payload)
        manifest = {
            "official": True,
            "version": version,
            "tolerance": tol,
            "max_area_change": max_area_change,
            "input_sha256": input_sha256,
            "counts": {
                "parcels": len(artifacts["parcels.json"]),
                "shared_boundaries": len(artifacts["shared_boundaries.json"]),
                "conflicts": len(artifacts["conflicts.json"]),
            },
            "artifact_sha256": hashes,
        }
        _write_json_atomic(os.path.join(tmp_dir, "manifest.json"), manifest)
        os.rename(tmp_dir, final_dir)  # 版本目录整体就位
    except BaseException:
        # 清理未完成的临时目录；旧版本与旧指针不受影响
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    _write_json_atomic(os.path.join(store, "current.json"), {"version": version})
    return version


# ---------------------------------------------------------------- 查询

def current_version(store: str) -> int:
    path = os.path.join(store, "current.json")
    if not os.path.exists(path):
        raise StoreError(f"存储区 {store!r} 尚未发布任何版本")
    return int(_read_json(path)["version"])


def _version_dir(store: str, version: Optional[int]) -> str:
    v = current_version(store) if version is None else version
    d = os.path.join(store, "versions", f"v{v}")
    if not os.path.isdir(d):
        raise StoreError(f"版本 v{v} 不存在")
    return d


def load_artifact(store: str, name: str, version: Optional[int] = None) -> Any:
    return _read_json(os.path.join(_version_dir(store, version), name))["data"]


def query_parcel(store: str, parcel_id: str, version: Optional[int] = None) -> Optional[dict]:
    """查询指定地块的当前几何与来源说明；不存在返回 None。"""
    d = _version_dir(store, version)
    parcels = _read_json(os.path.join(d, "parcels.json"))["data"]
    provenance = _read_json(os.path.join(d, "provenance.json"))["data"]
    geo_entry = next((p for p in parcels if p["id"] == parcel_id), None)
    prov_entry = next((p for p in provenance if p["parcel_id"] == parcel_id), None)
    if geo_entry is None:
        return None
    return {"parcel": geo_entry, "provenance": prov_entry}


def query_boundary(store: str, boundary_id: str, version: Optional[int] = None) -> Optional[dict]:
    """查询指定共享边目录记录；若该边界被隔离则返回其冲突记录。"""
    d = _version_dir(store, version)
    edges = _read_json(os.path.join(d, "shared_boundaries.json"))["data"]
    edge = next((e for e in edges if e["boundary_id"] == boundary_id), None)
    if edge is not None:
        return {"status": "published", "shared_edge": edge}
    conflicts = _read_json(os.path.join(d, "conflicts.json"))["data"]
    hits = [c for c in conflicts if c["boundary_id"] == boundary_id]
    if hits:
        return {"status": "conflicted", "conflicts": hits}
    return None
