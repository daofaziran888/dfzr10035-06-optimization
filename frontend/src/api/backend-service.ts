import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type PaginationPage = {
  page: number
  sequence?: number
  row_count: number
  boundary: string
  boundary_label: string
  token: string | null
  request_token?: string | null
}

export type PaginationChecklistItem = {
  trial_id: string
  page: number
  boundary: string
  boundary_label: string
  row_count: number
  token: string | null
  todo_id: number | null
  todo_state: string
  source_page: number | null
}

export type PaginationAcceptance = {
  trial_id: string
  endpoint_id: string
  status: string
  boundary_result: {
    stop_reason: string
    final_source_page: number | null
    tail_confirmed: boolean
    empty_page_seen: boolean
    duplicate_pages: number[]
    loop_stopped: boolean
    last_confirmed_page: number
  }
  collected: {
    row_count: number
    requests: number
    pages: PaginationPage[]
  }
  recovery: {
    next_page: number
    next_token: string | null
    next_sequence: number
  }
  checklist: {
    checklist: PaginationChecklistItem[]
    todos: Record<string, unknown>[]
    consistent: boolean
    todo_count: number
  }
  field_diffs: {
    field: string
    old_definition: string
    new_definition: string
    level: string
    note: string
  }[]
}

export async function startProbeTrial(trialId?: string): Promise<{ trial_id: string }> {
  const response = await request('/api/pagination/trials', {
    method: 'POST',
    body: JSON.stringify({
      trial_id: trialId,
      endpoint_id: 'ENDP-PAGE-PROBE',
      use_probe: true,
    }),
  })
  if (!response.ok) throw new Error('分页试采启动失败')
  return await response.json()
}

export async function loadPaginationAcceptance(trialId: string): Promise<PaginationAcceptance> {
  const response = await request(`/api/pagination/trials/${encodeURIComponent(trialId)}/acceptance`)
  if (!response.ok) throw new Error('分页试采结果读取失败')
  return await response.json()
}

export async function acceptPaginationTrial(trialId: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/pagination/trials/${encodeURIComponent(trialId)}/accept`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('分页试采验收失败')
  return await response.json()
}
