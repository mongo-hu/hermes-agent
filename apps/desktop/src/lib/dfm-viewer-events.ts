import type { DfmViewerTarget } from '@/store/dfm-viewer'

import type { GatewayEventPayload } from './chat-messages'

function record(value: unknown): Record<string, unknown> {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }

  if (typeof value !== 'string') {
    return {}
  }

  try {
    const parsed = JSON.parse(value) as unknown

    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : {}
  } catch {
    return {}
  }
}

function stringField(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) {
      return value.trim()
    }
  }

  return ''
}

export function dfmViewerTargetFromToolComplete(payload?: GatewayEventPayload): DfmViewerTarget | null {
  if (payload?.name !== 'dfm_project' && payload?.name !== 'dfm_analysis') {
    return null
  }

  const result = record(payload.result)
  const preview = record(result.preview)
  const manifestPath = stringField(payload.viewer_manifest, result.viewer_manifest, preview.viewer_manifest)

  if (!manifestPath) {
    return null
  }

  const completed = payload.name === 'dfm_analysis'

  if (completed && payload.status !== 'succeeded' && result.status !== 'succeeded') {
    return null
  }

  if (!completed && preview.status !== 'ready') {
    return null
  }

  return {
    manifestPath,
    projectId: stringField(payload.project_id, result.project_id) || undefined,
    runId: stringField(payload.run_id, result.run_id, preview.run_id) || undefined,
    status: completed ? 'completed' : 'preview'
  }
}

export function dfmHtmlReportPathFromToolComplete(payload?: GatewayEventPayload): string | null {
  if (payload?.name !== 'dfm_analysis') {
    return null
  }

  const result = record(payload.result)
  const run = record(result.run)
  const report = record(result.report)
  const status = stringField(run.status, result.status, payload.status)
  const kind = stringField(report.kind)
  const path = stringField(report.path)

  if (status !== 'succeeded' || kind !== 'report_html' || !path) {
    return null
  }

  return path
}
