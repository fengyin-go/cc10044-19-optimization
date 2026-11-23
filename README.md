# 湖仓一体任务编排与数据治理平台

面向数据湖和数据仓库的ETL任务编排、分区管理、数据血缘和质量治理的工程平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/views/SnapshotRestoreConsole.vue  快照恢复核对台（值班专用）
│   ├── src/api/snapshot-restore.ts          快照查询/对照/恢复计划接口封装
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/routers/snapshot_restore.py  快照恢复核对专用路由
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── scripts/
│   └── verify_snapshot_restore.py  快照恢复核对端到端验证脚本
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8018/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5198/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8018`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| ETL流水线 | `etl_pipeline` | 流水线 | 流水线名称、源端类型、目标端类型 |
| 分区管理 | `partition_manager` | 分区配置 | 配置名称、目标表、分区字段 |
| 数据质量规则 | `data_quality_rule` | 质量规则 | 规则名称、校验维度、目标表 |
| Schema变更管理 | `schema_change` | 变更记录 | 目标表、变更类型、变更内容 |
| 数据血缘 | `lineage_graph` | 血缘关系 | 目标表、来源表列表、转换逻辑 |
| 补数任务 | `backfill_task` | 补数任务 | 目标表、补数时间范围、补数状态 |
| 表归档管理 | `table_archive` | 归档策略 | 策略名称、目标表、归档条件 |
| 聚合表构建器 | `agg_builder` | 聚合配置 | 配置名称、源事实表、聚合维度 |
| 流水线监控 | `pipeline_monitor` | 监控记录 | 流水线名称、运行时间、执行状态 |
| 冷数据恢复 | `cold_restore` | 恢复任务 | 归档来源、恢复目标路径、恢复时间范围 |
| 维度表生成器 | `dimension_builder` | 维度配置 | 配置名称、来源表、维度键 |
| 数据保留策略 | `data_retention` | 保留规则 | 规则名称、目标数据集、保留天数 |
| 转换方案 | `transform_plan` | 转换步骤 | 方案名称、步骤序列、输入表 |
| 数据迁移任务 | `data_migration` | 迁移任务 | 任务名称、源存储、目标存储 |
| 增量合并引擎 | `delta_merge` | 合并任务 | 目标表、增量来源、合并键 |
| 快照生成器 | `snapshot_creator` | 快照任务 | 目标数据集、快照时间点、快照类型 |
| 湖仓审计 | `data_lake_audit` | 审计报告 | 审计范围、审计时间、表总数 |
| SLA策略 | `sla_policy` | SLA规则 | 规则名称、关联流水线、产出截止时间 |
| 清理作业 | `cleanup_job` | 清理作业 | 作业名称、目标路径、清理条件 |
| 目录同步 | `catalog_sync` | 同步任务 | 任务名称、源Catalog、目标Catalog |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 快照恢复核对（快照生成器 × 冷数据恢复）

值班员处理订单数据集冷恢复时，必须先核对快照再出计划。该流程在
「快照恢复核对台」（侧边导航 `/snapshot-restore-console`）完成，后端专用路由前缀
`/api/snapshot-restore`。

### 关键概念区分

- **快照生成时间**（`快照时间点`）：快照任务实际产出的时刻。
- **覆盖业务日期**（`覆盖起始/截止业务日期`）：快照数据实际对应的业务分区日期。
  两者是不同维度，按业务日期筛选候选时分开呈现，不能用生成时间代替覆盖日。
- **文件齐全 ≠ 状态已完成**：状态为「已完成」只代表任务流程结束；必须核对文件清单，
  存在不可读分片或覆盖区间缺分片时 `file_complete=false`、`restorable=false`。

### 核对与计划规则

1. `GET /snapshots?dataset=&business_date=`：按数据集检索、按业务日期筛选候选，
   每个候选写明版本号、覆盖业务日期、缺失范围（`缺少分片`/`分片不可读`）和可读性。
2. `POST /snapshots/compare`：对照两份快照。只在双方都可读的业务日期上比对校验和，
   不可读分片作为独立完整性问题呈现；覆盖窗口相同不代表内容相同。
3. `POST /snapshots/{id}/faults` / `DELETE`：模拟与清除「某文件随后不可读」，供演练。
4. `POST /restore-plans`：基于**单一快照版本**按业务日期生成实际待恢复文件清单，
   每个文件带 `快照版本` 与 `snapshot_id`，源路径只来自所选快照目录，**禁止混用不同快照分片**。
   所选版本缺件时 `can_start=false`，不产出文件清单，并在 `blocked_reasons` 写明
   所用版本与无法开工的原因。
5. `GET /restore-tasks/{id}/plan`：定位已生成的恢复计划，核对能否开工、用了哪份文件。

### 验证

```bash
# 后端跑在 8019（脚本内 HTTP 段默认连 8019，可用 -I 隔离模式运行）
cd backend && .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8019
.venv/bin/python -I ../scripts/verify_snapshot_restore.py
```

脚本覆盖：候选版本/覆盖日/缺失范围、故障注入、同截止点双快照对照、坏版本阻断、
好版本开工且文件绑定单一版本、同覆盖日内容不同的对照、覆盖缺口阻断、数据集不匹配拒绝，
以及全部 HTTP 接口联调（共 80 项断言）。

领域：数据工程
子方向：数仓与数据湖
