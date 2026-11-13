import type { GeometryReference } from './dfm-viewer-geometry'
import { FEATURE_LABELS } from './dfm-feature-presentation'

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

export interface FormingCandidate {
  mechanism_kind: 'slider' | 'lifter' | 'undetermined'
  geometry_refs: GeometryReference[]
  draw_direction: number[] | null
  confidence?: number
  evidence?: Record<string, unknown>
}

export interface ReviewScope {
  forming_scope_id: string
  kind: string
  mode: 'main' | 'local'
  mechanism_kind: 'none' | 'slider' | 'lifter' | 'undetermined'
  undercut_geometry_refs: GeometryReference[]
  candidate_solutions: FormingCandidate[]
  coordinate_system: 'model'
  direction_source: 'inferred' | 'user_confirmed' | null
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
  viewer_manifest?: string | null
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

/** Capture this batch only; incomplete forming plans still require user input. */
export function confirmableReviewGroupIds(context: ReviewContext): string[] {
  return context.review_groups.filter(group => {
    if (group.status !== 'pending_review') {return false}

    if (group.kind === 'product_forming' || group.kind === 'forming_scope') {
      return formingPlanReady(context.forming_scopes.find(scope => group.target_refs.includes(scope.forming_scope_id)))
    }

    return group.kind === 'ordinary_structure' || group.kind === 'surface_attribute'
  }).map(group => group.review_group_id)
}

export function removableReviewGroup(context: ReviewContext, group: ReviewGroup): boolean {
  if (group.status !== 'pending_review') {return false}

  if (group.kind === 'forming_scope') {
    return context.forming_scopes.some(scope => group.target_refs.includes(scope.forming_scope_id) &&
      scope.mode === 'local' && scope.status !== 'stale')
  }

  return group.kind === 'ordinary_structure' || group.kind === 'surface_attribute'
}

const LABELS: Record<string, string> = {
  ...FEATURE_LABELS,
  appearance: '面属性 · 外观',
  mold_side: '面属性 · 定模／动模归属',
  forming_side: '面属性 · 型腔／型芯侧',
  parting_boundary: '边属性 · 分型边界',
  product_forming: '整件设置 · 主出模方向',
  forming_scope: '局部成型方案'
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
  if (group.kind === 'forming_scope') {
    const scope = context.forming_scopes.find(item => item.forming_scope_id === group.target_refs[0])

    if (!scope) {return '成型方案已变化，请刷新当前审核状态。'}
    const mechanisms = { none: '主方向', slider: '滑块', lifter: '斜顶', undetermined: '机构待确定' }
    const direction = scope.draw_direction ? `[${scope.draw_direction.join(', ')}]` : '方向待补充'

    return `倒扣面 ${scope.undercut_geometry_refs.length} 个，成型面 ${scope.geometry_refs.length} 个；${mechanisms[scope.mechanism_kind]}，${direction}。结构在结构组独立审核；本方案整体确认后解锁相关方向计算。`
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

    const magnitude = Math.hypot(...values)

    return Math.abs(magnitude - 1) <= 1e-12 ? values : values.map(value => value / magnitude)
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
    ...context.forming_scopes.filter(item => selected.has(item.forming_scope_id)).flatMap(scopeDisplayGeometry)
  ]

  return Array.from(
    new Map(
      refs.map(ref => [JSON.stringify([ref.input_sha256, ref.topology_snapshot_id, ref.kind, ref.index]), ref])
    ).values()
  )
}

export function formingPlanReady(scope: ReviewScope | undefined): boolean {
  return Boolean(scope && scope.status !== 'stale' && formingPlanMissing(scope).length === 0)
}

/** Report actual contract requirements; an empty undercut set is not a new confirmation gate. */
export function formingPlanMissing(scope: ReviewScope): string[] {
  const missing: string[] = []

  if (!scope.geometry_refs.length) {missing.push('成型面')}
  if (scope.mode === 'main' ? scope.mechanism_kind !== 'none' : !['slider', 'lifter'].includes(scope.mechanism_kind)) {
    missing.push('成型机构')
  }
  if (!parseDirectionVector(JSON.stringify(scope.draw_direction))) {missing.push('退出方向')}

  return missing
}

export function formingDirectionLabel(scope: ReviewScope): string {
  const direction = parseDirectionVector(JSON.stringify(scope.draw_direction))
  if (!direction) {return '方向待补充'}
  const axis = direction.findIndex(value => Math.abs(value) > 1 - 1e-8)

  return axis >= 0 ? `${direction[axis] > 0 ? '+' : '−'}${'XYZ'[axis]}`
    : `[${direction.map(value => Number(value.toFixed(3))).join(', ')}]`
}

export function scopeDisplayGeometry(scope: ReviewScope): GeometryReference[] {
  return Array.from(new Map(
    [...scope.geometry_refs, ...scope.undercut_geometry_refs].map(ref => [
      JSON.stringify([ref.input_sha256, ref.topology_snapshot_id, ref.kind, ref.entity_id, ref.index]), ref
    ])
  ).values())
}

export function directionUnlocked(context: ReviewContext, group: ReviewGroup): boolean {
  return formingPlanReady(context.forming_scopes.find(scope => scope.forming_scope_id === group.target_refs[0]))
}

export function parseReviewDirection(values: string[]): number[] | null {
  if (values.length !== 3 || values.some(value => !value.trim())) {
    return null
  }

  const direction = values.map(Number)

  return parseDirectionVector(JSON.stringify(direction))
}
