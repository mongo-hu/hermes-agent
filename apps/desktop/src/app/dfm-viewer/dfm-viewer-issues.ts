export interface IssueTypeSource {
  check_id?: string
  issue_type_id?: string
  issue_type_label?: string
  metric_id?: string
}

export interface ViewerIssueTypeCount {
  count: number
  issue_type_id: string
  label: string
}

export function formatViewerValue(value: unknown): string {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? String(Number(value.toPrecision(12))) : String(value)
  }

  if (Array.isArray(value) && value.length === 2) {
    return `[${formatViewerValue(value[0])}, ${formatViewerValue(value[1])}]`
  }

  if (value && typeof value === 'object') {
    const range = value as { lower?: unknown; upper?: unknown }

    if (range.lower !== undefined && range.upper !== undefined) {
      return `[${formatViewerValue(range.lower)}, ${formatViewerValue(range.upper)}]`
    }

    return JSON.stringify(value)
  }

  return String(value ?? '—')
}

export function classifyViewerIssueType(issue: IssueTypeSource): [string, string] {
  const checkId = issue.check_id?.trim() ?? ''
  const metricId = issue.metric_id?.trim() ?? ''
  const issueTypeId = issue.issue_type_id?.trim() || checkId || metricId || 'check.unknown'
  const explicitLabel = issue.issue_type_label?.trim()

  if (explicitLabel) {
    return [issueTypeId, explicitLabel]
  }

  const searchable = `${checkId} ${issueTypeId} ${metricId}`.toLowerCase()

  if (searchable.includes('boss') && (searchable.includes('wall') || searchable.includes('thickness'))) {
    return [issueTypeId, '螺钉柱柱壁问题']
  }

  if (searchable.includes('rib') && searchable.includes('thickness')) {
    return [issueTypeId, '加强筋厚度问题']
  }

  if (searchable.includes('draft')) {
    return [issueTypeId, '拔模角问题']
  }

  if (searchable.includes('wall_thickness') || searchable.includes('minimum_thickness')) {
    return [issueTypeId, '壁厚问题']
  }

  if (searchable.includes('undercut')) {
    return [issueTypeId, '倒扣问题']
  }

  if (searchable.includes('radius') || searchable.includes('fillet')) {
    return [issueTypeId, '圆角半径问题']
  }

  return [issueTypeId, issueTypeId]
}

export function summarizeViewerIssueTypes(issues: IssueTypeSource[]): ViewerIssueTypeCount[] {
  const groups = new Map<string, ViewerIssueTypeCount>()

  for (const issue of issues) {
    const [issueTypeId, label] = classifyViewerIssueType(issue)
    const current = groups.get(issueTypeId)

    if (current) {
      current.count += 1
    } else {
      groups.set(issueTypeId, { count: 1, issue_type_id: issueTypeId, label })
    }
  }

  return Array.from(groups.values())
}
