# 灌区多源用水事实数据、指标发布与计费双时间对账平台

面向灌区「六套系统、多源口径」问题建设的数据治理平台。平台**不替代**轮灌调度、闸门控制与农户通知系统，
只提供经治理的数据集、质量事件、版本化指标和查询接口；在其之上建设**用水计费事实双时间对账**能力——
解释一条差额来自哪一段水量、哪版价目和哪次归属变更，而**不是收款或退款功能**。核心选择是：

> **保留追加式事实账和可回放指标版本**，而不是只维护一张当前读数表，也不因缺测而停发全部指标；
> 计费侧**按计量区间生成追加式分摊事实并冻结账期快照**，而不是按经营户当前累计量在查询时重算，
> 从而在转包或补传发生后仍能复现当时的应计依据。

技术栈：**React 18 + Vite（前端）｜ Python 3.12 + FastAPI（后端）｜ PostgreSQL（追加式/SCD2 存储）**。

---

## 1. 它解决什么问题

| 痛点 | 平台机制 |
| --- | --- |
| 报表把计划量当实灌量 | 计划只生成 `fact_kind=plan / status=target` 的目标事实，**永不充当实灌**；实灌须由读数证据编译 |
| 设备补传悄悄改写昨天数字 | 补传写入**新报文行**并挂 `supersedes` 链，旧行原样保留；事实与指标重算生成**新版本 + 差异说明** |
| 累计表换表后读数倒退 | 有凭证→换表前后拆两段；**无凭证→截断区间、关联渠道量列待核实，绝不解释成负供水** |
| 设备补传与人工补录冲突 | 两份证据都保留，各生成一条 `candidate` 候选事实并登记裁定；**未裁定前不进正式指标** |
| 渠道上下游对不上 | 超 5% 容差即**冻结损耗指标并生成对账任务**，禁止用计划量补平 |
| 数字无法追溯 | 血缘浏览器从发布指标回溯到原始报文、换表记录、映射版本、人工裁定、质量规则、对账任务 |

### 质量规则引擎覆盖

`READING_REVERSAL` 读数倒退 · `METER_CHANGE_NO_VOUCHER` / `METER_CHANGE_TRUNCATION` 换表缺证与截断 ·
`UNIT_MISMATCH` 单位不一致 · `TIME_OVERLAP` 时间重叠 · `CLOCK_DRIFT` 时钟漂移 · `GAP_MISSING` 缺测 ·
`CHANNEL_IMBALANCE` 渠道守恒失衡 · `EVIDENCE_CONFLICT` 证据冲突 · `MANUAL_WITHOUT_BASIS` 人工补录缺依据 ·
`LATE_ARRIVAL` 迟到补传 · `CONTRACT_VIOLATION` 契约违规。

---

## 2. 目录结构

```
23/
├── backend/
│   ├── app/
│   │   ├── models.py            # 追加式/SCD2 ORM 模型（计量治理）
│   │   ├── models_billing.py    # 计费对账 ORM：归属沿革/价目/外部账务/片段/快照/匹配/差异桥/调整候选
│   │   ├── schemas.py           # Pydantic 入参出参（计量治理）
│   │   ├── schemas_billing.py   # 计费对账 Pydantic 入参出参
│   │   ├── services/
│   │   │   ├── ingestion.py     # 契约登记 + 暂存接入（原样留痕、幂等、补传链）
│   │   │   ├── master_data.py   # 水系/地块/批次/SCD2 映射/换表
│   │   │   ├── fact_compiler.py # 事实编译器（差量/积分/截断/候选/目标分离）
│   │   │   ├── quality.py       # 质量规则引擎 + 守恒对账冻结
│   │   │   ├── metrics.py       # 指标版本服务（水位线/稳定期/差异说明）
│   │   │   ├── lineage.py       # 血缘浏览器
│   │   │   ├── workflow.py      # 人工裁定与对账处置
│   │   │   ├── ledger.py        # 追加式账本期视图（多值取代链）
│   │   │   ├── billing_common.py    # 计费公共工具（UID/账期键/账单指纹）
│   │   │   ├── billing_master.py    # 经营户/归属沿革/价目版本
│   │   │   ├── billing_external.py  # 外部账务事实接入（无支付动作）
│   │   │   ├── billing_compiler.py  # 区间分摊编译器（跨租约/跨价目/双候选）
│   │   │   ├── billing_snapshot.py  # 账期快照器（冻结不可变）
│   │   │   ├── billing_matcher.py   # 回执匹配器（重复/缺失/冲突）
│   │   │   ├── billing_bridge.py    # 差异桥接器 + 冻结后调整候选
│   │   │   ├── billing_review.py    # 审阅投影器（最小披露）+ 差额回查
│   │   │   └── billing_ledger.py    # 片段账活动视图与版本链
│   │   ├── routers/             # FastAPI 路由（含 billing.py：19 个计费对账接口路径）
│   │   └── main.py              # 应用入口（生产模式托管前端 dist）
│   ├── scripts/seed.py          # 计量治理样例（实际触发四类治理场景）
│   ├── scripts/seed_billing.py  # 计费对账样例（五组验收 + 跨价目待证，含硬性断言）
│   ├── tests/                   # pytest：治理硬规则 + 计费对账硬规则 + API 端到端
│   └── requirements.txt
├── frontend/                    # React 18 + Vite + React Router
│   └── src/pages/               # 9 个治理页面 + 4 个计费对账页面
├── docker/entrypoint.sh
├── Dockerfile                   # 多阶段构建（前端构建 + 后端运行时）
├── docker-compose.yml           # 一键启动（PostgreSQL + 应用，自动建表/种子）
└── run_local.sh                 # 本机（预装运行时）一键启动
```

---

## 3. 一键启动（Docker Compose，推荐）

```bash
docker compose up --build
# 启动后访问
#   前端 + API 同源：  http://localhost:8000/
#   交互式 API 文档：  http://localhost:8000/docs
```

`entrypoint.sh` 会自动：等待数据库就绪 → 建表 → 写入样例数据（含迟到补传、换表缺证、渠道失衡、证据冲突）。
重新开始：`docker compose down -v && docker compose up --build`。

### 测试账号

| 账号 | 密码 | 角色 |
| --- | --- | --- |
| `admin` | `admin123` | 灌区管理员（可人工裁定、对账处置、触发发布） |
| `viewer` | `viewer123` | 运行查看员 |

> 本平台为数据治理后端 + 展示前端，轮灌调度/闸门控制/农户通知仍在既有业务系统，故内置的是演示级登录。

---

## 4. 本地开发启动（容器/裸机已预装 Python、Node、PostgreSQL 时）

```bash
# 后端
cd backend
/opt/python-3.12/bin/python3 -m venv ../.venv
../.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
cp .env.example .env                      # 按需修改数据库连接
../.venv/bin/python -m scripts.seed       # 建表 + 计量治理样例
../.venv/bin/python -m scripts.seed_billing  # 计费双时间对账五组样例（含断言）
../.venv/bin/uvicorn app.main:app --reload --port 8000

# 前端（另开终端，开发模式带 /api 代理）
cd frontend
/opt/node-22/bin/npm install --registry=https://registry.npmmirror.com
# 若本机 npm 日志目录不可写导致安装中断，可追加
#   --cache /tmp/npm-cache --logs-dir /tmp/npm-logs
/opt/node-22/bin/npm run dev              # http://localhost:5173
```

或直接执行根目录 `./run_local.sh`（自动建虚拟环境、建库、写种子、起后端并托管已构建前端）。

---

## 5. 验证方式

### 5.1 自动化测试

```bash
cd backend
# 测试库（与开发库隔离）
PGPASSWORD=ccdev /opt/postgresql-18/bin/psql -h 127.0.0.1 -U ccdev -d postgres \
  -c "CREATE DATABASE irrigation_test OWNER ccdev;" 2>/dev/null || true
../.venv/bin/python -m pytest tests/ -v
```

测试覆盖（`tests/test_governance.py`、`tests/test_api.py`、`tests/test_billing.py`，共 15 项）：

1. **读数倒退永不产生负供水**，倒退区间落 `pending_verification` 并出质量事件；
2. **换表无凭证即截断**，生成两条 `truncated` 待核实事实；
3. **证据冲突双候选、未裁定不进指标**，人工裁定采信设备后重编译转为确认；
4. **渠道失衡冻结损耗指标且不用计划补平**（`loss_lower/upper` 置空、指标 `frozen`）；
5. **迟到补传生成指标新版本并写差异说明**，旧报文与旧版本均保留；
6. API 端到端：登录、契约登记、幂等接入、编译、发布、血缘链路、版本回放；
7. **计费对账硬规则**：跨阶梯三档计价、跨租约时间比例拆段、跨价目缺读数阻断→补证确认、
   重复回执幂等 vs 同号冲突并列、冻结后补传只出调整候选且快照不变、归属双候选裁定、
   审阅最小披露、差异桥三列、差额五类回查，以及计费 API 全链路（含「无任何资金接口」断言）。

### 5.2 计费对账样例（种子脚本已实际触发并内置断言）

```bash
cd backend
../.venv/bin/python -m scripts.seed_billing   # 末尾输出「全部断言通过」
```

脚本实际落地五组验收样例与一组跨价目待证（见 7.4 表格），运行结束打印 7 条 `断言 ✓`：
跨阶梯 202 元、跨租约两任经营户各 100m³、重复回执两种结论、冻结快照复演不变、
人工证据双候选金额范围、跨价目阻断转确认、差额五类回查。可在前端四个计费页面逐项核对。

### 5.3 计量治理样例手工验证（种子脚本已实际触发）

种子叙事日期为 **2026-11-10（UTC）**，运行 `python -m scripts.seed` 后可在界面核对：

| 场景 | 触发方式 | 期望可见结果 |
| --- | --- | --- |
| 迟到补传 | 渠首 `D-HEAD-01` 末日读数 600 于 11-12 补传为 620 | 暂存区两条报文（rev1/rev2，旧行保留）；`LATE_ARRIVAL` 事件；MAIN 渠道指标出现新版本，差异说明「确认实灌量: 600.0 -> 620.0」 |
| 换表缺证 | `D-B1-01` 02:30 换表、新表首读数 5、无凭证 | `METER_CHANGE_NO_VOUCHER` + `METER_CHANGE_TRUNCATION`；两条截断待核实事实（待核实 55m³）；无任何负值 |
| 渠道失衡 | 渠首确认 620，两支渠可解释仅 506.5（差异率 18.31% > 5%） | 对账任务 `frozen`；MAIN 损耗区间冻结为「冻结/不适用」；质量事件 `CHANNEL_IMBALANCE` |
| 证据冲突 | `D-B2-01` 01:00–02:00 同时有设备补传与人工补录 | 两条 `candidate` 事实 + 一条待裁定记录；未裁定前确认量不含该区间；裁定并重编译后才转正 |
| 附带规则 | 单位错填 `L`、05:00 缺测、03:00 时钟偏快、无依据人工补录 | `UNIT_MISMATCH`、`GAP_MISSING`、`CLOCK_DRIFT`、`MANUAL_WITHOUT_BASIS` 各 1 条 |

**血缘验证**：在「指标版本」页任选指标点「血缘」，可看到 `发布指标 → 供水事实 → 原始报文/人工证据 → 数据契约/映射版本/换表记录/人工裁定/质量事件/对账任务` 的完整有向图（样例 MAIN 指标约 9 类节点、40+ 条边），每个节点可点开查看留痕字段。

**追溯闭环操作路径**：
`质量事件与裁定` 页裁定冲突 → `供水事实账` 点「重新编译事实」→ `指标版本` 点「发布新版本」→
查看新版本差异说明 → `对账与重算` 页处置对账任务 → 任一版本「血缘」回看全部输入与规则。

---

## 6. 关键设计说明

- **追加式事实账**：`supply_facts` 行永不物理删除或原地改写。重算时新事实通过
  `supersedes_fact_id`（单值，兼容链）与 `replaces_fact_ids`（JSONB 多值，覆盖双候选场景）取代旧事实；
  指标 `metric_versions` 同理，旧版本以 `superseded_by` 指向新版本并保留 `diff_note`。
- **SCD2 主数据**：设备→计量点映射、地块→批次分配均带 `effective_from/effective_to` 与 `mapping_version`，
  事实行固化当时的映射版本号，血缘可按版本回放。
- **事件时间 / 水位线 / 稳定期**：指标按事件时间归集，水位线 = 发布时刻 − 允许迟到时长（15 分钟），
  稳定期 30 分钟；含迟到、缺测、待核实、未裁定候选的指标标 `tentative`，被对账冻结的标 `frozen`。
- **损耗区间**：`[max(渠首−支渠−待核实,0), max(渠首−支渠,0)]`，待核实带来区间不确定性；渠道冻结时整体置空。
- **缺测不停发**：缺测只折算 `missing_rate` 并产生事件，其余指标照常发布并标暂定状态。
- **计费双时间**：归属沿革同时保存业务有效区间（`effective_from/to`）与登记系统时间（`recorded_at`），
  转包回溯登记也只追加新行；分摊片段固化 `tenure_version` 与 `price_version_no`，
  因此任一片额都能回答「当时依据哪版归属、哪版价目」。
- **快照即复演凭证**：`billing_snapshot_items` 保存引用行的完整 JSON 副本而非只存外键，
  冻结后主数据再变更也不影响快照；冻结后的修订经调整候选体现，原快照永不回改。
- **指纹与幂等分离**：账单行指纹刻意排除回执号，使「同号重传」与「同号内容冲突」可被区分；
  匹配/差异桥是派生工作记录，重跑只替换未绑定确认快照的部分，冻结结果随快照保留。

## 7. 用水计费事实双时间对账

财务侧的痛点是：同一经营户的水量事实、价目目录和外部账务回执常在不同日期更正——流量计补传改变计费区间、
经营权转包改变用水归属、外部系统又可能把同一调整回执重传两次。本模块只做**对账解释**，
**不发起收款、退款、抵扣、争议冻结或任何资金状态迁移，也不调用任何支付接口**。

### 7.1 四本追加式 / SCD2 账

| 账 | 表 | 关键纪律 |
| --- | --- | --- |
| 原始计量账 | 复用 `raw_messages` / `manual_revisions` / `supply_facts` | 每段读数的发生区间、接收时间、设备与人工来源 |
| 归属沿革账 | `tenure_records` | 经营户-地块-租约的双时间 SCD2：业务有效区间 + 登记系统时间；转包只新开区间，不改历史行 |
| 价目版本账 | `price_versions` / `price_tiers` / `price_relief_rules` | 阶梯、季节价、减免规则按**批准版本**整体保存，支持全局/地块专用作用域 |
| 外部账务事实账 | `external_billing_entries` | 只接入账单行、冲补行、回执号、状态；`entry_uid` 幂等，原样留痕 |

### 7.2 五个能力器（`backend/app/services/billing_*.py`）

1. **区间分摊编译器** `billing_compiler.py`：把计量区间按归属沿革与价目生效点拆成可解释的 `billing_fragments`
   片段，每段固化归属版本、价目版本、阶梯单价与减免；重算以版本链（`supersedes_fragment_id` /
   `replaces_fragment_ids`）取代旧片段，旧行保留。补入中间读数后，自动剔除被细区间事实完整覆盖的粗区间事实，
   避免重复计价。
2. **账期快照器** `billing_snapshot.py`：冻结某次对账所见的计量、归属、价目版本，逐行保存 JSON 副本
   （`billing_snapshot_items`）；含待证/候选时状态为 `blocked` 禁止确认，全部可解释才允许 `confirmed`。
3. **回执匹配器** `billing_matcher.py`：以外部回执号、账单行指纹（刻意排除回执号）和金额方向识别
   幂等重传、并列冲突、双向缺失。
4. **差异桥接器** `billing_bridge.py`：分三列列示「应计量口径 `metering_accrual` / 外部账务事实
   `external_fact` / 后补修订影响 `late_revision`」；冻结后的修订只生成引用原快照的 `adjustment_candidates`。
5. **审阅投影器** `billing_review.py`：仅输出地块、账期、差异来源与必要证据指针（版本号/回执号/快照号），
   不暴露经营户姓名等无关身份信息；`trace` 接口按具体差额回查五类依据。

### 7.3 四条硬规则（均有自动化测试与种子断言守护）

- **跨价目缺中间读数不猜测**：一条累计读数跨越两个价目版本却缺边界有效读数时，发布
  `cross_price_pending` 的 `blocked_pending` 片段并阻止进入确认对账；补齐边界读数后重编译才按两版价目确认。
- **同号异指纹不算幂等**：外部回执号重复但金额方向或账单行指纹不同，两条记录并列标为 `conflict`，
  绝不把后一条当幂等重传；只有同号、同指纹、同方向才判 `duplicate_idempotent`。
- **冻结后修订只出调整候选**：已确认快照之后到达的人工读数更正/迟到补传，只生成引用原快照的调整候选，
  原账期快照明细与金额保持不可变、可复演。
- **归属矛盾双候选并列**：转包生效日与人工补录读数区间矛盾时，`attribution_conflicts` 同时展示
  「沿革归属」与「人工主张」两套候选及金额范围，凭证裁定前不进确认对账。

### 7.4 五组验收样例（`scripts/seed_billing.py`，叙事账期 2026-11）

| # | 样例 | 地块 | 期望结果（种子内有断言） |
| --- | --- | --- | --- |
| 1 | 跨阶梯 | PLOT-T1 | 260m³ 跨三档：100×0.5 + 100×0.8 + 60×1.2 = **202 元**，出现三档片段 |
| 2 | 跨租约 | PLOT-T2 | 一条区间跨 02:00 转包点，无中间读数按时间比例各 100m³ 拆给两任经营户，合计 130 元 |
| 3 | 重复回执 | PLOT-T3 | 同号同指纹=幂等重传；同号异金额（异指纹）=并列冲突；另含一条冲补行与经营户减免 |
| 4 | 冻结后补传 | PLOT-T4 | 快照先冻结 100m³/50 元；补传 100→130 后原快照不变，只出 +30m³/+24 元调整候选 |
| 5 | 人工证据冲突 | PLOT-T5 | 双归属候选与金额范围并列，未裁定不进确认；裁定后重编译转确认 |
| 6 | 跨价目待证（硬规则补充） | PLOT-T6 | 缺边界读数先阻断；补 02:00=60 读数后按旧价 0.4 / 新价 0.6 分别确认 |

**任一差额都能回查**：计量区间（含原始报文/人工证据）→ 归属版本 → 价目版本 → 账期快照 → 外部回执号，
见 `GET /api/billing/trace`。

### 7.5 前端页面（导航新增四项）

「计费分摊片段」（阻断/候选/确认与回查抽屉）·「对账审阅投影」（最小披露 + 差异桥三列）·
「快照·回执·差异桥」（冻结、匹配、调整候选、归属裁定工作台）·「计费三本账」（归属/价目/外部账务）。

## 8. 主要 HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | 演示登录 |
| POST/GET | `/api/contracts` | 数据契约登记/查询 |
| POST/GET | `/api/ingestion/raw`、`/api/ingestion/manual` | 设备报文/人工修订接入（幂等、原样留痕） |
| POST/GET | `/api/master/*` | 渠道、计量点、地块、批次、SCD2 映射、换表登记 |
| POST | `/api/compile` | 供水事实编译 + 质量规则全量检查 |
| GET | `/api/facts`、`/api/quality/events` | 事实账、质量事件 |
| GET/POST | `/api/adjudications` | 证据冲突裁定 |
| GET/POST | `/api/reconciliations/{id}/resolve` | 渠道对账任务与处置 |
| POST/GET | `/api/metrics/publish`、`/api/metrics` | 指标版本发布/查询 |
| GET | `/api/metrics/{id}/versions`、`/api/metrics/{id}/lineage` | 版本回放与血缘图 |
| GET | `/api/recompute-runs` | 重算任务登记 |
| POST/GET | `/api/billing/operators`、`/api/billing/tenures` | 经营户与归属沿革（双时间 SCD2） |
| POST/GET | `/api/billing/price-versions` | 价目批准版本（阶梯/季节/减免） |
| POST/GET | `/api/billing/external-entries` | 外部账务事实接入（账单/冲补/回执，无支付动作） |
| POST/GET | `/api/billing/compile`、`/api/billing/fragments` | 区间分摊编译与片段账 |
| POST/GET | `/api/billing/snapshots`、`/api/billing/snapshots/{id}/confirm`、`/items` | 账期快照冻结/确认/副本 |
| POST/GET | `/api/billing/matching`、`/api/billing/receipt-matches` | 回执匹配与结论 |
| POST/GET | `/api/billing/bridge` | 差异桥三列 |
| POST/GET | `/api/billing/adjustments/scan`、`/api/billing/adjustments`、`/{id}/decide` | 冻结后调整候选 |
| GET/POST | `/api/billing/attribution-conflicts`、`/{id}/decide` | 归属冲突与凭证裁定 |
| GET | `/api/billing/review`、`/api/billing/trace` | 审阅投影（最小披露）与差额五类回查 |

完整 schema 见运行后的 `http://localhost:8000/docs`（FastAPI 自动生成的 OpenAPI）。
全平台不存在 `/payment`、`/refund`、`/settle`、`/fund` 等任何资金动作路径。
