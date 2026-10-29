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
  snapshot_id?: string
  features: ReviewFeature[]
  forming_scopes: ReviewScope[]
  review_groups: ReviewGroup[]
  annotations: ReviewAnnotation[]
  annotation_kinds?: AnnotationKind[]
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
  product_forming: '第一轮 · 主成型范围',
  undercut_scope: '第一轮 · 倒扣范围',
  local_structure: '第二轮 · 局部结构',
  local_direction: '第二轮 · 局部退出方向'
}

export function reviewBuckets(context: ReviewContext): Array<{ id: string; label: string; groups: ReviewGroup[] }> {
  const buckets = new Map<string, { id: string; label: string; groups: ReviewGroup[] }>()
  for (const group of context.review_groups.filter(item => item.status !== 'stale')) {
    const kind =
      group.kind === 'ordinary_structure'
        ? (context.features.find(feature => group.target_refs.includes(feature.feature_id))?.kind ?? '结构')
        : group.kind === 'surface_attribute'
          ? (context.annotations.find(annotation => group.target_refs.includes(annotation.annotation_id))?.kind ??
            'surface_attribute')
          : group.kind
    const bucket = buckets.get(kind) ?? { id: kind, label: LABELS[kind] ?? kind, groups: [] }
    bucket.groups.push(group)
    buckets.set(kind, bucket)
  }
  return Array.from(buckets.values())
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
  if (values.length !== 3 || values.some(value => !value.trim())) return null
  const direction = values.map(Number)
  return direction.every(Number.isFinite) && direction.some(value => value !== 0) ? direction : null
}
