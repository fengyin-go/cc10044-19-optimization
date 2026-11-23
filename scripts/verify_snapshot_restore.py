"""订单数据集快照恢复核对端到端验证脚本。

覆盖题目要求的完整链路：
1. 按数据集 + 业务日期检索快照，候选写明版本/覆盖日/缺失范围；
2. 同截止点两份快照，选中的那份注入文件不可读，另一份完整且分区/校验一致；
3. 用原选择提交恢复计划，核对不能开工及原因；
4. 用完整的另一份提交计划，核对可开工及实际使用版本与文件；
5. 选覆盖日期相同但内容不同的快照做对照；
6. 覆盖缺口快照的缺失范围核对；
7. HTTP API 层路由联调（FastAPI TestClient）。
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/workspace/backend")

from app.services.cold_restore import ColdRestoreService  # noqa: E402
from app.services.snapshot_creator import SnapshotCreatorService  # noqa: E402
from app.store import store  # noqa: E402

DATASET = "ods_orders"
TASK_ID = 4
S1, S2, S3, S4 = 10, 11, 12, 13

passed: list[str] = []
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        passed.append(name)
        print(f"  [PASS] {name}")
    else:
        failed.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name} :: {detail}")


def reset_faults() -> None:
    """把 S1 的故障分片恢复，保证脚本可重复运行。"""
    snap = store.find("snapshot_creator", S1)
    for f in snap.get("文件清单", []):
        f["可读"] = True
        f.pop("不可读原因", None)
    snap["abnormal"] = False
    # 恢复任务回到初始排队状态，并清掉历史计划。
    task = store.find("cold_restore", TASK_ID)
    task["status"] = "排队中"
    task.pop("恢复计划", None)
    task.pop("关联快照版本", None)
    task.pop("关联快照ID", None)


reset_faults()
print("== 1. 按数据集 + 业务日期检索快照候选 ==")
svc = SnapshotCreatorService()
result = svc.query_dataset_snapshots(DATASET, "2026-11-19")
check("返回4份已完成订单快照", result["total"] == 4, str(result["total"]))
for c in result["candidates"]:
    check(f"候选 {c['snapshot_id']} 带版本号", bool(c["version"]), str(c))
    check(f"候选 {c['snapshot_id']} 区分生成时间与覆盖日",
          c["generated_at"] != c["coverage_start"], str(c))
    check(f"候选 {c['snapshot_id']} 带缺失范围字段", "missing_ranges" in c, str(c.keys()))
    check(f"候选 {c['snapshot_id']} 覆盖日包含筛选日",
          "2026-11-19" in c["covered_dates"], str(c["covered_dates"]))

print("== 2. 生成时间 ≠ 覆盖业务日期 的区分 ==")
s1 = svc.get_snapshot_coverage(S1)
check("S1 生成时间是 11-21", s1["generated_at"].startswith("2026-11-21"), s1["generated_at"])
check("S1 覆盖业务日期是 11-18~11-20",
      s1["coverage_start"] == "2026-11-18" and s1["coverage_end"] == "2026-11-20",
      str((s1["coverage_start"], s1["coverage_end"])))

print("== 3. 初始状态：S1/S2 文件齐全；S4 有覆盖缺口 ==")
check("S1 初始文件齐全", svc.is_file_complete(store.find("snapshot_creator", S1)))
check("S2 文件齐全", svc.is_file_complete(store.find("snapshot_creator", S2)))
check("S4 文件不齐全（缺 11-19）", not svc.is_file_complete(store.find("snapshot_creator", S4)))
s4_view = svc._candidate_view(store.find("snapshot_creator", S4))
check("S4 缺失范围指向 2026-11-19",
      len(s4_view["missing_ranges"]) == 1 and s4_view["missing_ranges"][0]["start"] == "2026-11-19",
      str(s4_view["missing_ranges"]))
check("S4 缺失原因是缺少分片",
      s4_view["missing_ranges"][0]["原因"] == "缺少分片",
      str(s4_view["missing_ranges"]))

print("== 4. 已完成状态 ≠ 文件齐全：给 S1 注入一个不可读文件 ==")
fault = svc.mark_file_unreadable(S1)
check("故障注入成功", fault["ok"], str(fault))
check("故障后 S1 状态仍是已完成",
      store.find("snapshot_creator", S1)["status"] == "已完成")
check("故障后 S1 文件不齐全", not fault["file_complete"], str(fault))
s1_view = svc._candidate_view(store.find("snapshot_creator", S1))
check("S1 缺失范围标出 11-20 分片不可读",
      any(g["start"] == "2026-11-20" and g["原因"] == "分片不可读"
          for g in s1_view["missing_ranges"]),
      str(s1_view["missing_ranges"]))
check("S1 restorable=False（已完成但不可恢复）", s1_view["restorable"] is False)
check("S2 仍然完整可恢复",
      svc._candidate_view(store.find("snapshot_creator", S2))["restorable"] is True)

print("== 5. S1 与 S2 对照：分区与内容校验一致（S1 带不可读分片）==")
cmp_12 = svc.compare_snapshot_contents(S1, S2)
check("S1/S2 覆盖窗口相同", cmp_12["same_coverage_window"])
check("S1/S2 分区一致", cmp_12["same_partitions"], str(cmp_12))
check("S1/S2 可读分片内容校验和一致", cmp_12["content_identical"], str(cmp_12["verdict"]))
check("S1/S2 可读分片无校验和分歧", cmp_12["checksum_mismatches"] == [],
      str(cmp_12["checksum_mismatches"]))
check("对照结果标出 S1 的不可读文件",
      len(cmp_12["unreadable_files_a"]) == 1
      and cmp_12["unreadable_files_a"][0]["业务日期"] == "2026-11-20",
      str(cmp_12["unreadable_files_a"]))
check("S2 一侧没有不可读文件", cmp_12["unreadable_files_b"] == [])
check("因 S1 坏件，整体不可完整验证", cmp_12["fully_verifiable"] is False)

print("== 6. 用原选择 S1 提交恢复计划：不能开工 ==")
restore = ColdRestoreService()
plan_s1 = restore.build_snapshot_restore_plan(TASK_ID, S1)
check("S1 计划 can_start=False", plan_s1["can_start"] is False, str(plan_s1))
check("S1 计划文件数为0（不产出清单）", plan_s1["files_to_restore"] == 0)
check("S1 计划写明所用版本", plan_s1["snapshot_version"] == "snap-ods_orders-20261121-0010")
check("S1 计划阻断原因包含不可读分片",
      any("不可读" in r for r in plan_s1["blocked_reasons"]),
      str(plan_s1["blocked_reasons"]))
check("S1 计划未写关联快照到任务",
      store.find("cold_restore", TASK_ID).get("关联快照ID") is None)

print("== 7. 换用 S2 提交恢复计划：可开工，文件全部关联 S2 版本 ==")
plan_s2 = restore.build_snapshot_restore_plan(TASK_ID, S2)
check("S2 计划 can_start=True", plan_s2["can_start"], str(plan_s2["blocked_reasons"]))
check("S2 计划生成3个文件", plan_s2["files_to_restore"] == 3, str(plan_s2["files_to_restore"]))
check("S2 计划每个文件都带 S2 版本",
      all(f["快照版本"] == "snap-ods_orders-20261121-0025" for f in plan_s2["files"]),
      str(plan_s2["files"]))
check("S2 计划每个文件都带 snapshot_id=11",
      all(f["snapshot_id"] == S2 for f in plan_s2["files"]))
check("S2 计划文件按业务日期 18/19/20 生成",
      sorted(f["业务日期"] for f in plan_s2["files"]) ==
      ["2026-11-18", "2026-11-19", "2026-11-20"],
      str(plan_s2["files"]))
check("任务记录关联的快照ID为11",
      store.find("cold_restore", TASK_ID).get("关联快照ID") == S2)
check("计划声明单一快照、不混用分片", plan_s2["single_snapshot_only"] is True)
# 所有文件路径都来自 S2 的存储目录，绝不出现 S1 目录
check("文件路径全部来自 S2 目录，未混用 S1 分片",
      all("/v20261121-0025/" in f["文件路径"] for f in plan_s2["files"])
      and not any("/v20261121-0010/" in f["文件路径"] for f in plan_s2["files"]),
      str([f["文件路径"] for f in plan_s2["files"]]))

print("== 8. 已生成计划可回查（定位一份恢复计划）==")
saved = store.find("cold_restore", TASK_ID)["恢复计划"]
check("任务上保存了计划", saved["snapshot_version"] == "snap-ods_orders-20261121-0025")

print("== 9. 执行恢复时，坏版本计划拦截、好版本计划放行 ==")
# 先挂一个坏计划
restore.build_snapshot_restore_plan(TASK_ID, S1)
blocked_exec = restore.execute_restore(TASK_ID)
check("坏计划执行被拦截", blocked_exec["ok"] is False, str(blocked_exec))
check("任务未被标记完成", store.find("cold_restore", TASK_ID)["status"] == "排队中")
# 重新挂好计划并执行
restore.build_snapshot_restore_plan(TASK_ID, S2)
ok_exec = restore.execute_restore(TASK_ID)
check("好计划执行成功", ok_exec["ok"] and ok_exec["restored_files"] == 3, str(ok_exec))
check("执行结果带所用快照版本",
      ok_exec["snapshot_version"] == "snap-ods_orders-20261121-0025", str(ok_exec))

print("== 10. 对照快照 S3：覆盖日期相同但内容不同 ==")
cmp_23 = svc.compare_snapshot_contents(S2, S3)
check("S2/S3 覆盖窗口相同", cmp_23["same_coverage_window"])
check("S2/S3 分区一致", cmp_23["same_partitions"])
check("S2/S3 内容不一致", cmp_23["content_identical"] is False, str(cmp_23))
check("S2/S3 存在逐文件校验和分歧", len(cmp_23["checksum_mismatches"]) == 3,
      str(cmp_23["checksum_mismatches"]))
check("结论提示不能互相替代", "不能互相替代" in cmp_23["verdict"], cmp_23["verdict"])
# S3 本身文件齐全，可开工，但内容是另一份
plan_s3 = restore.build_snapshot_restore_plan(TASK_ID, S3)
check("S3 计划可开工", plan_s3["can_start"], str(plan_s3["blocked_reasons"]))
check("S3 计划使用 S3 自己的版本与文件",
      all(f["快照版本"] == "snap-ods_orders-20261121-0105"
          and "/v20261121-0105/" in f["文件路径"] for f in plan_s3["files"]))

print("== 11. S4 覆盖缺口快照生成计划被阻断并写明缺口 ==")
plan_s4 = restore.build_snapshot_restore_plan(TASK_ID, S4)
check("S4 计划不能开工", plan_s4["can_start"] is False, str(plan_s4))
check("S4 阻断原因写明 11-19 缺分片",
      any("2026-11-19" in r and "缺少分片" in r for r in plan_s4["blocked_reasons"]),
      str(plan_s4["blocked_reasons"]))

print("== 12. 数据集不匹配拒绝出计划 ==")
# snapshot 2 属于 dws_sales_daily
mismatch = restore.build_snapshot_restore_plan(TASK_ID, 2)
check("数据集不一致被拒绝", mismatch["can_start"] is False and "数据集" in mismatch["message"],
      str(mismatch))

print("== 13. HTTP API 联调（通过运行中的 uvicorn + 标准库 urllib）==")
import json  # noqa: E402
import urllib.request  # noqa: E402

API_BASE = "http://127.0.0.1:8019"


def api_get(path: str, params: dict[str, str] | None = None) -> tuple[int, dict]:
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


def api_post(path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        API_BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def api_delete(path: str) -> tuple[int, dict]:
    req = urllib.request.Request(API_BASE + path, method="DELETE")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


# 服务进程持有独立内存数据，先清除历史故障并复位任务，保证脚本可重复运行。
status, body = api_delete(f"/api/snapshot-restore/snapshots/{S1}/faults")
check("清除 S1 历史故障", status == 200 and body["ok"], str(body))
status, body = api_post(f"/api/snapshot-restore/restore-tasks/{TASK_ID}/reset", {})
check("恢复任务复位为排队中", status == 200 and body["ok"], str(body))

status, body = api_get("/api/snapshot-restore/snapshots", {"dataset": DATASET})
check("GET 快照查询 200", status == 200, str(status))
# 共5份已完成快照：4份带清单的场景快照 + 1份无清单的旧快照（已完成但无法证明文件齐全）。
check("API 无日期过滤返回5个候选", body["total"] == 5, str(body.get("total")))
legacy = next(c for c in body["candidates"] if c["snapshot_id"] == 1)
check("旧快照虽已完成但 file_complete=False（已完成≠文件齐全）",
      legacy["file_complete"] is False and legacy["restorable"] is False, str(legacy))
check("旧快照缺失范围为空但无清单，不能开工",
      legacy["file_total"] == 0, str(legacy))

status, body = api_get("/api/snapshot-restore/snapshots",
                       {"dataset": DATASET, "business_date": "2026-11-19"})
check("按业务日期筛选 200 且数量正确", status == 200 and body["total"] == 4)

status, body = api_post(f"/api/snapshot-restore/snapshots/{S1}/faults", {})
check("故障注入接口 200", status == 200 and body["ok"], str(body))

status, body = api_post("/api/snapshot-restore/restore-plans",
                        {"task_id": TASK_ID, "snapshot_id": S1})
check("API 用 S1 出计划返回不能开工",
      status == 200 and body["can_start"] is False, str(body))

status, body = api_post("/api/snapshot-restore/restore-plans",
                        {"task_id": TASK_ID, "snapshot_id": S2})
check("API 用 S2 出计划可开工", body["can_start"] and body["files_to_restore"] == 3, str(body))

status, body = api_get(f"/api/snapshot-restore/restore-tasks/{TASK_ID}/plan")
check("回查计划接口返回 S2 版本",
      body["snapshot_version"] == "snap-ods_orders-20261121-0025", str(body))

status, body = api_post("/api/snapshot-restore/snapshots/compare",
                        {"snapshot_id_a": S2, "snapshot_id_b": S3})
check("对照接口返回内容不一致", body["content_identical"] is False, str(body))

status, body = api_get(f"/api/snapshot-restore/restore-tasks/{TASK_ID}/candidates")
check("任务候选接口 200 且带可开工标记",
      status == 200 and all("plan_can_start" in c for c in body["candidates"]), str(body))

status, body = api_get(f"/api/snapshot-restore/snapshots/{S1}/coverage")
check("覆盖详情接口 200", status == 200 and body["ok"], str(body))

status, body = api_get("/api/snapshot-restore/snapshots", {"dataset": "no_such_dataset"})
check("未知数据集返回空候选不报错", status == 200 and body["total"] == 0, str(body))

print()
print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
if failed:
    print("失败明细：")
    for item in failed:
        print(" -", item)
    sys.exit(1)
print("全部核对场景通过。")
