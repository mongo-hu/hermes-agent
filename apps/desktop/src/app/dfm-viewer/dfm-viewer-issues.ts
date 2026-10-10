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

const CHECK_LABELS_ZH: Record<string, string> = {
  C_BOSS_DRAFT: '普通凸台拔模角问题',
  C_BOSS_ROOT_FILLET: '普通凸台根部圆角问题',
  C_HOLE_BOTTOM_THK: '一般孔孔底厚度问题',
  C_HOLE_CLEARANCE: '一般孔孔边距/孔间距问题',
  C_HOLE_DRAFT: '一般孔孔壁拔模角问题',
  C_HOLE_THIN_STEEL: '一般孔薄钢问题',
  C_RIB_DRAFT: '加强筋拔模角问题',
  C_RIB_HEIGHT_RATIO: '加强筋高度比问题',
  C_RIB_ROOT_FILLET: '加强筋根部圆角问题',
  C_RIB_THK_RATIO: '加强筋厚度比问题',
  C_SCREW_BOSS_BOTTOM_THK: '螺钉柱孔底厚度问题',
  C_SCREW_BOSS_DRAFT: '螺钉柱拔模角问题',
  C_SCREW_BOSS_FILLET: '螺钉柱圆角问题',
  C_SCREW_BOSS_HOLE_DEPTH: '螺钉柱孔芯深度问题',
  C_SCREW_BOSS_THIN_STEEL: '螺钉柱薄钢问题',
  C_SCREW_BOSS_WALL_THK: '螺钉柱柱壁问题',
  C_WALL_DRAFT: '主体壁拔模角问题',
  C_WALL_FILLET: '主体壁圆角问题',
  C_WALL_THK_RANGE: '主体壁厚范围问题',
  C_WALL_THK_TRANSITION: '主体壁厚过渡问题'
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

  const mappedLabel =
    CHECK_LABELS_ZH[checkId.toUpperCase()] ?? CHECK_LABELS_ZH[issueTypeId.toUpperCase()]

  if (mappedLabel) {
    return [issueTypeId, mappedLabel]
  }

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
