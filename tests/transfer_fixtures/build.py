"""跨期事实转接验收夹具：用真实 import/check/publish 入口生成已发布地块版本。

每个 store 是独立目录，只含 versions/v1（正式发布物）。
所有几何均为手写矩形/直角多边形；预期面积、路径与整数分配在测试中手算，
不由生产转接函数生成。坐标单位为任意长度单位，面积为其平方。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def run_cli(*argv, check=True):
    env = dict(os.environ, PYTHONPATH=ROOT)
    r = subprocess.run([sys.executable, "-m", "parcel_boundary"] + list(argv),
                       capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"命令失败：{argv}\nstdout={r.stdout}\nstderr={r.stderr}")
    return r


def publish_store(store, parcels, crs="LOCAL_METRE"):
    """parcels: [{"id","exterior","holes"?,"attributes"?}]，全部归入来源 S。

    返回 versions/v1 目录。reconcile 未声明任何相邻边，几何原样发布
    （仅做导入时的环走向规范化），因此发布面与手写输入几何一致。
    """
    os.makedirs(store, exist_ok=True)
    doc = {
        "crs": crs,
        "sources": [{"id": "S", "priority": 1}],
        "parcels": [
            {"id": p["id"], "source": "S", "attributes": p.get("attributes", {}),
             "exterior": p["exterior"], "holes": p.get("holes", [])}
            for p in parcels
        ],
        "adjacencies": [],
    }
    inp = os.path.join(store, "_source_input.json")
    write_json(inp, doc)
    run_cli("import", "--store", store, inp)
    run_cli("check", "--store", store, "--tolerance", "0", "--max-area-change", "0")
    run_cli("publish", "--store", store, "--tolerance", "0", "--max-area-change", "0")
    os.unlink(inp)
    return os.path.join(store, "versions", "v1")


def rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def write_chain_and_facts(case_dir, layers, facts, crs="LOCAL_METRE"):
    os.makedirs(case_dir, exist_ok=True)
    chain = {"crs": crs, "layers": [
        {"store": store, "version": 1} for store in layers]}
    write_json(os.path.join(case_dir, "chain.json"), chain)
    with open(os.path.join(case_dir, "facts.jsonl"), "w", encoding="utf-8") as f:
        for fact in facts:
            f.write(json.dumps(fact, ensure_ascii=False, sort_keys=True) + "\n")


# ---------------------------------------------------------------- 用例 1
def case1(dirpath):
    """合并再拆分 + 同编号不同位置。

    第一层 A=[0,2]x[0,1]、B=[2,4]x[0,1]、Z=[10,11]x[0,1]；
    第二层 M=[0,4]x[0,1]；第三层 X=[0,2]x[0,1]、Y=[2,4]x[0,1]，
    并有与第一层同编号的 Z=[20,21]x[0,1]（不同位置）。
    """
    old = os.path.join(dirpath, "old-store")
    mid = os.path.join(dirpath, "survey-store")
    new = os.path.join(dirpath, "new-store")
    publish_store(old, [
        {"id": "A", "exterior": rect(0, 0, 2, 1)},
        {"id": "B", "exterior": rect(2, 0, 4, 1)},
        {"id": "Z", "exterior": rect(10, 0, 11, 1)},
    ])
    publish_store(mid, [{"id": "M", "exterior": rect(0, 0, 4, 1)}])
    publish_store(new, [
        {"id": "X", "exterior": rect(0, 0, 2, 1)},
        {"id": "Y", "exterior": rect(2, 0, 4, 1)},
        {"id": "Z", "exterior": rect(20, 0, 21, 1)},
    ])
    write_chain_and_facts(dirpath, [old, mid, new], [
        {"row_id": "F_A", "parcel_id": "A", "quantity": 10,
         "attributes": {"category": "lamp"}},
        {"row_id": "F_B", "parcel_id": "B", "quantity": 6,
         "attributes": {"category": "lamp"}},
        {"row_id": "F_ID_REUSE", "parcel_id": "Z", "quantity": 2},
    ])


# ---------------------------------------------------------------- 用例 2
def case2(dirpath):
    """一排拆分：S 面积 10，末层三桶面积 3/3/4。"""
    old = os.path.join(dirpath, "old-store")
    mid = os.path.join(dirpath, "survey-store")
    new = os.path.join(dirpath, "new-store")
    publish_store(old, [{"id": "S", "exterior": rect(0, 0, 10, 1)}])
    publish_store(mid, [
        {"id": "B", "exterior": rect(0, 0, 6, 1)},
        {"id": "C", "exterior": rect(6, 0, 10, 1)},
    ])
    publish_store(new, [
        {"id": "T", "exterior": rect(0, 0, 3, 1)},
        {"id": "U", "exterior": rect(3, 0, 6, 1)},
        {"id": "V", "exterior": rect(6, 0, 10, 1)},
    ])
    write_chain_and_facts(dirpath, [old, mid, new], [
        {"row_id": "POS", "parcel_id": "S", "quantity": 1},
        {"row_id": "NEG", "parcel_id": "S", "quantity": -1},
    ])


# ---------------------------------------------------------------- 用例 3a
def case3a(dirpath):
    """中间层缺口 [4,6]，末层覆盖整片；缺口仍停在第二层。"""
    old = os.path.join(dirpath, "old-store")
    mid = os.path.join(dirpath, "survey-store")
    new = os.path.join(dirpath, "new-store")
    publish_store(old, [{"id": "S", "exterior": rect(0, 0, 10, 1)}])
    publish_store(mid, [
        {"id": "G1", "exterior": rect(0, 0, 4, 1)},
        {"id": "G2", "exterior": rect(6, 0, 10, 1)},
    ])
    publish_store(new, [{"id": "ALL", "exterior": rect(0, 0, 10, 1)}])
    write_chain_and_facts(dirpath, [old, mid, new], [
        {"row_id": "GAPROW", "parcel_id": "S", "quantity": 4},
    ])


# ---------------------------------------------------------------- 用例 3b
def case3b(dirpath):
    """中间层正常/两重/三重/未覆盖；末层覆盖整片。

    第二层 P=[0,2]、Q=[2,6]、R=[4,8]、T=[5,7]（均 y∈[0,1]）。
    """
    old = os.path.join(dirpath, "old-store")
    mid = os.path.join(dirpath, "survey-store")
    new = os.path.join(dirpath, "new-store")
    publish_store(old, [{"id": "S", "exterior": rect(0, 0, 10, 1)}])
    publish_store(mid, [
        {"id": "P", "exterior": rect(0, 0, 2, 1)},
        {"id": "Q", "exterior": rect(2, 0, 6, 1)},
        {"id": "R", "exterior": rect(4, 0, 8, 1)},
        {"id": "T", "exterior": rect(5, 0, 7, 1)},
    ])
    publish_store(new, [{"id": "ALL", "exterior": rect(0, 0, 10, 1)}])
    write_chain_and_facts(dirpath, [old, mid, new], [
        {"row_id": "MIX", "parcel_id": "S", "quantity": 20},
    ])


# ---------------------------------------------------------------- 用例 4
def case4(dirpath):
    """带洞源面 + 下一层洞造成的新缺口 + 同业务键不连通片段。

    区域一（x∈[0,10]）：
      第一层 S=[0,10]² 去掉洞 [6,8]²（面积 96）；
      第二层 E=[0,10]² 去掉洞 [2,4]²（面积 96）；
      第三层 N=[0,10]² 覆盖整片。
      E 的洞在 S 内造成 4 平方单位缺口，第三层再覆盖也不能接回。
    区域二（x∈[20,26]）：
      第一层 S2=[20,26]x[0,1]（面积 6）；
      第二层 M=[20,26]x[0,1]；
      第三层 W 为梳形：上带 [20,26]x[1,2]，两齿 [20,21.5]x[0,1] 与
        [23,24.5]x[0,1]。
        W∩M 为 [20,21.5]x[0,1] 与 [23,24.5]x[0,1] 两片不连通片段（合计 3），
        同属业务键 (['S2','M','W'], arrived, 3, ())，须合成一个桶；
        [21.5,23]x[0,1] 与 [24.5,26]x[0,1] 无覆盖（合计 3），同属
        (['S2','M'], gap, 3, ())，也须合成一个桶并在两处中断。
      数量 5 时两桶配额均为 2.5，余数并列；按业务键决胜：path
        ('S2','M') 是 ('S2','M','W') 的前缀，短序列在前，故 gap 桶得 3、
        arrived 桶得 2，与几何组件顺序无关。
    """
    old = os.path.join(dirpath, "old-store")
    mid = os.path.join(dirpath, "survey-store")
    new = os.path.join(dirpath, "new-store")
    publish_store(old, [
        {"id": "S", "exterior": rect(0, 0, 10, 10),
         "holes": [rect(6, 6, 8, 8)]},
        {"id": "S2", "exterior": rect(20, 0, 26, 1)},
    ])
    publish_store(mid, [
        {"id": "E", "exterior": rect(0, 0, 10, 10),
         "holes": [rect(2, 2, 4, 4)]},
        {"id": "M", "exterior": rect(20, 0, 26, 1)},
    ])
    publish_store(new, [
        {"id": "N", "exterior": rect(0, 0, 10, 10)},
        {"id": "W", "exterior": [[20, 0], [21.5, 0], [21.5, 1], [23, 1],
                                 [23, 0], [24.5, 0], [24.5, 1], [26, 1],
                                 [26, 2], [20, 2]]},
    ])
    write_chain_and_facts(dirpath, [old, mid, new], [
        {"row_id": "HOLES", "parcel_id": "S", "quantity": 96},
        {"row_id": "SPLIT", "parcel_id": "S2", "quantity": 5,
         "attributes": {"category": "bench"}},
    ])


# ---------------------------------------------------------------- 用例 5
def case5(dirpath):
    """多行同地块不同属性、零数量、余数并列与业务键决胜。

    第一层 S=[0,6]x[0,1]（面积 6）。
    第二层 M=[0,6]x[0,1]。
    第三层：
      A=[1,3]x[0,1]、B=[1,3]x[0,1]（完全重叠 -> 两重争议，claimants A,B）
      C=[3,4]x[0,1]、D=[3,4]x[0,1]（完全重叠 -> 两重争议，claimants C,D）
      E=[4,6]x[0,1]（唯一覆盖 -> arrived）
      [0,1]x[0,1] 无覆盖 -> gap
    数量 3 时配额 0.5 / 1.0 / 0.5 / 1.0：
      下取整 0+1+0+1=2，余 1 单位；gap 与 amb(C,D) 小数余数并列 0.5，
      按业务键 gap 的 status "gap" > "ambiguous"，单位给 amb(C,D)。
    """
    old = os.path.join(dirpath, "old-store")
    mid = os.path.join(dirpath, "survey-store")
    new = os.path.join(dirpath, "new-store")
    publish_store(old, [{"id": "S", "exterior": rect(0, 0, 6, 1)}])
    publish_store(mid, [{"id": "M", "exterior": rect(0, 0, 6, 1)}])
    publish_store(new, [
        {"id": "A", "exterior": rect(1, 0, 3, 1)},
        {"id": "B", "exterior": rect(1, 0, 3, 1)},
        {"id": "C", "exterior": rect(3, 0, 4, 1)},
        {"id": "D", "exterior": rect(3, 0, 4, 1)},
        {"id": "E", "exterior": rect(4, 0, 6, 1)},
    ])
    write_chain_and_facts(dirpath, [old, mid, new], [
        {"row_id": "R1", "parcel_id": "S", "quantity": 3,
         "attributes": {"category": "lamp"}},
        {"row_id": "R2", "parcel_id": "S", "quantity": 0,
         "attributes": {"category": "bench", "note": "重排后逐行对账"}},
        {"row_id": "R3", "parcel_id": "S", "quantity": -3,
         "attributes": {"category": "lamp"}},
    ])


CASES = {
    "case1": case1,
    "case2": case2,
    "case3a": case3a,
    "case3b": case3b,
    "case4": case4,
    "case5": case5,
}


def build(name, dirpath):
    CASES[name](dirpath)
    return dirpath


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("case", choices=sorted(CASES))
    ap.add_argument("dir")
    args = ap.parse_args()
    build(args.case, args.dir)
    print(f"{args.case} -> {args.dir}")
