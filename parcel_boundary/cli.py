"""命令行入口：import / check / publish / query。

  import   导入并校验输入，写入暂存区（结构错误则整次失败）
  check    检查冲突：对暂存输入跑完整协调，报告写入暂存区（非正式）
  publish  发布：全部候选组检查完成后，把结果原子发布为新版本
  query    查询当前（或指定）版本中的地块 / 共享边

容差 --tolerance：长度单位，与坐标系一致（坐标为米则为米）。
面积上限 --max-area-change：面积单位，坐标单位的平方（如平方米）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import model, store
from .reconcile import reconcile


def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _non_negative(name: str):
    def parse(s: str) -> float:
        try:
            v = float(s)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} 必须为有限十进制数：{s!r}")
        import math
        if not math.isfinite(v) or v < 0.0:
            raise argparse.ArgumentTypeError(f"{name} 必须为非负有限数：{s!r}")
        return v
    return parse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="parcel_boundary",
        description="地块边界整理：把已声明相邻关系中的共享边界校成一致几何。")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("import", help="导入并校验输入文件，写入暂存区")
    pi.add_argument("--store", required=True, help="本地存储目录")
    pi.add_argument("files", nargs="+", help="输入 JSON 文件（可多个，合并校验）")

    def add_run_args(sp):
        sp.add_argument("--store", required=True, help="本地存储目录")
        sp.add_argument("--tolerance", required=True, type=_non_negative("容差"),
                        help="边界匹配容差，长度单位，与坐标系一致（如米）")
        sp.add_argument("--max-area-change", required=True,
                        type=_non_negative("面积上限"),
                        help="每个地块允许的最大面积变化，面积单位（如平方米）")

    pc = sub.add_parser("check", help="检查冲突（结果写入暂存区，非正式）")
    add_run_args(pc)

    pp = sub.add_parser("publish", help="发布正式结果（原子切换新版本）")
    add_run_args(pp)

    pq = sub.add_parser("query", help="查询指定地块 / 共享边")
    pq.add_argument("--store", required=True, help="本地存储目录")
    pq.add_argument("--version", type=int, default=None, help="版本号（默认当前版本）")
    pq.add_argument("kind", choices=["parcel", "boundary"], help="查询对象类型")
    pq.add_argument("id", help="地块标识或共享边界标识")
    return p


def _load_staged(store_dir: str) -> model.InputData:
    return model.from_canonical_json(store.load_staged_input(store_dir))


def _cmd_import(args) -> int:
    data = model.load_inputs(args.files)
    digest = store.stage_input(args.store, data)
    print(f"导入完成：{len(data.parcels)} 个地块，{len(data.adjacencies)} 条相邻记录，"
          f"{len(data.sources)} 个来源；输入 SHA-256 = {digest}")
    return 0


def _run(data: model.InputData, tol: float, max_area: float):
    return reconcile(data, tol, max_area)


def _cmd_check(args) -> int:
    data = _load_staged(args.store)
    result = _run(data, args.tolerance, args.max_area_change)
    report = {
        "tolerance": args.tolerance,
        "max_area_change": args.max_area_change,
        "accepted_shared_boundaries": result.shared_edges_json(),
        "conflicts": result.conflicts_json(),
        "summary": {
            "accepted": len(result.shared_edges_json()),
            "conflicts": len(result.conflicts_json()),
        },
    }
    store.write_check_report(args.store, report)
    print(f"检查完成（非正式，已写入暂存区）：可修复边界组 {report['summary']['accepted']} 个，"
          f"冲突 {report['summary']['conflicts']} 个")
    for c in result.conflicts_json():
        print(f"  冲突 {c['code']} 边界 {c['boundary_id']} "
              f"地块 {c['parcels']}：{c['message']}")
    return 0


def _cmd_publish(args) -> int:
    data = _load_staged(args.store)
    input_sha = store.sha256_of(model.to_canonical_json(data))
    result = _run(data, args.tolerance, args.max_area_change)
    version = store.publish(args.store, result, tol=args.tolerance,
                            max_area_change=args.max_area_change,
                            input_sha256=input_sha)
    print(f"已发布版本 v{version}：地块 {len(result.parcels)} 个，"
          f"共享边 {len(result.shared_edges_json())} 条，"
          f"冲突 {len(result.conflicts_json())} 个（已隔离，保留原始地块）")
    return 0


def _cmd_query(args) -> int:
    if args.kind == "parcel":
        out = store.query_parcel(args.store, args.id, args.version)
    else:
        out = store.query_boundary(args.store, args.id, args.version)
    if out is None:
        print(f"未找到 {args.kind} {args.id!r}", file=sys.stderr)
        return 1
    _print_json(out)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "import":
            return _cmd_import(args)
        if args.command == "check":
            return _cmd_check(args)
        if args.command == "publish":
            return _cmd_publish(args)
        if args.command == "query":
            return _cmd_query(args)
    except (model.StructuralError, store.StoreError) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
