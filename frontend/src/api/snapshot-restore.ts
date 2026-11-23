/** 快照恢复核对台接口封装：区分快照生成时间与覆盖业务日期，串起查询→核对→计划→对照。 */
import { request } from './client'

export type MissingRange = {
  start: string
  end: string
  业务日期: string[]
  原因: '缺少分片' | '分片不可读' | string
  说明: string
}

export type UnreadableFile = {
  文件路径: string
  业务日期: string
  文件校验和: string
  不可读原因: string
}

export type SnapshotCandidate = {
  snapshot_id: number
  version: string
  dataset: string
  status: string
  generated_at: string
  snapshot_type: string
  storage_path: string
  coverage_start: string
  coverage_end: string
  covered_dates: string[]
  missing_ranges: MissingRange[]
  file_total: number
  file_readable: number
  unreadable_files: UnreadableFile[]
  file_complete: boolean
  restorable: boolean
  content_checksum: string
  plan_can_start?: boolean
}

export type SnapshotQueryResult = {
  ok: boolean
  query: { dataset: string; business_date: string | null }
  total: number
  restorable_count: number
  candidates: SnapshotCandidate[]
  excluded_non_completed: { snapshot_id: number; status: string; version: string | null }[]
}

export type RestorePlanFile = {
  业务日期: string
  快照版本: string
  snapshot_id: number
  文件路径: string
  文件校验和: string
  恢复目标路径: string
}

export type RestorePlan = {
  ok: boolean
  can_start: boolean
  task_id: number
  dataset: string
  snapshot_id: number
  snapshot_version: string
  content_checksum: string
  generated_at: string
  coverage_start: string
  coverage_end: string
  requested_range: [string, string]
  files_to_restore: number
  files: RestorePlanFile[]
  uncovered_dates: string[]
  blocked_reasons: string[]
  single_snapshot_only: boolean
  message: string
}

export type SnapshotCompare = {
  ok: boolean
  dataset: string
  snapshot_a: {
    snapshot_id: number
    version: string
    generated_at: string
    coverage_start: string
    coverage_end: string
    content_checksum: string
    file_complete: boolean
  }
  snapshot_b: {
    snapshot_id: number
    version: string
    generated_at: string
    coverage_start: string
    coverage_end: string
    content_checksum: string
    file_complete: boolean
  }
  same_coverage_window: boolean
  same_partitions: boolean
  partitions_only_in_a: string[]
  partitions_only_in_b: string[]
  mutually_readable_dates: string[]
  checksum_mismatches: {
    业务日期: string
    snapshot_a_checksum: string
    snapshot_b_checksum: string
  }[]
  unreadable_files_a: UnreadableFile[]
  unreadable_files_b: UnreadableFile[]
  content_identical: boolean
  fully_verifiable: boolean
  verdict: string
}

async function readJson<T>(response: Response): Promise<T> {
  return (await response.json()) as T
}

export async function querySnapshots(dataset: string, businessDate = ''): Promise<SnapshotQueryResult> {
  const params = new URLSearchParams({ dataset })
  if (businessDate) params.set('business_date', businessDate)
  const response = await request(`/api/snapshot-restore/snapshots?${params.toString()}`)
  if (!response.ok) throw new Error('快照候选查询失败')
  return readJson<SnapshotQueryResult>(response)
}

export async function fetchSnapshotCoverage(snapshotId: number): Promise<SnapshotCandidate & { manifest: unknown[] }> {
  const response = await request(`/api/snapshot-restore/snapshots/${snapshotId}/coverage`)
  if (!response.ok) throw new Error('快照覆盖详情读取失败')
  return readJson(response)
}

export async function injectFault(snapshotId: number): Promise<{ ok: boolean; message: string }> {
  const response = await request(`/api/snapshot-restore/snapshots/${snapshotId}/faults`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
  return readJson(response)
}

export async function clearFaults(snapshotId: number): Promise<{ ok: boolean; message: string }> {
  const response = await request(`/api/snapshot-restore/snapshots/${snapshotId}/faults`, {
    method: 'DELETE',
  })
  return readJson(response)
}

export async function compareSnapshots(
  snapshotIdA: number,
  snapshotIdB: number,
): Promise<SnapshotCompare> {
  const response = await request('/api/snapshot-restore/snapshots/compare', {
    method: 'POST',
    body: JSON.stringify({ snapshot_id_a: snapshotIdA, snapshot_id_b: snapshotIdB }),
  })
  if (!response.ok) throw new Error('快照对照失败')
  return readJson<SnapshotCompare>(response)
}

export async function buildRestorePlan(
  taskId: number,
  snapshotId: number,
): Promise<RestorePlan> {
  const response = await request('/api/snapshot-restore/restore-plans', {
    method: 'POST',
    body: JSON.stringify({ task_id: taskId, snapshot_id: snapshotId }),
  })
  if (!response.ok) throw new Error('恢复计划生成失败')
  return readJson<RestorePlan>(response)
}

export async function fetchRestorePlan(taskId: number): Promise<RestorePlan> {
  const response = await request(`/api/snapshot-restore/restore-tasks/${taskId}/plan`)
  if (!response.ok) throw new Error('恢复计划读取失败')
  return readJson<RestorePlan>(response)
}
