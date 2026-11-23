"""快照恢复核对路由：把快照查询/覆盖核对/恢复计划/内容对照能力开放给值班台。

路径统一放在 /api/snapshot-restore 下，避免与通用资源路由 /api/{module} 冲突。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services.cold_restore import ColdRestoreService
from app.services.snapshot_creator import SnapshotCreatorService

router = APIRouter(prefix="/api/snapshot-restore", tags=["快照恢复核对"])

snapshot_service = SnapshotCreatorService()
restore_service = ColdRestoreService()


class RestorePlanPayload(BaseModel):
    task_id: int
    snapshot_id: int
    business_start: str | None = None
    business_end: str | None = None


class FaultPayload(BaseModel):
    file_path: str | None = None
    reason: str | None = None


class ComparePayload(BaseModel):
    snapshot_id_a: int
    snapshot_id_b: int


def _require_ok(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok", False):
        raise HTTPException(status_code=400, detail=result.get("message", "操作失败"))
    return result


@router.get("/snapshots")
def query_snapshots(
    dataset: str = Query(..., description="目标数据集，如 ods_orders"),
    business_date: str | None = Query(default=None, description="按业务日期筛选候选"),
) -> dict[str, Any]:
    """按数据集检索快照候选，可再按业务日期筛选；区分生成时间与覆盖业务日期。"""
    return snapshot_service.query_dataset_snapshots(dataset, business_date)


@router.get("/snapshots/{snapshot_id}/coverage")
def snapshot_coverage(snapshot_id: int) -> dict[str, Any]:
    return _require_ok(snapshot_service.get_snapshot_coverage(snapshot_id))


@router.post("/snapshots/{snapshot_id}/faults")
def inject_fault(snapshot_id: int, payload: FaultPayload) -> dict[str, Any]:
    """模拟「随后有一个文件不可读」，用于恢复前核对演练。"""
    return _require_ok(
        snapshot_service.mark_file_unreadable(snapshot_id, payload.file_path, payload.reason)
    )


@router.delete("/snapshots/{snapshot_id}/faults")
def clear_faults(snapshot_id: int) -> dict[str, Any]:
    """清除快照上的全部模拟故障，恢复所有分片可读，便于重复演练。"""
    entry = snapshot_service.get_snapshot_coverage(snapshot_id)
    if not entry.get("ok"):
        raise HTTPException(status_code=400, detail=entry.get("message", "快照不存在"))
    from app.store import store

    snap = store.find(snapshot_service.MODULE, snapshot_id)
    for f in snap.get("文件清单", []):
        f["可读"] = True
        f.pop("不可读原因", None)
    snap["abnormal"] = False
    return {
        "ok": True,
        "snapshot_id": snapshot_id,
        "file_complete": snapshot_service.is_file_complete(snap),
        "message": "模拟故障已清除，全部分片恢复可读",
    }


@router.post("/snapshots/compare")
def compare_snapshots(payload: ComparePayload) -> dict[str, Any]:
    """对照两份快照：分区是否一致、逐文件校验和与内容是否相同。"""
    return _require_ok(
        snapshot_service.compare_snapshot_contents(
            payload.snapshot_id_a, payload.snapshot_id_b
        )
    )


@router.get("/restore-tasks/{task_id}/candidates")
def task_candidates(
    task_id: int,
    business_start: str | None = None,
    business_end: str | None = None,
) -> dict[str, Any]:
    """冷数据恢复任务视角：查询本次业务日期范围内可选用的快照版本。"""
    return _require_ok(
        restore_service.find_candidate_snapshots_for_task(
            task_id, business_start, business_end
        )
    )


@router.post("/restore-plans")
def build_restore_plan(payload: RestorePlanPayload) -> dict[str, Any]:
    """基于单一快照版本生成恢复计划；缺件时返回不能开工及原因。"""
    return restore_service.build_snapshot_restore_plan(
        payload.task_id,
        payload.snapshot_id,
        payload.business_start,
        payload.business_end,
    )


@router.get("/restore-tasks/{task_id}/plan")
def get_restore_plan(task_id: int) -> dict[str, Any]:
    """定位一份已生成的恢复计划，核对能否开工以及实际使用的快照版本与文件。"""
    from app.store import store

    task = store.find(restore_service.MODULE, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"恢复任务 {task_id} 不存在")
    plan = task.get("恢复计划")
    if not plan:
        return {
            "ok": False,
            "task_id": task_id,
            "message": "该恢复任务尚未生成快照恢复计划",
        }
    return plan


@router.post("/restore-tasks/{task_id}/reset")
def reset_restore_task(task_id: int) -> dict[str, Any]:
    """把恢复任务复位为排队中并清除历史计划，便于值班员重复核对演练。"""
    from app.store import store

    task = store.find(restore_service.MODULE, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"恢复任务 {task_id} 不存在")
    task["status"] = "排队中"
    task["恢复状态"] = "排队中"
    task["pending"] = True
    task.pop("恢复计划", None)
    task.pop("关联快照版本", None)
    task.pop("关联快照ID", None)
    return {"ok": True, "task_id": task_id, "message": "恢复任务已复位为排队中"}
