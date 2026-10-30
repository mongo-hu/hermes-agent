import type { GeometryReference } from './dfm-viewer-geometry'

export interface ReviewFeature {
  feature_id: string
  kind: string
  confidence: number
  properties: {
    geometry_refs?: GeometryReference[]
    subtype?: string
    method?: string
    diagnostics?: Record<string, unknown>
    [key: string]: unknown
  }
}

export interface ReviewScope {
  forming_scope_id: string
  kind: string
  mode: 'main' | 'local'
  draw_direction: number[] | null
  geometry_refs: GeometryReference[]
  status?: 'candidate' | 'confirmed' | 'stale'
}

export interface ReviewGroup {
  review_group_id: string
  kind: string
  target_refs: string[]
  status: 'pending_review' | 'confirmed' | 'stale'
}

export interface ReviewAnnotation {
  annotation_id: string
  kind: string
  value: string
  status: 'active' | 'stale'
  geometry_refs: GeometryReference[]
}

export interface AnnotationKind {
  kind: string
  geometry_kinds: Array<'face' | 'edge'>
  value_schema: { enum?: string[] }
  recognition_status: 'supported' | 'not_implemented'
}

export interface ReviewContext {
  project_id: string
  revision: number
  edit_timeout_ms?: number
  snapshot_id?: string
  features: ReviewFeature[]
  forming_scopes: ReviewScope[]
  review_groups: ReviewGroup[]
  annotations: ReviewAnnotation[]
  annotation_kinds?: AnnotationKind[]
}

/** Worker execution budget plus time for committing state and generating the Viewer. */
export function reviewEditTimeout(context: Pick<ReviewContext, 'edit_timeout_ms'>) {
  const value = context.edit_timeout_ms

  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : 960_000
}

export type ReviewCategory = 'structure' | 'annotation' | 'forming'

export const REVIEW_CATEGORIES: Array<{ id: ReviewCategory; label: string }> = [
  { id: 'structure', label: '结构特征' },
  { id: 'annotation', label: '属性标注' },
  { id: 'forming', label: '出模设置' }
]

export interface ReviewNavigation {
  category: ReviewCategory
  objectId: string
  sequence: number
}

export function reviewCategory(group: ReviewGroup): ReviewCategory {
  return group.kind === 'ordinary_structure'
    ? 'structure'
    : group.kind === 'surface_attribute'
      ? 'annotation'
      : 'forming'
}

const LABELS: Record<string, string> = {
  main_wall: '主体壁',
  rib: '筋',
  hollow_boss: '带孔 Boss 柱',
  screw_boss: '带孔 Boss 柱',
  solid_boss: '实心 Boss 柱',
  drilled_hole: '孔',
  blend: '圆角',
  chamfer: '倒角',
  shaft: '轴状结构',
  cavity: '几何凹腔',
  appearance: '面属性 · 外观',
  mold_side: '面属性 · 定模／动模归属',
  forming_side: '面属性 · 型腔／型芯侧',
  parting_boundary: '边属性 · 分型边界',
  product_forming: '整件设置 · 主出模方向',
  undercut_scope: '第一轮 · 倒扣范围',
  local_structure: '第二轮 · 局部结构',
  local_direction: '第二轮 · 局部退出方向'
}

export function confirmedFeatureIds(groups: readonly ReviewGroup[]): Set<string> {
  const pending = new Set(
    groups
      .filter(group => group.kind === 'ordinary_structure' && group.status === 'pending_review')
      .flatMap(group => group.target_refs)
  )

  return new Set(
    groups
      .filter(group => group.kind === 'ordinary_structure' && group.status === 'confirmed')
      .flatMap(group => group.target_refs)
      .filter(id => !pending.has(id))
  )
}

export function featureNeedsPartReview(feature: ReviewFeature | undefined): boolean {
  const refs = feature?.properties.diagnostics?.unassigned_geometry_refs

  return Array.isArray(refs) && refs.length > 0
}

export function reviewTargetLabel(group: ReviewGroup): string {
  return group.kind === 'product_forming' ? '整件主出模设置' : group.target_refs[0]
}

export function reviewGroupNote(context: ReviewContext, group: ReviewGroup): string | null {
  if (group.kind === 'undercut_scope') {
    const scope = context.forming_scopes.find(item => item.forming_scope_id === group.target_refs[0])
    const faces = scope?.geometry_refs.filter(ref => ref.kind === 'face').length ?? 0

    return `${faces} 个面；确认范围后补充识别局部结构，再确认结构和退出方向。`
  }

  if (group.kind === 'local_structure') {
    const features = context.features.filter(feature => group.target_refs.includes(feature.feature_id))

    return features.length
      ? `关联结构：${features.map(feature => `${LABELS[feature.kind] ?? feature.kind} ${feature.feature_id}`).join('、')}。结构详情见“结构特征”。`
      : '该范围内的结构将由第二轮识别补充。'
  }

  if (group.kind === 'local_direction') {
    const scope = context.forming_scopes.find(item => item.forming_scope_id === group.target_refs[0])

    return scope?.draw_direction
      ? `局部退出方向 [${scope.draw_direction.join(', ')}]。${scope.status === 'candidate' ? '已暂存，尚未参与计算。' : ''}`
      : '局部退出方向尚未确认。'
  }

  if (group.kind !== 'product_forming') {
    return null
  }

  const scope = context.forming_scopes.find(item => item.forming_scope_id === group.target_refs[0])

  if (!scope) {
    return '整件主出模设置，不属于结构特征。'
  }

  const direction = scope.draw_direction ? `[${scope.draw_direction.join(', ')}]` : '尚未设置'
  const faces = scope.geometry_refs.filter(ref => ref.kind === 'face').length

  return `覆盖整件 ${faces} 个面；主出模方向 ${direction}。用于拔模角和倒扣分析，不计入结构特征数量。`
}

export function reviewBuckets(
  context: ReviewContext,
  includeConfirmed = false
): Array<{ id: string; category: ReviewCategory; label: string; groups: ReviewGroup[] }> {
  const buckets = new Map<string, { id: string; category: ReviewCategory; label: string; groups: ReviewGroup[] }>()
  const featureKinds = new Map(context.features.map(item => [item.feature_id, item.kind]))
  const annotationKinds = new Map(context.annotations.map(item => [item.annotation_id, item.kind]))

  for (const group of context.review_groups.filter(
    item =>
      item.status === 'pending_review' ||
      (includeConfirmed && item.status === 'confirmed' && item.kind !== 'ordinary_structure')
  )) {
    const kind =
      group.kind === 'ordinary_structure'
        ? (group.target_refs.map(ref => featureKinds.get(ref)).find(Boolean) ?? '结构')
        : group.kind === 'surface_attribute'
          ? (group.target_refs.map(ref => annotationKinds.get(ref)).find(Boolean) ?? 'surface_attribute')
          : group.kind

    const id = `${reviewCategory(group)}:${kind}${group.status === 'confirmed' ? ':confirmed' : ''}`
    const bucket = buckets.get(id) ?? { id, category: reviewCategory(group), label: LABELS[kind] ?? kind, groups: [] }
    bucket.groups.push(group)
    buckets.set(id, bucket)
  }

  return Array.from(buckets.values()).sort(
    (a, b) => Number(a.groups[0].status === 'confirmed') - Number(b.groups[0].status === 'confirmed')
  )
}

export function parseDirectionVector(text: string): number[] | null {
  try {
    const values: unknown = JSON.parse(text)

    if (
      !Array.isArray(values) ||
      values.length !== 3 ||
      values.some(value => typeof value !== 'number' || !Number.isFinite(value)) ||
      Math.hypot(...values) <= 1e-12 ||
      !Number.isFinite(Math.hypot(...values))
    ) {
      return null
    }

    return values
  } catch {
    return null
  }
}

export function reviewGeometry(context: ReviewContext, group: ReviewGroup): GeometryReference[] {
  const selected = new Set(group.target_refs)

  if (group.kind === 'surface_attribute') {
    return context.annotations
      .filter(item => item.status !== 'stale' && selected.has(item.annotation_id))
      .flatMap(item => item.geometry_refs)
  }

  const refs = [
    ...context.features
      .filter(item => selected.has(item.feature_id))
      .flatMap(item => item.properties.geometry_refs ?? []),
    ...context.forming_scopes.filter(item => selected.has(item.forming_scope_id)).flatMap(item => item.geometry_refs)
  ]

  return Array.from(
    new Map(
      refs.map(ref => [JSON.stringify([ref.input_sha256, ref.topology_snapshot_id, ref.kind, ref.index]), ref])
    ).values()
  )
}

export function directionUnlocked(context: ReviewContext, group: ReviewGroup): boolean {
  const scope = group.target_refs[0]

  return ['undercut_scope', 'local_structure'].every(kind =>
    context.review_groups.some(
      item => item.kind === kind && item.target_refs.includes(scope) && item.status === 'confirmed'
    )
  )
}

export function parseReviewDirection(values: string[]): number[] | null {
  if (values.length !== 3 || values.some(value => !value.trim())) {
    return null
  }

  const direction = values.map(Number)

  return direction.every(Number.isFinite) && direction.some(value => value !== 0) ? direction : null
}
