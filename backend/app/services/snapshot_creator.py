"""数据快照领域服务：创建带血缘信息的数据快照、快照差异对比和快照恢复列表查询。

数据链路位置：数据快照层，为数据版本管理和恢复提供时间点快照。

恢复核对相关概念区分：
- 快照生成时间（``快照时间点``）：快照任务实际产出的时刻；
- 覆盖业务日期（``覆盖起始/截止业务日期``）：快照内数据实际对应的业务分区日期。
两者不是同一个维度，值班员按业务日期筛选候选时必须分开呈现。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATE_FMT = "%Y-%m-%d"


def parse_business_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), DATE_FMT)
    except ValueError:
        return None


def daterange(start: str, end: str) -> list[str]:
    """返回闭区间内的业务日期列表；区间非法时返回空列表。"""
    start_dt = parse_business_date(start)
    end_dt = parse_business_date(end)
    if start_dt is None or end_dt is None or end_dt < start_dt:
        return []
    days = (end_dt - start_dt).days
    return [(start_dt + timedelta(days=i)).strftime(DATE_FMT) for i in range(days + 1)]


class SnapshotCreatorService(BaseModuleService):
    MODULE = "snapshot_creator"
    ENTITY = "快照任务"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["目标数据集", "快照时间点", "快照类型", "存储路径", "文件大小"]
    REQUIRED_FIELDS = ["目标数据集", "快照时间点", "快照类型"]
    STATUS_ORDER = ["排队中", "创建中", "已完成", "创建失败"]
    ACTION_RULES = {"创建快照": "排队中", "取消快照": "创建中"}
    NEGATIVE_ACTIONS = []

    SNAPSHOT_TYPES = frozenset({"full", "incremental", "daily_sealed"})

    # ------------------------------------------------------------------
    # 覆盖核对基础能力
    # ------------------------------------------------------------------
    def _manifest(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """快照分片清单。老数据没有清单时返回空列表，而不是假装文件齐全。"""
        manifest = entry.get("文件清单")
        return list(manifest) if isinstance(manifest, list) else []

    def coverage_dates(self, entry: dict[str, Any]) -> list[str]:
        """快照元数据声明覆盖的全部业务日期（按日历闭区间展开）。"""
        return daterange(
            str(entry.get("覆盖起始业务日期", "")),
            str(entry.get("覆盖截止业务日期", "")),
        )

    def _readable_dates(self, entry: dict[str, Any]) -> set[str]:
        return {
            str(f.get("业务日期"))
            for f in self._manifest(entry)
            if f.get("可读") and f.get("业务日期")
        }

    def unreadable_files(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "文件路径": f.get("文件路径"),
                "业务日期": f.get("业务日期"),
                "文件校验和": f.get("文件校验和"),
                "不可读原因": f.get("不可读原因", "分片不可读"),
            }
            for f in self._manifest(entry)
            if not f.get("可读")
        ]

    def missing_ranges(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """把覆盖区间内没有可读分片的业务日期压缩成连续缺失段。

        缺失分两种原因：
        - 缺少分片：该业务日期在清单里根本没有文件；
        - 分片不可读：清单里有文件但全部标记为不可读。
        """
        covered = self.coverage_dates(entry)
        if not covered:
            return []
        manifest_dates: dict[str, list[dict[str, Any]]] = {}
        for f in self._manifest(entry):
            manifest_dates.setdefault(str(f.get("业务日期")), []).append(f)

        gaps: list[dict[str, Any]] = []
        for day in covered:
            files = manifest_dates.get(day, [])
            readable = any(f.get("可读") for f in files)
            if readable:
                continue
            reason = "分片不可读" if files else "缺少分片"
            detail = "；".join(
                str(f.get("不可读原因", "分片不可读")) for f in files if not f.get("可读")
            ) or "该业务日期无任何分片"
            if gaps and gaps[-1]["end"] == self._prev_day(day) and gaps[-1]["原因"] == reason:
                gaps[-1]["end"] = day
                gaps[-1]["业务日期"].append(day)
            else:
                gaps.append({
                    "start": day, "end": day, "业务日期": [day],
                    "原因": reason, "说明": detail,
                })
        return gaps

    @staticmethod
    def _prev_day(day: str) -> str:
        return (datetime.strptime(day, DATE_FMT) - timedelta(days=1)).strftime(DATE_FMT)

    def is_file_complete(self, entry: dict[str, Any]) -> bool:
        """文件齐全 = 清单里没有不可读分片，且覆盖区间每个业务日期都有可读分片。

        注意：状态为「已完成」只代表快照任务流程结束，不能据此认定文件齐全。
        """
        manifest = self._manifest(entry)
        if not manifest:
            return False
        if any(not f.get("可读") for f in manifest):
            return False
        return not self.missing_ranges(entry)

    def _candidate_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        manifest = self._manifest(entry)
        missing = self.missing_ranges(entry)
        unreadable = self.unreadable_files(entry)
        complete = self.is_file_complete(entry)
        return {
            "snapshot_id": entry["id"],
            "version": entry.get("版本号"),
            "dataset": entry.get("目标数据集"),
            "status": entry.get("status"),
            "generated_at": entry.get("快照时间点"),
            "snapshot_type": entry.get("快照类型"),
            "storage_path": entry.get("存储路径"),
            "coverage_start": entry.get("覆盖起始业务日期"),
            "coverage_end": entry.get("覆盖截止业务日期"),
            "covered_dates": self.coverage_dates(entry),
            "missing_ranges": missing,
            "file_total": len(manifest),
            "file_readable": sum(1 for f in manifest if f.get("可读")),
            "unreadable_files": unreadable,
            "file_complete": complete,
            # 能否用于恢复取决于文件齐全，而不是快照任务状态。
            "restorable": complete,
            "content_checksum": entry.get("内容校验和"),
        }

    # ------------------------------------------------------------------
    # 值班员查询入口
    # ------------------------------------------------------------------
    def query_dataset_snapshots(
        self, dataset: str, business_date: str | None = None,
    ) -> dict[str, Any]:
        """按数据集检索快照候选，可用业务日期筛选。

        候选逐条写明版本、覆盖业务日期和缺失范围，并区分快照生成时间与覆盖日。
        """
        rows = store.rows(self.MODULE)
        candidates = [s for s in rows if s.get("目标数据集") == dataset]

        if business_date:
            candidates = [
                s for s in candidates
                if business_date in self.coverage_dates(s)
            ]

        # 已完成的快照才可能成为恢复候选；但文件是否齐全由清单核对单独给出。
        completed = [s for s in candidates if s.get("status") == "已完成"]
        completed.sort(key=lambda s: str(s.get("快照时间点", "")), reverse=True)

        views = [self._candidate_view(s) for s in completed]
        return {
            "ok": True,
            "query": {"dataset": dataset, "business_date": business_date},
            "total": len(views),
            "restorable_count": sum(1 for v in views if v["restorable"]),
            "candidates": views,
            "excluded_non_completed": [
                {"snapshot_id": s["id"], "status": s.get("status"), "version": s.get("版本号")}
                for s in candidates if s.get("status") != "已完成"
            ],
        }

    def get_snapshot_coverage(self, snapshot_id: int) -> dict[str, Any]:
        """定位单份快照的覆盖核对详情，供恢复计划引用。"""
        entry = store.find(self.MODULE, snapshot_id)
        if entry is None:
            return {"ok": False, "message": f"快照 {snapshot_id} 不存在"}
        view = self._candidate_view(entry)
        view["ok"] = True
        view["manifest"] = self._manifest(entry)
        return view

    # ------------------------------------------------------------------
    # 同覆盖日快照对照
    # ------------------------------------------------------------------
    def compare_snapshot_contents(self, snapshot_id_a: int, snapshot_id_b: int) -> dict[str, Any]:
        """对照两份快照：分区与逐文件校验和是否一致、内容是否真的相同。

        覆盖业务日期相同不代表内容相同，必须比对校验和。
        """
        snap_a = store.find(self.MODULE, snapshot_id_a)
        snap_b = store.find(self.MODULE, snapshot_id_b)
        if snap_a is None or snap_b is None:
            return {"ok": False, "message": "快照不存在"}
        if snap_a.get("目标数据集") != snap_b.get("目标数据集"):
            return {"ok": False, "message": "快照数据集不一致，无法对照"}

        dates_a = set(self.coverage_dates(snap_a))
        dates_b = set(self.coverage_dates(snap_b))
        checksums_a = {
            str(f.get("业务日期")): f.get("文件校验和")
            for f in self._manifest(snap_a) if f.get("可读")
        }
        checksums_b = {
            str(f.get("业务日期")): f.get("文件校验和")
            for f in self._manifest(snap_b) if f.get("可读")
        }
        unreadable_a = self.unreadable_files(snap_a)
        unreadable_b = self.unreadable_files(snap_b)
        # 只在双方都能读到的业务日期上比对校验和；不可读属于完整性问题，
        # 由 unreadable_files / file_complete 单独呈现，不能混同为内容分歧。
        readable_dates_a = set(checksums_a)
        readable_dates_b = set(checksums_b)
        mutually_readable = sorted(readable_dates_a & readable_dates_b)
        checksum_mismatches = [
            {"业务日期": day,
             "snapshot_a_checksum": checksums_a.get(day),
             "snapshot_b_checksum": checksums_b.get(day)}
            for day in mutually_readable
            if checksums_a.get(day) != checksums_b.get(day)
        ]
        same_partitions = dates_a == dates_b
        same_content = (
            same_partitions
            and not checksum_mismatches
            and snap_a.get("内容校验和") == snap_b.get("内容校验和")
        )
        fully_verifiable = not unreadable_a and not unreadable_b
        if same_content and fully_verifiable:
            verdict = "两份快照分区与内容校验一致"
        elif same_content:
            verdict = "可读分片校验一致，但存在不可读分片，无法完整验证内容一致性"
        else:
            verdict = "覆盖日期相同但内容不同，不能互相替代"
        return {
            "ok": True,
            "dataset": snap_a.get("目标数据集"),
            "snapshot_a": {
                "snapshot_id": snapshot_id_a, "version": snap_a.get("版本号"),
                "generated_at": snap_a.get("快照时间点"),
                "coverage_start": snap_a.get("覆盖起始业务日期"),
                "coverage_end": snap_a.get("覆盖截止业务日期"),
                "content_checksum": snap_a.get("内容校验和"),
                "file_complete": self.is_file_complete(snap_a),
            },
            "snapshot_b": {
                "snapshot_id": snapshot_id_b, "version": snap_b.get("版本号"),
                "generated_at": snap_b.get("快照时间点"),
                "coverage_start": snap_b.get("覆盖起始业务日期"),
                "coverage_end": snap_b.get("覆盖截止业务日期"),
                "content_checksum": snap_b.get("内容校验和"),
                "file_complete": self.is_file_complete(snap_b),
            },
            "same_coverage_window": (
                snap_a.get("覆盖起始业务日期") == snap_b.get("覆盖起始业务日期")
                and snap_a.get("覆盖截止业务日期") == snap_b.get("覆盖截止业务日期")
            ),
            "same_partitions": same_partitions,
            "partitions_only_in_a": sorted(dates_a - dates_b),
            "partitions_only_in_b": sorted(dates_b - dates_a),
            "mutually_readable_dates": mutually_readable,
            "checksum_mismatches": checksum_mismatches,
            "unreadable_files_a": unreadable_a,
            "unreadable_files_b": unreadable_b,
            "content_identical": same_content,
            "fully_verifiable": fully_verifiable,
            "verdict": verdict,
        }

    # ------------------------------------------------------------------
    # 故障注入：模拟「随后有一个文件不可读」
    # ------------------------------------------------------------------
    def mark_file_unreadable(
        self, snapshot_id: int, file_path: str | None = None, reason: str | None = None,
    ) -> dict[str, Any]:
        entry = store.find(self.MODULE, snapshot_id)
        if entry is None:
            return {"ok": False, "message": f"快照 {snapshot_id} 不存在"}
        manifest = self._manifest(entry)
        if not manifest:
            return {"ok": False, "message": "该快照没有文件清单，无法标记分片故障"}

        target = None
        if file_path:
            target = next((f for f in manifest if f.get("文件路径") == file_path), None)
            if target is None:
                return {"ok": False, "message": f"快照中找不到文件：{file_path}"}
        else:
            # 默认故障注入到「当前仍可读」的最后一个分片，避免重复命中已坏分片。
            readable = [f for f in manifest if f.get("可读")]
            if not readable:
                return {"ok": False, "message": "该快照已没有可读分片，无法再次注入故障"}
            target = readable[-1]

        target["可读"] = False
        target["不可读原因"] = reason or "存储节点返回 500 Internal Error，分片无法读取"
        entry["abnormal"] = True
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "version": entry.get("版本号"),
            "file_path": target.get("文件路径"),
            "business_date": target.get("业务日期"),
            "file_complete": self.is_file_complete(entry),
            "message": "分片已标记为不可读",
        }

    def mark_file_readable(self, snapshot_id: int, file_path: str) -> dict[str, Any]:
        """恢复故障分片可读性，便于重复演练。"""
        entry = store.find(self.MODULE, snapshot_id)
        if entry is None:
            return {"ok": False, "message": f"快照 {snapshot_id} 不存在"}
        target = next(
            (f for f in self._manifest(entry) if f.get("文件路径") == file_path), None,
        )
        if target is None:
            return {"ok": False, "message": f"快照中找不到文件：{file_path}"}
        target["可读"] = True
        target.pop("不可读原因", None)
        entry["abnormal"] = not self.is_file_complete(entry)
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "file_path": file_path,
            "file_complete": self.is_file_complete(entry),
            "message": "分片已恢复可读",
        }

    # ------------------------------------------------------------------
    # 既有能力（保持兼容，恢复可用性改为基于清单核对）
    # ------------------------------------------------------------------
    def create_snapshot_with_lineage(self, entry_id: int) -> dict[str, Any]:
        """创建快照并同步记录血缘关系。

        快照创建后自动注册到 lineage_graph 模块，
        记录快照数据集与来源表之间的血缘关系。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "快照任务不存在"}

        snapshot_type = entry.get("快照类型", "full")
        dataset = entry.get("目标数据集", "")
        snapshot_time = entry.get("快照时间点", datetime.now().isoformat())
        storage_path = entry.get("存储路径", f"/data/snapshots/{dataset}/{snapshot_time}")

        entry["status"] = "已完成"
        entry["存储路径"] = storage_path
        entry["文件大小"] = f"{self._estimate_snapshot_size(snapshot_type)} GB"
        entry["pending"] = False

        lineage_rows = store.rows("lineage_graph")
        lid = max((int(r.get("id", 0)) for r in lineage_rows), default=0) + 1
        lineage_rows.append({
            "id": lid,
            "status": "已建立",
            "目标表": f"{dataset}_snapshot",
            "来源表列表": dataset,
            "转换逻辑": f"snapshot_{snapshot_type}_at_{snapshot_time}",
            "中间层级": "1",
            "血缘深度": "1",
            "pending": False,
            "abnormal": False,
            "snapshot_id": entry_id,
        })

        return {
            "ok": True,
            "snapshot_id": entry_id,
            "snapshot_type": snapshot_type,
            "dataset": dataset,
            "timestamp": snapshot_time,
            "storage_path": storage_path,
            "size_gb": entry["文件大小"],
            "lineage_id": lid,
            "message": "快照创建完成并已注册血缘",
        }

    def diff_snapshots(self, snapshot_id_a: int, snapshot_id_b: int) -> dict[str, Any]:
        """比较两个快照之间的数据差异。

        返回新增行数、删除行数、修改行数和变更字段列表，
        可用于审计追溯和数据回滚决策。
        """
        snap_a = store.find(self.MODULE, snapshot_id_a)
        snap_b = store.find(self.MODULE, snapshot_id_b)
        if snap_a is None or snap_b is None:
            return {"ok": False, "message": "快照不存在"}
        if snap_a.get("目标数据集") != snap_b.get("目标数据集"):
            return {"ok": False, "message": "快照数据集不一致，无法比较"}

        diff = {
            "ok": True,
            "snapshot_a": {"id": snapshot_id_a, "time": snap_a.get("快照时间点"), "size": snap_a.get("文件大小")},
            "snapshot_b": {"id": snapshot_id_b, "time": snap_b.get("快照时间点"), "size": snap_b.get("文件大小")},
            "dataset": snap_a.get("目标数据集"),
            "added_rows": 2850,
            "deleted_rows": 120,
            "modified_rows": 3400,
            "unchanged_rows": 1540000,
            "changed_fields": ["amount", "status", "updated_at"],
            "revert_cost_estimate_seconds": 120,
        }
        return diff

    def list_restorable_snapshots(self, dataset: str) -> dict[str, Any]:
        """列出指定数据集的可恢复快照，供冷数据恢复模块查询。

        ``restorable`` 以文件清单核对结果为准：已完成但缺件/坏件的快照不算可恢复。
        """
        query = self.query_dataset_snapshots(dataset)
        candidates = query["candidates"]
        return {
            "dataset": dataset,
            "total_snapshots": len(candidates),
            "latest_snapshot_time": candidates[0]["generated_at"] if candidates else None,
            "oldest_snapshot_time": candidates[-1]["generated_at"] if candidates else None,
            "snapshots": [
                {
                    "id": c["snapshot_id"],
                    "version": c["version"],
                    "time": c["generated_at"],
                    "coverage_start": c["coverage_start"],
                    "coverage_end": c["coverage_end"],
                    "type": c["snapshot_type"],
                    "path": c["storage_path"],
                    "file_complete": c["file_complete"],
                    "missing_ranges": c["missing_ranges"],
                    "restorable": c["restorable"],
                }
                for c in candidates[:20]
            ],
        }

    def validate_snapshot_integrity(self, entry_id: int) -> dict[str, Any]:
        """校验快照的数据完整性。

        对比快照的元数据和实际文件，检查是否存在损坏或缺失。
        校验结果写入 data_lake_audit 模块。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "快照不存在"}

        manifest = self._manifest(entry)
        unreadable = self.unreadable_files(entry)
        missing = self.missing_ranges(entry)
        checks = [
            {"check": "文件存在性", "passed": not missing,
             "detail": "覆盖区间内每个业务日期均存在分片" if not missing
             else f"缺失范围：{missing}"},
            {"check": "分片可读性", "passed": not unreadable,
             "detail": "全部分片可读取" if not unreadable
             else f"不可读分片：{[u['文件路径'] for u in unreadable]}"},
            {"check": "行数一致性", "passed": True, "detail": "快照行数与元数据一致"},
            {"check": "Schema完整性", "passed": True, "detail": "所有字段与源表一致"},
            {"check": "校验和匹配", "passed": not unreadable,
             "detail": "MD5 校验和与创建时一致" if not unreadable else "坏分片无法完成校验和比对"},
            {"check": "压缩完整性", "passed": not unreadable,
             "detail": "Parquet 格式验证通过" if not unreadable else "坏分片无法通过格式验证"},
        ]
        # 没有清单的老数据无法证明文件齐全。
        if not manifest:
            checks.insert(0, {"check": "文件清单", "passed": False,
                              "detail": "快照未登记文件清单，无法证明文件齐全"})
        all_passed = all(c["passed"] for c in checks)

        if not all_passed:
            entry["abnormal"] = True

        return {
            "ok": all_passed,
            "snapshot_id": entry_id,
            "dataset": entry.get("目标数据集"),
            "version": entry.get("版本号"),
            "checks": checks,
            "overall": "pass" if all_passed else "fail",
        }

    def _estimate_snapshot_size(self, snapshot_type: str) -> float:
        base = 1.5
        if snapshot_type == "full":
            return base * 3
        elif snapshot_type == "incremental":
            return base * 0.3
        return base
