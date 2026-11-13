import type { GeometryReference } from './dfm-viewer-geometry'
import { formatViewerValue } from './dfm-viewer-issues'

export interface FeaturePresentationInput {
  feature_id: string
  kind: string
  confidence?: unknown
  quality?: unknown
  geometry_refs?: readonly GeometryReference[]
  properties?: Record<string, unknown>
  parameters?: Record<string, unknown>
  subtype?: string
}

export interface FeatureConfidence {
  label: string
  value: number | null
}

export const FEATURE_LABELS: Record<string, string> = {
  main_wall: '主体壁',
  hollow_boss: '带孔 BOSS 柱',
  screw_boss: '带孔 BOSS 柱', // Historical snapshots retain their original IDs.
  solid_boss: '实心 BOSS 柱',
  blend: '圆角链',
  canonical_surface: '规范曲面',
  cavity: '几何凹腔',
  chamfer: '倒角',
  convex_hull: '凸包面集',
  drilled_hole: '钻孔',
  isolated: '孤立特征',
  rib: '加强筋',
  shaft: '轴类特征',
  surface_probe: '曲面探针'
}

export const FEATURE_SUBTYPE_LABELS: Record<string, string> = {
  hollow_boss: '空心柱',
  solid_boss: '实心柱',
  arbitrary_cavity: '任意型腔',
  blind_hole: '盲孔',
  cliff_blend_chain: '悬崖圆角链',
  coaxial_shaft: '同轴轴段',
  conical: '圆锥倒角',
  conical_hole: '锥孔',
  countersunk_hole: '沉头孔',
  edge_blend_chain: '边圆角链',
  face_probe_set: '面探针集',
  hull_face_set: '凸包面集',
  inner_contour_feature: '内轮廓孤立特征',
  planar_band: '平面倒角',
  straight_rib: '直加强筋',
  stepped_countersunk_hole: '阶梯沉头孔',
  stepped_hole: '阶梯孔',
  through_hole: '通孔',
  uncertain_blend_chain: '待确认圆角链',
  vertex_blend_chain: '顶点圆角链'
}

const PARAMETER_LABELS: Record<string, string> = {
  depth_mm: '深度',
  diameter_mm: '直径',
  face_count: '面数',
  height_mm: '高度',
  length_mm: '长度',
  radius_mm: '半径',
  semi_angle_deg: '半角',
  thickness_mm: '厚度',
  width_mm: '宽度'
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {}
}

export function formatFeatureConfidence(confidence: unknown, quality?: unknown): FeatureConfidence {
  if (record(quality).confidence_available === false || confidence === null || confidence === undefined) {
    return { label: '未提供', value: null }
  }
  if (typeof confidence !== 'number' || !Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
    return { label: '不可用', value: null }
  }
  return { label: `${Math.round(confidence * 1000) / 10}%`, value: confidence }
}

export function formatFeatureParameters(parameters: Record<string, unknown>): string {
  return Object.entries(parameters)
    .filter(([key, value]) => key in PARAMETER_LABELS && (typeof value === 'number' || typeof value === 'string'))
    .map(([key, value]) => {
      const formatted = formatViewerValue(value)
      return `${PARAMETER_LABELS[key]} ${formatted}${key.endsWith('_mm') ? ' mm' : key.endsWith('_deg') ? '°' : ''}`
    }).join(' · ') || '无尺寸参数'
}

export function featurePresentation(feature: FeaturePresentationInput) {
  const properties = record(feature.properties)
  const suppliedParameters = record(feature.parameters)
  const parameters = {
    ...properties, ...record(properties.parameters),
    ...suppliedParameters, ...record(suppliedParameters.parameters)
  }
  const quality = { ...record(properties.quality), ...record(suppliedParameters.quality), ...record(feature.quality) }
  const refs = feature.geometry_refs ?? properties.geometry_refs
  const faces = (Array.isArray(refs) ? refs : []).filter((ref): ref is GeometryReference =>
    record(ref).kind === 'face' && Number.isSafeInteger(record(ref).index) && Number(record(ref).index) > 0)
  const faceRefs = [...new Map(faces.map(ref => [
    `${ref.input_sha256 ?? ''}:${ref.topology_snapshot_id ?? ''}:${ref.index}`, ref
  ])).values()].sort((left, right) => left.index - right.index)
  const subtype = feature.subtype || (typeof parameters.subtype === 'string' ? parameters.subtype : '')
  return {
    label: FEATURE_LABELS[feature.kind] ?? feature.kind,
    subtype: FEATURE_SUBTYPE_LABELS[subtype] ?? (subtype || '未提供'),
    parameters: formatFeatureParameters(parameters),
    faceRefs,
    confidence: formatFeatureConfidence(feature.confidence, quality)
  }
}
