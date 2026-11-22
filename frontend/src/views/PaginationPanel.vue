<template>
  <section class="pagination-panel" data-testid="pagination-panel">
    <header class="panel-head">
      <div>
        <h3>分页试采边界校验</h3>
        <p class="panel-desc">用「1 条记录 → 空页（带 nextToken）→ 后续数据 → 重复 token」的端点核对分页边界。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="pagination-start" type="button" :disabled="busy" @click="start">
          {{ busy ? '试采中…' : '启动探针试采' }}
        </button>
        <button class="btn" data-testid="pagination-accept" type="button" :disabled="busy || !view" @click="accept">
          写入验收
        </button>
      </div>
    </header>

    <p v-if="message" class="panel-message" data-testid="pagination-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="pagination-error">{{ errorMessage }}</p>

    <template v-if="view">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">判定停止原因</span>
          <strong class="stat-value">{{ view.boundary_result.stop_reason || '未结束' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">采集行数</span>
          <strong class="stat-value">{{ view.collected.row_count }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">最后确认来源页</span>
          <strong class="stat-value">{{ view.boundary_result.final_source_page ?? '—' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">清单/待办对齐</span>
          <strong class="stat-value">{{ view.checklist.consistent ? '一致' : '不一致' }}</strong>
        </article>
      </div>

      <table class="data-table" data-testid="pagination-pages">
        <thead>
          <tr>
            <th>页</th><th>边界判定</th><th>行数</th><th>返回 token</th><th>待办状态</th><th>确认来源页</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in view.checklist.checklist" :key="`${item.page}-${item.boundary}`">
            <td>{{ item.page }}</td>
            <td>{{ item.boundary }} · {{ item.boundary_label }}</td>
            <td>{{ item.row_count }}</td>
            <td>{{ item.token ?? '—' }}</td>
            <td>{{ item.todo_state }}</td>
            <td>{{ item.source_page ?? '—' }}</td>
          </tr>
        </tbody>
      </table>

      <div class="panel-split">
        <div>
          <h4>恢复位置</h4>
          <p data-testid="pagination-recovery">
            下一页第 {{ view.recovery.next_page }} 页，token：{{ view.recovery.next_token ?? '—' }}，
            请求序号 {{ view.recovery.next_sequence }}。
          </p>
        </div>
        <div>
          <h4>字段差异</h4>
          <ul data-testid="pagination-diffs">
            <li v-for="diff in view.field_diffs" :key="diff.field">
              [{{ diff.level }}] {{ diff.field }}：{{ diff.new_definition }}
            </li>
          </ul>
        </div>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import {
  acceptPaginationTrial,
  loadPaginationAcceptance,
  startProbeTrial,
  type PaginationAcceptance,
} from '@/api/backend-service'

const view = ref<PaginationAcceptance | null>(null)
const busy = ref(false)
const message = ref('')
const errorMessage = ref('')

async function start() {
  busy.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    const started = await startProbeTrial()
    view.value = await loadPaginationAcceptance(started.trial_id)
    message.value = `试采 ${started.trial_id} 已完成边界核对`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '试采失败'
  } finally {
    busy.value = false
  }
}

async function accept() {
  if (!view.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    await acceptPaginationTrial(view.value.trial_id)
    view.value = await loadPaginationAcceptance(view.value.trial_id)
    message.value = view.value
      ? `验收结果已写入字段差异（状态：${view.value.status}）`
      : '验收结果已写入'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '验收失败'
  } finally {
    busy.value = false
  }
}
</script>
