# 跨期事实转接（parcel_boundary transfer）

本功能在既有 `parcel_boundary` 中增加一条下游入口：把按**旧地块编号**登记的
可加总事实（如设施数量及其冲销记录）沿指定版本链转接到新编号，并留下每笔
事实经过哪些地块、在哪里失去唯一归属的空间血缘。

它只读取已正式发布的地块版本，不重新协调共享边、不改源 store、不计算地价或
预测指标。

## 依赖安装

需要 Python 3.9+ 与 Shapely 2.x（内含 GEOS）：

```bash
python3 -m pip install "shapely>=2.0"
```

原有 `import/check/publish/query` 命令只依赖标准库，无需 Shapely；`transfer`
与 `query row / final-parcel` 才需要。

## 运行方法

```bash
# 转接：读取链中每层的正式发布物，生成全新 OUT 目录
python -m parcel_boundary transfer --chain chain.json --facts facts.jsonl --out OUT

# 按原始 row_id 查询该行的全部去向（桶、路径、停止层、争议集合、数量与分配）
python -m parcel_boundary query --out OUT row F17

# 按最终地块标识查询其全部来源及加总数量（只累计 arrived）
python -m parcel_boundary query --out OUT final-parcel X
```

`OUT` 必须是尚不存在的目录；已存在则拒绝覆盖并以非零退出。输入或计算失败
不会留下 `OUT`（先写同目录临时目录，全部完成才对账后整体改名就位）。

## 链文件

```json
{"crs":"LOCAL_METRE","layers":[
  {"store":"./old-store","version":1},
  {"store":"./survey-store","version":2},
  {"store":"./new-store","version":1}
]}
```

- `layers` 含 2 至 8 层，**顺序即转接顺序**，不根据版本号或地块编号猜先后。
- `store` 相对 `chain.json` 所在目录解释；`version` 必须显式给出，绝不读取
  `current.json`。
- `crs` 是调用方对全部层采用同一平面坐标系的非空声明。现有发布物未保存 CRS，
  本功能不声称能从旧文件自动验证或转换坐标系；长度单位随此声明，面积为平方。
- 每层读取 `versions/v<N>/parcels.json`，并用该层 `manifest.json` 记录的
  `artifact_sha256["parcels.json"]`（规范化 JSON 哈希，与既有发布算法一致）
  核对；缺文件、摘要不符、`official` 标记缺失、无效面或非有限坐标均整次失败，
  不静默修几何，也不拿 staging 数据替代历史发布物。

## 事实文件

每行一个 JSON 对象：

```json
{"row_id":"F17","parcel_id":"P_old","quantity":7,"attributes":{"category":"lamp"}}
```

- `row_id`、`parcel_id` 为非空字符串，`row_id` 全文件唯一；
  `parcel_id` 必须存在于第一层。
- `quantity` 为绝对值不超过十亿的**有符号整数**（布尔值不算整数）；负数为冲销，
  与正数遵循相同空间去向。
- `attributes` 可选，原样保留；不同行即使属性或地块相同也绝不合并。
- 空文件允许；未知地块、重复 `row_id`、坏 JSON、非法字段整批失败并指出源行。

## 业务语义

一条事实的第一层地块有效面（洞不占面积）是其支持范围，事实在其中均匀分布。
从第二层起逐层判断每片土地：

- 恰一个地块覆盖：血缘路径追加该地块，继续下一层。
- 无地块覆盖：该层停止，记为 `gap`；同一片土地以后重新出现也不接回。
- 两个及以上地块覆盖：该层停止，记为 `ambiguous`，记录完整且排序后的争议
  地块集合；边或点接触不算正面积交叠。三重交叠与两重交叠分别成键。

停止以**土地片段**为单位，一条原始地块可同时有到达、缺口与争议部分。
三类终点恰好分割原始有效面。身份由“层号 + 地块标识”联合确定；编号相等不
代替几何判断。

## 交付文件

- `buckets.json`：逐行转接桶。业务键 `(path, status, layer, claimants)`；
  相同键的不连通片段合并为一个桶（MultiPolygon），不按组件分别舍入。
  每桶含 `row_id`、原始属性、原始/桶面积、面积比例、整数数量、分配明细
  （精确配额、下取整、余数）与 Polygon/MultiPolygon GeoJSON。
- `rows.json`：逐原始行对账摘要（各状态数量与面积、业务键、面积守恒标志）。
- `final_parcels.json`：最终地块入仓汇总——只累计 `arrived`，逐来源列出
  row_id、路径、面积与数量；`gap/ambiguous` 单列不进此表。
- `summary.json`：运行汇总与对账（输入数量 = arrived + gap + ambiguous、
  桶面积和 = 原始面积和）。
- `manifest.json`：原始事实文件摘要、链选择器与链文件摘要、每层 manifest 与
  `parcels.json` 的原始/规范化摘要及核对结果、CRS 声明、产物摘要、依赖版本。

查询只依赖 `OUT`：先按 manifest 校验各产物摘要，再作答，不临时重算源版本。
把源 store 移走后查询仍可用。

## 整数分配

只在所有终点桶确定后分一次：对 `quantity` 的绝对值按桶面积占该行原始面积的
比例取配额，先向下取整，剩余单位按小数余数从大到小补齐，再恢复原符号。
余数并列时按业务键升序破同：先比较 `path` 中各标识的 Unicode 字典序（前缀
相同则短序列在前），再比较 `status`、整数 `layer`、`claimants` 序列。几何组件
顺序不参与决胜。各桶整数之和严格等于输入，最终汇总加停止数量也必须对账。

## 面积与守恒

面积用 Shapely 在声明坐标系下计算，允许普通浮点误差；对账阈值
`1e-8 × max(1, 原始面积)`，只用于判定，不用于丢弃任何正面积片段或补差。
输出前检查每行片段互不重叠（面积和等于并集面积）且并集面积等于原始面积；
不守恒则整次失败，不生成“貌似成功”的 OUT。

## 验收

```bash
python3 tests/run_transfer_tests.py   # 跨期转接验收（真实命令入口）
python3 tests/run_tests.py           # 原有 23 个用例（兼容性）
```

夹具见 `tests/transfer_fixtures/build.py`，全部用真实 `import/check/publish`
生成历史版本；预期面积、路径与整数分配均为手算，不由生产转接函数生成。
覆盖：

1. 合并后再拆分（A→M→X，B→M→Y，不因中间合并各分一半）；同编号不同位置
   证明编号不构成空间联系。
2. 一排拆分面积 3/3/4，数量 1 只给 V；数量 -1 同桶得 -1。
3. 中间层缺口在末层重新覆盖仍停在中间层；两重/三重/未覆盖各自的面积表。
4. 带洞源面、下一层洞造成的新缺口、同业务键不连通片段合成一个桶。
5. 多行同地块不同属性、零数量、余数并列、空事实文件与重排逐行对账。
6. 坏事实与被篡改的历史 `parcels.json` 非零退出且无 OUT；恢复后成功，
   源 store 字节未变。

## 已知限制

- 不校验覆盖地块彼此之间的重叠是否合法，重叠正是本功能要识别的归属问题。
- 不做坐标转换；`crs` 仅为声明，面积单位为其平方。
- 不支持数据库、服务或并发作业系统。
