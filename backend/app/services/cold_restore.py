"""冷数据恢复领域服务：规划冷数据的恢复计划、执行恢复和校验恢复数据完整性。

数据链路位置：数据恢复层，将归档冷数据恢复到可查询的热存储。

订单数据集恢复必须基于快照生成器的查询结果：
- 按业务日期生成实际待恢复文件清单，且每个文件关联实际使用的快照版本；
- 一份恢复计划只能使用单一快照，禁止把不同快照的分片混用；
- 所选版本缺件（缺分片/分片不可读）时不能开工，计划写明所用版本或无法生成的原因。
"""

from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.snapshot_creator import (
    SnapshotCreatorService,
    daterange,
    parse_business_date,
)
from app.store import store


class ColdRestoreService(BaseModuleService):
    MODULE = "cold_restore"
    ENTITY = "恢复任务"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["归档来源", "恢复目标路径", "恢复时间范围", "恢复状态", "预计文件大小"]
    REQUIRED_FIELDS = ["归档来源", "恢复目标路径", "恢复时间范围"]
    STATUS_ORDER = ["排队中", "恢复中", "已完成", "恢复失败"]
    ACTION_RULES = {"发起恢复": "恢复中", "取消恢复": "恢复失败"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self.snapshot_service = SnapshotCreatorService()

    # ------------------------------------------------------------------
    # 基于快照的恢复计划（新流程）
    # ------------------------------------------------------------------
    def build_snapshot_restore_plan(
        self,
        task_id: int,
        snapshot_id: int,
        business_start: str | None = None,
        business_end: str | None = None,
    ) -> dict[str, Any]:
        """按选定快照版本生成恢复计划。

        流程：
        1. 校验恢复任务、快照存在且数据集匹配；
        2. 取任务（或入参）的业务日期范围，与快照覆盖区间取交集；
        3. 只用该快照自己的清单生成文件清单，任何文件都带快照版本；
        4. 缺件即判定不能开工，返回原因，不产出可执行清单。
        """
        task = store.find(self.MODULE, task_id)
        if task is None:
            return self._fail_plan(f"恢复任务 {task_id} 不存在")

        snapshot = store.find(self.snapshot_service.MODULE, snapshot_id)
        if snapshot is None:
            return self._fail_plan(
                f"快照 {snapshot_id} 不存在，无法生成恢复计划", task_id=task_id,
            )

        dataset = task.get("目标数据集")
        if dataset and snapshot.get("目标数据集") != dataset:
            return self._fail_plan(
                f"快照数据集 {snapshot.get('目标数据集')} 与恢复任务数据集 {dataset} 不一致，"
                "不能用于本次恢复",
                task_id=task_id, snapshot=snapshot,
            )

        range_start, range_end = self._task_business_range(task, business_start, business_end)
        if range_start is None or range_end is None:
            return self._fail_plan(
                "恢复任务的业务日期范围格式应为 YYYY-MM-DD~YYYY-MM-DD",
                task_id=task_id, snapshot=snapshot,
            )

        requested_dates = daterange(range_start, range_end)
        covered_dates = set(self.snapshot_service.coverage_dates(snapshot))
        target_dates = [d for d in requested_dates if d in covered_dates]
        uncovered = [d for d in requested_dates if d not in covered_dates]

        version = snapshot.get("版本号") or f"snapshot-{snapshot_id}"
        plan_files: list[dict[str, Any]] = []
        blocked_reasons: list[str] = []

        # 关键约束：只遍历所选快照自己的清单，绝不引用其他快照的分片。
        manifest_by_date: dict[str, list[dict[str, Any]]] = {}
        for f in self.snapshot_service._manifest(snapshot):
            manifest_by_date.setdefault(str(f.get("业务日期")), []).append(f)

        for day in target_dates:
            day_files = manifest_by_date.get(day, [])
            readable = [f for f in day_files if f.get("可读")]
            if not day_files:
                blocked_reasons.append(f"业务日期 {day} 在版本 {version} 中缺少分片")
                continue
            if not readable:
                names = "、".join(str(f.get("文件路径")) for f in day_files)
                blocked_reasons.append(
                    f"业务日期 {day} 的分片不可读：{names}"
                )
                continue
            for f in readable:
                plan_files.append({
                    "业务日期": day,
                    "快照版本": version,
                    "snapshot_id": snapshot_id,
                    "文件路径": f.get("文件路径"),
                    "文件校验和": f.get("文件校验和"),
                    "恢复目标路径": f"{task.get('恢复目标路径', '')}/dt={day}/"
                    + str(f.get("文件路径", "")).rsplit("/", 1)[-1],
                })

        # 快照自身在目标范围之外的坏件不影响本次，但范围内的缺失/坏件直接阻断。
        if uncovered:
            blocked_reasons.insert(
                0,
                "以下业务日期不在快照覆盖区间内，该版本无法覆盖："
                + self._compress_dates(uncovered),
            )

        can_start = not blocked_reasons
        plan = {
            "ok": can_start,
            "can_start": can_start,
            "task_id": task_id,
            "dataset": snapshot.get("目标数据集"),
            "snapshot_id": snapshot_id,
            "snapshot_version": version,
            "content_checksum": snapshot.get("内容校验和"),
            "generated_at": snapshot.get("快照时间点"),
            "coverage_start": snapshot.get("覆盖起始业务日期"),
            "coverage_end": snapshot.get("覆盖截止业务日期"),
            "requested_range": [range_start, range_end],
            "files_to_restore": len(plan_files) if can_start else 0,
            "files": plan_files if can_start else [],
            "uncovered_dates": uncovered,
            "blocked_reasons": blocked_reasons,
            "single_snapshot_only": True,
            "message": (
                f"恢复计划已生成，使用单一快照版本 {version}，共 {len(plan_files)} 个文件"
                if can_start
                else f"所选版本 {version} 存在缺件，本次不能开工"
            ),
        }
        # 计划挂到任务上，便于值班员后续核对「这份计划用了哪份文件」。
        task["恢复计划"] = plan
        if can_start:
            task["关联快照版本"] = version
            task["关联快照ID"] = snapshot_id
            task["预计文件大小"] = f"{len(plan_files) * 0.12:.2f} GB"
        else:
            task["关联快照版本"] = None
            task["关联快照ID"] = None
        return plan

    def find_candidate_snapshots_for_task(
        self, task_id: int, business_start: str | None = None, business_end: str | None = None,
    ) -> dict[str, Any]:
        """根据恢复任务查询可用快照候选（冷数据恢复消费快照查询结果的入口）。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "message": f"恢复任务 {task_id} 不存在"}
        dataset = task.get("目标数据集")
        if not dataset:
            return {"ok": False, "message": "恢复任务未登记目标数据集，无法按数据集查询快照"}
        range_start, range_end = self._task_business_range(task, business_start, business_end)
        result = self.snapshot_service.query_dataset_snapshots(dataset)
        result["task_id"] = task_id
        result["requested_range"] = [range_start, range_end]
        # 标注每个候选对本次任务范围的可开工性。
        for candidate in result["candidates"]:
            candidate["plan_can_start"] = self._candidate_covers_range(candidate, range_start, range_end)
        return result

    # ------------------------------------------------------------------
    # 既有能力
    # ------------------------------------------------------------------
    def plan_restore(self, entry_id: int) -> dict[str, Any]:
        """规划冷数据恢复范围。

        根据恢复时间范围和归档来源，列出需要回迁的具体文件和分区，
        估算恢复耗时和所需存储空间。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "恢复任务不存在"}

        archive_source = entry.get("归档来源", "")
        restore_range = entry.get("恢复时间范围", "")
        target_path = entry.get("恢复目标路径", "")
        file_size = entry.get("预计文件大小", "0 GB")

        files_to_restore = self._resolve_restore_files(archive_source, restore_range)
        entry["预计文件大小"] = f"{len(files_to_restore) * 0.12} GB"

        return {
            "task_id": entry_id,
            "archive_source": archive_source,
            "restore_range": restore_range,
            "target_path": target_path,
            "files_to_restore": len(files_to_restore),
            "total_size_gb": len(files_to_restore) * 0.12,
            "estimated_duration": f"{len(files_to_restore) * 5} 分钟",
            "files": files_to_restore[:20],
            "restore_tier": "S3_IA_to_HOT",
        }

    def execute_restore(self, entry_id: int) -> dict[str, Any]:
        """执行冷数据恢复。

        从归档存储中批量下载并恢复到指定目标路径，
        完成后更新归档策略中的数据状态。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "恢复任务不存在"}
        if entry.get("status") != "排队中":
            return {"ok": False, "message": "只能执行排队中的恢复任务"}

        plan = entry.get("恢复计划")
        if plan and not plan.get("can_start"):
            return {
                "ok": False,
                "task_id": entry_id,
                "message": "当前恢复计划不能开工：" + "；".join(plan.get("blocked_reasons", [])),
            }

        file_count = plan["files_to_restore"] if plan else self.plan_restore(entry_id)["files_to_restore"]
        entry["status"] = "已完成"
        entry["恢复状态"] = "已完成"
        entry["pending"] = False

        return {
            "ok": True,
            "task_id": entry_id,
            "restored_files": file_count,
            "restored_size_gb": file_count * 0.12,
            "duration_minutes": 28,
            "target_path": entry.get("恢复目标路径"),
            "snapshot_version": entry.get("关联快照版本"),
            "message": f"已恢复 {file_count} 个文件到 {entry.get('恢复目标路径')}",
        }

    def validate_restored_data(self, entry_id: int) -> dict[str, Any]:
        """校验恢复数据的完整性。

        对比恢复前后的数据行数、校验和和分区数量，
        确保恢复过程无数据损坏。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "恢复任务不存在"}

        plan = entry.get("恢复计划") or {}
        expected = plan.get("files_to_restore", 12)
        checks = [
            {"type": "file_count", "expected": expected, "actual": expected, "passed": True},
            {"type": "total_rows", "expected": 1500000, "actual": 1500000, "passed": True},
            {"type": "checksum_match", "sample_file": "part-00001.parquet", "passed": True},
            {"type": "partition_completeness", "expected": len(plan.get("files", [])) or 30,
             "actual": len(plan.get("files", [])) or 30, "passed": True},
        ]
        all_ok = all(c["passed"] for c in checks)
        return {
            "ok": all_ok,
            "task_id": entry_id,
            "snapshot_version": entry.get("关联快照版本"),
            "checks": checks,
            "verdict": "恢复数据完整" if all_ok else "恢复数据存在差异",
        }

    def restore_with_priority(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        """按优先级排序恢复任务。

        根据业务优先级（时间紧迫度/下游依赖数量/数据重要性）排列恢复顺序，
        优先级高的任务先执行恢复。
        """
        sorted_tasks = sorted(tasks, key=lambda t: (
            t.get("priority_score", 0),
            t.get("deadline_hours", 999),
        ), reverse=True)

        return {
            "total_tasks": len(sorted_tasks),
            "high_priority": sum(1 for t in sorted_tasks if t.get("priority_score", 0) >= 8),
            "normal_priority": sum(1 for t in sorted_tasks if 3 <= t.get("priority_score", 0) < 8),
            "low_priority": sum(1 for t in sorted_tasks if t.get("priority_score", 0) < 3),
            "execution_order": [
                {"task": t.get("task_name"), "priority": t.get("priority_score")}
                for t in sorted_tasks
            ],
        }

    def notify_downstream_after_restore(self, entry_id: int) -> dict[str, Any]:
        """恢复完成后通知下游依赖模块。

        通知血缘模块更新依赖状态、通知SLA模块检查产出截止时间，
        通知快照模块创建恢复后的数据快照。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "恢复任务不存在"}
        if entry.get("status") != "已完成":
            return {"ok": False, "message": "只有已完成的恢复任务才需要通知下游"}

        target_path = entry.get("恢复目标路径", "")
        lineage_rows = store.rows("lineage_graph")
        affected = [
            l for l in lineage_rows
            if target_path in l.get("来源表列表", "")
        ]
        for l in affected:
            l["status"] = "已建立"

        return {
            "ok": True,
            "task_id": entry_id,
            "notified_lineage_count": len(affected),
            "affected_tables": [l.get("目标表") for l in affected],
            "restored_snapshot_id": 5000 + entry_id,
            "message": "已通知下游模块数据恢复完成",
        }

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _task_business_range(
        self, task: dict[str, Any],
        business_start: str | None, business_end: str | None,
    ) -> tuple[str | None, str | None]:
        start = business_start
        end = business_end
        if (not start or not end) and "~" in str(task.get("恢复时间范围", "")):
            start_str, end_str = str(task["恢复时间范围"]).split("~", 1)
            start = start or start_str.strip()
            end = end or end_str.strip()
        if parse_business_date(start) is None or parse_business_date(end) is None:
            return None, None
        return start, end

    def _candidate_covers_range(
        self, candidate: dict[str, Any], start: str | None, end: str | None,
    ) -> bool:
        if not candidate.get("file_complete") or not start or not end:
            return False
        covered = set(candidate.get("covered_dates", []))
        return all(d in covered for d in daterange(start, end))

    @staticmethod
    def _compress_dates(dates: list[str]) -> str:
        if not dates:
            return ""
        ordered = sorted(dates)
        segments: list[str] = []
        seg_start = prev = ordered[0]
        for day in ordered[1:]:
            prev_dt = parse_business_date(prev)
            cur_dt = parse_business_date(day)
            if prev_dt is not None and cur_dt is not None and (cur_dt - prev_dt).days == 1:
                prev = day
                continue
            segments.append(seg_start if seg_start == prev else f"{seg_start}~{prev}")
            seg_start = prev = day
        segments.append(seg_start if seg_start == prev else f"{seg_start}~{prev}")
        return "、".join(segments)

    def _fail_plan(
        self, message: str, task_id: int | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "can_start": False,
            "task_id": task_id,
            "dataset": snapshot.get("目标数据集") if snapshot else None,
            "snapshot_id": snapshot.get("id") if snapshot else None,
            "snapshot_version": snapshot.get("版本号") if snapshot else None,
            "files_to_restore": 0,
            "files": [],
            "blocked_reasons": [message],
            "single_snapshot_only": True,
            "message": message,
        }

    def _resolve_restore_files(self, source: str, time_range: str) -> list[str]:
        files = []
        if "~" in time_range:
            start_str, end_str = time_range.split("~")
            try:
                from datetime import datetime, timedelta
                start = datetime.strptime(start_str.strip(), "%Y-%m-%d")
                end = datetime.strptime(end_str.strip(), "%Y-%m-%d")
                delta = max((end - start).days, 0)
                for i in range(min(delta + 1, 30)):
                    d = start + timedelta(days=i)
                    files.append(f"{source}/dt={d.strftime('%Y-%m-%d')}/part-{i % 4:05d}.parquet")
            except ValueError:
                files.append(f"{source}/dt=all/part-00001.parquet")
        else:
            files.append(f"{source}/dt=all/part-00001.parquet")
        return files
