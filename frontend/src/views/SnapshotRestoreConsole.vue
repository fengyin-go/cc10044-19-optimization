<template>
  <section class="page restore-console">
    <header class="page-head">
      <div>
        <h2>快照恢复核对台</h2>
        <p class="page-desc">
          按数据集检索快照、用业务日期筛选候选；区分「快照生成时间」与「实际覆盖业务日期」，
          核对覆盖缺口与分片可读性后，再基于单一快照版本生成冷数据恢复计划。
        </p>
      </div>
    </header>

    <!-- 第一步：按数据集 + 业务日期检索候选 -->
    <form class="analysis-form" @submit.prevent="loadCandidates">
      <label>
        目标数据集
        <input v-model="dataset" data-testid="dataset-input" placeholder="如 ods_orders" required />
      </label>
      <label>
        业务日期筛选（可空）
        <input v-model="businessDate" data-testid="business-date-input" type="date" />
      </label>
      <label>
        恢复任务ID
        <input v-model.number="taskId" data-testid="task-input" type="number" min="1" required />
      </label>
      <button class="btn primary" data-testid="query-snapshots" type="submit">查询快照候选</button>
      <button class="btn ghost" type="button" @click="resetQuery">重置</button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <div v-if="queryResult" class="stat-row">
      <article class="stat-card">
        <span class="stat-label">候选快照（已完成）</span>
        <strong class="stat-value">{{ queryResult.total }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">文件齐全可恢复</span>
        <strong class="stat-value">{{ queryResult.restorable_count }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">缺件/坏件不可恢复</span>
        <strong class="stat-value">{{ queryResult.total - queryResult.restorable_count }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">非已完成已排除</span>
        <strong class="stat-value">{{ queryResult.excluded_non_completed.length }}</strong>
      </article>
    </div>

    <!-- 候选清单：版本、覆盖日、缺失范围分开写清 -->
    <table v-if="queryResult" class="data-table candidate-table">
      <thead>
        <tr>
          <th>版本号</th>
          <th>快照生成时间</th>
          <th>覆盖业务日期</th>
          <th>分片（可读/总数）</th>
          <th>缺失范围</th>
          <th>内容校验和</th>
          <th>文件齐全 / 可恢复</th>
          <th>核对操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="candidate in queryResult.candidates" :key="candidate.snapshot_id"
            :data-testid="`candidate-${candidate.snapshot_id}`"
            :class="{ 'row-blocked': !candidate.restorable }">
          <td>
            <div class="cell-strong">#{{ candidate.snapshot_id }} {{ candidate.version }}</div>
            <div class="cell-sub">{{ candidate.storage_path }}</div>
          </td>
          <td>
            <div>{{ formatTime(candidate.generated_at) }}</div>
            <div class="cell-sub">任务状态：{{ candidate.status }}</div>
          </td>
          <td>
            <div class="cell-strong">{{ candidate.coverage_start }} ~ {{ candidate.coverage_end }}</div>
            <div class="cell-sub">{{ candidate.covered_dates.join('、') || '无覆盖声明' }}</div>
          </td>
          <td>
            <span :class="candidate.file_readable === candidate.file_total ? 'badge ok' : 'badge warn'">
              {{ candidate.file_readable }} / {{ candidate.file_total }}
            </span>
            <div v-if="candidate.unreadable_files.length" class="cell-sub danger">
              <div v-for="f in candidate.unreadable_files" :key="f.文件路径">
                不可读：{{ f.业务日期 }}
              </div>
            </div>
          </td>
          <td>
            <span v-if="!candidate.missing_ranges.length" class="badge ok">无缺失</span>
            <div v-else class="gap-list">
              <div v-for="(gap, idx) in candidate.missing_ranges" :key="idx" class="gap-item">
                <span class="badge danger">{{ gap.原因 }}</span>
                <span class="cell-sub">{{ gap.start === gap.end ? gap.start : `${gap.start} ~ ${gap.end}` }}</span>
              </div>
            </div>
          </td>
          <td><code>{{ candidate.content_checksum || '—' }}</code></td>
          <td>
            <span :class="candidate.file_complete ? 'badge ok' : 'badge danger'">
              {{ candidate.file_complete ? '文件齐全' : '文件不齐全' }}
            </span>
            <div class="cell-sub">{{ candidate.restorable ? '可恢复' : '不可恢复' }}</div>
          </td>
          <td class="row-actions vertical">
            <button class="link" type="button"
                    :data-testid="`plan-${candidate.snapshot_id}`"
                    @click="submitPlan(candidate.snapshot_id)">
              生成恢复计划
            </button>
            <button class="link" type="button"
                    :data-testid="`fault-${candidate.snapshot_id}`"
                    @click="injectFault(candidate.snapshot_id)">
              模拟文件不可读
            </button>
            <button class="link" type="button"
                    :data-testid="`clear-${candidate.snapshot_id}`"
                    @click="clearFault(candidate.snapshot_id)">
              清除故障
            </button>
            <button class="link" type="button"
                    :data-testid="`pick-compare-${candidate.snapshot_id}`"
                    @click="pickForCompare(candidate.snapshot_id)">
              {{ comparePickA === candidate.snapshot_id ? '已选为对照A' : '选为对照样本' }}
            </button>
          </td>
        </tr>
        <tr v-if="!queryResult.candidates.length">
          <td colspan="8" class="empty-state">该数据集下没有已完成快照候选</td>
        </tr>
      </tbody>
    </table>

    <!-- 第二步：快照对照 -->
    <section v-if="queryResult" class="panel">
      <h3>同覆盖日快照内容对照</h3>
      <div class="compare-bar">
        <span>对照A：快照 <strong>{{ comparePickA ?? '未选择' }}</strong></span>
        <span>对照B：快照 <strong>{{ comparePickB ?? '未选择' }}</strong></span>
        <button class="btn" data-testid="run-compare" type="button"
                :disabled="!comparePickA || !comparePickB || comparePickA === comparePickB"
                @click="runCompare">
          执行对照
        </button>
      </div>
      <div v-if="compareResult" class="compare-result" data-testid="compare-result">
        <p :class="compareResult.content_identical ? 'verdict ok' : 'verdict danger'">
          结论：{{ compareResult.verdict }}
        </p>
        <ul class="kv-list">
          <li>覆盖窗口相同：{{ compareResult.same_coverage_window ? '是' : '否' }}</li>
          <li>分区一致：{{ compareResult.same_partitions ? '是' : '否' }}</li>
          <li>内容校验和一致：{{ compareResult.content_identical ? '是' : '否' }}</li>
          <li>可完整验证（无坏件）：{{ compareResult.fully_verifiable ? '是' : '否' }}</li>
          <li>A 侧不可读文件：{{ compareResult.unreadable_files_a.length }} 个</li>
          <li>B 侧不可读文件：{{ compareResult.unreadable_files_b.length }} 个</li>
        </ul>
        <table v-if="compareResult.checksum_mismatches.length" class="data-table inner-table">
          <thead>
            <tr><th>业务日期</th><th>A 校验和</th><th>B 校验和</th></tr>
          </thead>
          <tbody>
            <tr v-for="m in compareResult.checksum_mismatches" :key="m.业务日期">
              <td>{{ m.业务日期 }}</td>
              <td><code>{{ m.snapshot_a_checksum }}</code></td>
              <td><code>{{ m.snapshot_b_checksum }}</code></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 第三步：恢复计划核对 -->
    <section class="panel">
      <h3>恢复计划核对（任务 #{{ taskId }}）</h3>
      <p class="cell-sub">
        计划只允许使用单一快照版本，文件清单逐文件关联实际快照；所选版本缺件时不能开工。
      </p>
      <div v-if="plan" data-testid="plan-panel">
        <p :class="plan.can_start ? 'verdict ok' : 'verdict danger'" data-testid="plan-verdict">
          {{ plan.can_start ? `可以开工：使用版本 ${plan.snapshot_version}` : `不能开工：${plan.message}` }}
        </p>
        <ul class="kv-list">
          <li>所用快照版本：<strong>{{ plan.snapshot_version }}</strong>（快照 #{{ plan.snapshot_id }}）</li>
          <li>快照生成时间：{{ formatTime(plan.generated_at) }}</li>
          <li>快照覆盖业务日期：{{ plan.coverage_start }} ~ {{ plan.coverage_end }}</li>
          <li>本次请求范围：{{ plan.requested_range.join(' ~ ') }}</li>
          <li>待恢复文件数：{{ plan.files_to_restore }}</li>
          <li>单一快照约束：{{ plan.single_snapshot_only ? '已启用，禁止混用分片' : '未启用' }}</li>
        </ul>

        <div v-if="plan.blocked_reasons.length" class="blocked-box">
          <strong>阻断原因：</strong>
          <ul>
            <li v-for="(reason, idx) in plan.blocked_reasons" :key="idx">{{ reason }}</li>
          </ul>
        </div>

        <table v-if="plan.files.length" class="data-table inner-table" data-testid="plan-files">          <thead>
            <tr>
              <th>业务日期</th><th>关联快照版本</th><th>源文件路径</th>
              <th>文件校验和</th><th>恢复目标路径</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(file, idx) in plan.files" :key="idx">
              <td>{{ file.业务日期 }}</td>
              <td><code>{{ file.快照版本 }}</code></td>
              <td class="path-cell">{{ file.文件路径 }}</td>
              <td><code>{{ file.文件校验和 }}</code></td>
              <td class="path-cell">{{ file.恢复目标路径 }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="cell-sub">在上方候选中点击「生成恢复计划」，结果会显示在这里。</p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import {
  buildRestorePlan,
  clearFaults as clearFaultsApi,
  compareSnapshots,
  injectFault as injectFaultApi,
  querySnapshots,
  type RestorePlan,
  type SnapshotCompare,
  type SnapshotQueryResult,
} from '@/api/snapshot-restore'

const dataset = ref('ods_orders')
const businessDate = ref('')
const taskId = ref(4)
const errorMessage = ref('')
const queryResult = ref<SnapshotQueryResult | null>(null)
const plan = ref<RestorePlan | null>(null)
const comparePickA = ref<number | null>(null)
const comparePickB = ref<number | null>(null)
const compareResult = ref<SnapshotCompare | null>(null)

async function loadCandidates() {
  errorMessage.value = ''
  try {
    queryResult.value = await querySnapshots(dataset.value.trim(), businessDate.value.trim())
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '快照候选查询失败'
  }
}

function resetQuery() {
  dataset.value = 'ods_orders'
  businessDate.value = ''
  queryResult.value = null
  plan.value = null
  compareResult.value = null
  comparePickA.value = null
  comparePickB.value = null
}

async function submitPlan(snapshotId: number) {
  errorMessage.value = ''
  try {
    plan.value = await buildRestorePlan(taskId.value, snapshotId)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复计划生成失败'
  }
}

async function injectFault(snapshotId: number) {
  errorMessage.value = ''
  try {
    await injectFaultApi(snapshotId)
    await loadCandidates()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '故障注入失败'
  }
}

async function clearFault(snapshotId: number) {
  errorMessage.value = ''
  try {
    await clearFaultsApi(snapshotId)
    await loadCandidates()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清除故障失败'
  }
}

function pickForCompare(snapshotId: number) {
  if (comparePickA.value === null) {
    comparePickA.value = snapshotId
    return
  }
  if (comparePickA.value === snapshotId) {
    comparePickA.value = null
    return
  }
  comparePickB.value = snapshotId
}

async function runCompare() {
  if (comparePickA.value === null || comparePickB.value === null) return
  errorMessage.value = ''
  try {
    compareResult.value = await compareSnapshots(comparePickA.value, comparePickB.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '快照对照失败'
  }
}

function formatTime(value: string | undefined): string {
  if (!value) return '—'
  return value.replace('T', ' ').replace('Z', ' UTC')
}

// 进入页面即载入默认订单数据集候选。
void loadCandidates()
</script>

<style scoped>
.restore-console .panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  margin: 16px 0;
}
.restore-console h3 { margin: 0 0 10px; font-size: 15px; }
.candidate-table .cell-strong { font-weight: 600; }
.candidate-table .cell-sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
.candidate-table .cell-sub.danger { color: #b42318; }
.candidate-table tr.row-blocked { background: #fff7f6; }
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  white-space: nowrap;
}
.badge.ok { background: #dcfce7; color: #166534; }
.badge.warn { background: #fef3c7; color: #92400e; }
.badge.danger { background: #fee2e2; color: #991b1b; }
.gap-list { display: flex; flex-direction: column; gap: 4px; }
.gap-item { display: flex; align-items: center; gap: 6px; }
.row-actions.vertical { flex-direction: column; align-items: flex-start; gap: 4px; }
.compare-bar { display: flex; gap: 18px; align-items: center; font-size: 13px; margin-bottom: 10px; }
.compare-result { border-top: 1px solid var(--border); padding-top: 10px; }
.verdict { font-weight: 600; padding: 8px 12px; border-radius: 6px; }
.verdict.ok { background: #dcfce7; color: #166534; }
.verdict.danger { background: #fee2e2; color: #991b1b; }
.kv-list { list-style: none; padding: 0; margin: 8px 0; font-size: 13px; display: grid; grid-template-columns: 1fr 1fr; gap: 4px 16px; }
.inner-table { margin-top: 10px; }
.path-cell { font-family: monospace; font-size: 12px; word-break: break-all; }
.blocked-box {
  background: #fff7f6;
  border: 1px solid #fecaca;
  border-radius: 6px;
  padding: 8px 12px;
  margin: 10px 0;
  font-size: 13px;
}
.blocked-box ul { margin: 6px 0 0; padding-left: 18px; }
</style>
