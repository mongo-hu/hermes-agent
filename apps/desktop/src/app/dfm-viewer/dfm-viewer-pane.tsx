import { useStore } from '@nanostores/react'
import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { openDfmViewerWindow } from '@/lib/dfm-viewer-window'
import { $clarifyRequest, type ClarifyRequest, clearClarifyRequest, type DfmClarificationItem } from '@/store/clarify'
import type { DfmViewerTarget } from '@/store/dfm-viewer'
import { $gateway } from '@/store/gateway'

import { DfmAnnotationEditor } from './dfm-annotation-editor'
import type { CanvasMenuPoint } from './dfm-canvas-menus'
import type { DirectionAxisState } from './dfm-direction-axis'
import { DfmDirectionEditor, type DirectionDraft } from './dfm-direction-editor'
import { createFaceCandidateIndex, type FaceCandidate } from './dfm-face-navigation'
import { DfmFeatureEditor } from './dfm-feature-editor'
import { DfmFeatureGroups } from './dfm-feature-groups'
import { ModelCanvas } from './dfm-model-canvas'
import { DfmManualReviewNotice, type ManualReviewResult } from './dfm-manual-review-notice'
import { DfmObjectMenu, type ObjectMenuItem } from './dfm-object-menu'
import type { ReviewNavigation } from './dfm-review'
import { type AnnotationKind, confirmedFeatureIds, type ReviewAnnotation, type ReviewContext, type ReviewGroup, type ReviewScope } from './dfm-review'
import { parseDirectionVector, reviewBuckets, reviewEditTimeout, reviewGeometry, scopeDisplayGeometry } from './dfm-review'
import { captureReviewEdit, isReviewConflict, type ReviewEditBase, reviewEditUnchanged } from './dfm-review-edits'
import { DfmReviewPanel, type DfmReviewPanelActions, type DfmScopePreparation, removableObjectReviewGroup } from './dfm-review-panel'
import { resolveReviewDisplayRefs, type TopologyEdge } from './dfm-viewer-geometry'
import {
  type GeometryReference,
  type RenderPrimitive,
  type RenderTriangleReference,
  resolveGeometryRefFaceIndices,
  type TopologyFace
} from './dfm-viewer-geometry'
import {
  classifyViewerIssueType,
  formatViewerValue,
  summarizeViewerIssueTypes,
  type ViewerIssueTypeCount
} from './dfm-viewer-issues'
import { type DfmHighlightSelection, objectHighlightSelection, sameHighlightSelection } from './dfm-viewer-selection'

interface ViewerIssue {
  actual: unknown
  check_id?: string
  evaluation_id: string
  expected: unknown
  geometry_refs: GeometryReference[]
  triangle_refs?: RenderTriangleReference[]
  metric_id: string
  operator: string
  issue_type_id?: string
  issue_type_label?: string
  title: string
}

interface ViewerFeature {
  confidence: unknown
  diagnostics: Record<string, unknown>
  feature_id: string
  geometry_refs: GeometryReference[]
  kind: string
  method: string
  parameters: Record<string, unknown>
  subtype: string
}

interface ViewerManifest {
  contract_version: 'hermes.dfm.viewer/v2'
  project_id?: string
  snapshot_id?: string
  feature_count?: number
  features?: ViewerFeature[]
  manual_review_count?: number
  manual_review_results?: ManualReviewResult[]
  issue_count: number
  issue_type_counts?: ViewerIssueTypeCount[]
  issues: ViewerIssue[]
  scene_path: string
  scope_id: string
  project_revision?: number
  review_status?: 'confirmed' | 'pending'
  review_groups?: ReviewGroup[]
  status?: 'completed' | 'partial' | 'discovery' | 'preview'
  topology_path: string
  verification_level: string
}

interface RenderSceneDocument {
  primitives: RenderPrimitive[]
  render_mesh_snapshot: { render_mesh_snapshot_id?: string; triangle_count: number }
  schema_version: 2
}

interface TopologyDocument {
  faces: TopologyFace[]
  edges?: TopologyEdge[]
  schema_version: 2
}


function dirname(filePath: string): string {
  const index = Math.max(filePath.lastIndexOf('/'), filePath.lastIndexOf('\\'))

  return index < 0 ? '' : filePath.slice(0, index)
}

function joinPath(base: string, child: string): string {
  const separator = base.includes('\\') ? '\\' : '/'

  return `${base.replace(/[\\/]$/, '')}${separator}${child}`
}

async function readJson<T>(filePath: string): Promise<T> {
  const readJsonFile = window.hermesDesktop.readJsonFile

  if (readJsonFile) {
    return (await readJsonFile(filePath)) as T
  }

  const result = await window.hermesDesktop.readFileText(filePath)

  return JSON.parse(result.text) as T
}


const FEATURE_LABELS: Record<string, string> = {
  main_wall: '主体壁',
  hollow_boss: '带孔 BOSS 柱',
  screw_boss: '带孔 BOSS 柱', // Display historical snapshots without changing their IDs.
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

const FEATURE_SUBTYPE_LABELS: Record<string, string> = {
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

function formatFeatureParameters(parameters: Record<string, unknown>): string {
  const preferred = Object.entries(parameters)
    .filter(([key, value]) => key in PARAMETER_LABELS && (typeof value === 'number' || typeof value === 'string'))
    .slice(0, 3)

  return (
    preferred
      .map(
        ([key, value]) =>
          PARAMETER_LABELS[key] +
          ' ' +
          formatViewerValue(value) +
          (key.endsWith('_mm') ? ' mm' : key.endsWith('_deg') ? '°' : '')
      )
      .join(' · ') || '无尺寸参数'
  )
}

export function DfmClarificationPanel({
  features,
  onFeaturesHighlight,
  priorityFeatureId,
  request
}: {
  features: ViewerFeature[]
  onFeaturesHighlight: (featureRefs: string[]) => void
  priorityFeatureId: string | null
  request: ClarifyRequest
}) {
  const gateway = useStore($gateway)
  const context = request.context!
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [inheritedFeatureIds, setInheritedFeatureIds] = useState<Set<string>>(new Set())
  const [openFeatureKinds, setOpenFeatureKinds] = useState<Set<string>>(new Set())
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const questionsRef = useRef<HTMLDivElement>(null)

  const featureById = useMemo(
    () => new Map(features.map((feature, index) => [feature.feature_id, { feature, index }] as const)),
    [features]
  )

  const wholeItems = useMemo(
    () =>
      context.items.filter(
        item => !item.featureId && item.impactScope !== '特征实例级' && !item.impactScope.startsWith('非前置')
      ),
    [context.items]
  )

  const inferredItems = useMemo(
    () => context.items.filter(item => item.impactScope.startsWith('非前置')),
    [context.items]
  )

  const featureTypeGroups = useMemo(() => {
    const byKind = new Map<
      string,
      { firstIndex: number; instances: Map<string, { firstIndex: number; items: DfmClarificationItem[] }> }
    >()

    for (const item of context.items) {
      if (item.impactScope !== '特征实例级' || !item.featureId) {
        continue
      }

      const featureEntry = featureById.get(item.featureId)
      const kind = item.featureKind || featureEntry?.feature.kind || 'unknown'
      const featureIndex = featureEntry?.index ?? Number.MAX_SAFE_INTEGER
      const typeGroup = byKind.get(kind) ?? { firstIndex: featureIndex, instances: new Map() }

      const instance = typeGroup.instances.get(item.featureId) ?? {
        firstIndex: featureIndex,
        items: []
      }

      instance.items.push(item)
      typeGroup.instances.set(item.featureId, instance)
      typeGroup.firstIndex = Math.min(typeGroup.firstIndex, featureIndex)
      byKind.set(kind, typeGroup)
    }

    const priorityKind = priorityFeatureId ? featureById.get(priorityFeatureId)?.feature.kind : undefined

    return [...byKind.entries()]
      .map(([kind, group]) => ({
        firstIndex: group.firstIndex,
        kind,
        instances: [...group.instances.entries()]
          .map(([featureId, instance]) => ({ featureId, ...instance }))
          .sort((left, right) => left.firstIndex - right.firstIndex)
      }))
      .sort((left, right) => {
        if (left.kind === priorityKind && right.kind !== priorityKind) {
          return -1
        }

        if (right.kind === priorityKind && left.kind !== priorityKind) {
          return 1
        }

        return left.firstIndex - right.firstIndex
      })
  }, [context.items, featureById, priorityFeatureId])

  useEffect(() => {
    setAnswers({})
    setInheritedFeatureIds(new Set())
    setError('')
    setSubmitting(false)
  }, [request.requestId])

  useEffect(() => {
    setOpenFeatureKinds(new Set(featureTypeGroups.map(typeGroup => typeGroup.kind)))
  }, [featureTypeGroups, priorityFeatureId, request.requestId])

  useEffect(() => {
    if (priorityFeatureId) {
      questionsRef.current?.scrollTo({ behavior: 'smooth', top: 0 })
    }
  }, [priorityFeatureId])

  const answeredCount = context.items.filter(item => Boolean(answers[item.clarificationId]?.trim())).length
  const allAnswered = answeredCount === context.items.length

  const pendingDefaults = context.items.filter(
    item => item.hasDefault && item.defaultValue !== null && !answers[item.clarificationId]?.trim()
  ).length

  const fillAllDefaults = useCallback(() => {
    setAnswers(current => {
      const next = { ...current }

      for (const item of context.items) {
        if (item.hasDefault && item.defaultValue !== null && !next[item.clarificationId]?.trim()) {
          next[item.clarificationId] = item.defaultValue
        }
      }

      return next
    })
  }, [context.items])

  const inheritFeatureType = useCallback(
    (instances: Array<{ featureId: string; items: DfmClarificationItem[] }>) => {
      const source = instances[0]

      if (!source) {
        return
      }

      const sourceValues = new Map(
        source.items.flatMap(item => {
          const value = answers[item.clarificationId]?.trim()

          return value ? [[`${item.runtimeKey}@${item.checkId ?? ''}`, value] as const] : []
        })
      )

      const inherited = new Set(
        instances
          .slice(1)
          .flatMap(instance =>
            instance.items.some(item => sourceValues.has(`${item.runtimeKey}@${item.checkId ?? ''}`))
              ? [instance.featureId]
              : []
          )
      )

      setAnswers(current => {
        const next = { ...current }

        for (const instance of instances.slice(1)) {
          for (const item of instance.items) {
            const value = sourceValues.get(`${item.runtimeKey}@${item.checkId ?? ''}`)

            if (value !== undefined) {
              next[item.clarificationId] = value
            }
          }
        }

        return next
      })
      setInheritedFeatureIds(current => new Set([...current, ...inherited]))
    },
    [answers]
  )

  const submit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault()

      if (!gateway || !allAnswered) {
        return
      }

      setSubmitting(true)
      setError('')

      try {
        await gateway.request<{ ok?: boolean }>('clarify.respond', {
          answer: JSON.stringify({
            answers: context.items.map(item => ({
              fact_name: item.factName,
              feature_id: item.featureId,
              value: answers[item.clarificationId].trim()
            }))
          }),
          request_id: request.requestId
        })
        clearClarifyRequest(request.requestId, request.sessionId)
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause))
        setSubmitting(false)
      }
    },
    [allAnswered, answers, context.items, gateway, request.requestId, request.sessionId]
  )

  const renderQuestion = (item: DfmClarificationItem) => {
    const answered = Boolean(answers[item.clarificationId]?.trim())
    const question = item.question.replace(/^(?:【[^】]+】)+/, '')

    return (
      <label
        className={`grid gap-1.5 rounded-md border px-2 py-2 transition ${
          answered
            ? 'border-cyan-400/25 bg-cyan-500/[0.055]'
            : 'border-white/8 bg-black/10 focus-within:border-cyan-400/50'
        }`}
        key={item.clarificationId}
      >
        <span className="flex items-start justify-between gap-2 text-[10px] leading-4 text-slate-200">
          <span>{question}</span>
          {item.hasDefault && item.defaultValue !== null && (
            <span className="shrink-0 text-[9px] text-slate-500">默认：{item.defaultValue}</span>
          )}
        </span>
        {item.choices.length > 0 ? (
          <select
            className="min-w-0 rounded-md border border-white/10 bg-[#10151d] px-2 py-1.5 text-[11px] text-slate-100 outline-none focus:border-cyan-400/60"
            disabled={submitting}
            onChange={event => setAnswers(current => ({ ...current, [item.clarificationId]: event.target.value }))}
            value={answers[item.clarificationId] ?? ''}
          >
            <option value="">请选择</option>
            {item.choices.map(choice => (
              <option key={choice} value={choice}>
                {choice}
              </option>
            ))}
          </select>
        ) : (
          <input
            className="min-w-0 rounded-md border border-white/10 bg-[#10151d] px-2 py-1.5 text-[11px] text-slate-100 outline-none placeholder:text-slate-600 focus:border-cyan-400/60"
            disabled={submitting}
            onChange={event => setAnswers(current => ({ ...current, [item.clarificationId]: event.target.value }))}
            placeholder="请输入"
            value={answers[item.clarificationId] ?? ''}
          />
        )}
      </label>
    )
  }

  return (
    <form className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)_auto] gap-2" onSubmit={submit}>
      <div className="rounded-lg border border-cyan-400/15 bg-cyan-500/[0.06] px-2.5 py-2">
        <div className="flex items-center justify-between gap-3 text-[10px]">
          <div className="grid gap-0.5">
            <span className="text-cyan-100">请补充分析条件</span>
            <span className="text-slate-500">默认值只填充空项</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-slate-400">
              {answeredCount}/{context.items.length}
            </span>
            <button
              className="rounded-md border border-cyan-300/20 bg-cyan-500/10 px-2 py-1 text-[10px] text-cyan-100 hover:bg-cyan-500/20 disabled:cursor-not-allowed disabled:opacity-40"
              disabled={pendingDefaults === 0 || submitting}
              onClick={fillAllDefaults}
              type="button"
            >
              一键填充默认值{pendingDefaults > 0 ? `（${pendingDefaults}）` : ''}
            </button>
          </div>
        </div>
        <div className="mt-2 h-1 overflow-hidden rounded-full bg-white/8">
          <div
            className="h-full rounded-full bg-cyan-400 transition-[width]"
            style={{ width: `${(answeredCount / context.items.length) * 100}%` }}
          />
        </div>
        <p className="mt-1.5 text-[10px] leading-4 text-slate-400">整件 → 特征类型 → 特征实例</p>
      </div>

      <div className="min-h-0 overflow-y-auto pr-1" ref={questionsRef}>
        <div className="overflow-hidden rounded-lg border border-cyan-300/20 bg-cyan-500/[0.025]">
          <div className="border-b border-white/10 bg-cyan-500/[0.07] px-2.5 py-2">
            <div className="text-[11px] font-medium text-cyan-50">整件：当前零件</div>
            <div className="mt-0.5 text-[9px] text-slate-400">所有补充信息的最外层作用域</div>
          </div>

          <div className="grid gap-2 p-2">
            {wholeItems.length > 0 && (
              <section className="overflow-hidden rounded-lg border border-white/10 bg-white/[0.025]">
                <div className="border-b border-white/10 px-2.5 py-2">
                  <div className="text-[11px] font-medium text-slate-100">整件级</div>
                  <div className="mt-0.5 text-[9px] text-slate-500">答案对当前零件整体生效</div>
                </div>
                <div className="grid gap-1.5 p-2">{wholeItems.map(renderQuestion)}</div>
              </section>
            )}

            {featureTypeGroups.length > 0 && (
              <section className="overflow-hidden rounded-lg border border-amber-300/15 bg-amber-500/[0.025]">
                <div className="border-b border-white/10 px-2.5 py-2">
                  <div className="text-[11px] font-medium text-amber-100">特征实例级</div>
                  <div className="mt-0.5 text-[9px] text-slate-500">先按特征类型，再按模型中的实例顺序</div>
                </div>

                <div className="grid gap-2 p-2">
                  {featureTypeGroups.map(typeGroup => {
                    const source = typeGroup.instances[0]

                    const sourceReady = Boolean(
                      source && source.items.every(item => answers[item.clarificationId]?.trim())
                    )

                    const inheritableCount = Math.max(0, typeGroup.instances.length - 1)

                    return (
                      <details
                        className="overflow-hidden rounded-lg border border-white/10 bg-[#171f2b]"
                        key={typeGroup.kind}
                        onToggle={event => {
                          const isOpen = event.currentTarget.open

                          setOpenFeatureKinds(current => {
                            if (current.has(typeGroup.kind) === isOpen) {
                              return current
                            }

                            const next = new Set(current)

                            if (isOpen) {
                              next.add(typeGroup.kind)
                            } else {
                              next.delete(typeGroup.kind)
                            }

                            return next
                          })
                        }}
                        open={openFeatureKinds.has(typeGroup.kind)}
                      >
                        <summary className="flex items-center justify-between gap-2 px-2.5 py-2 text-[11px] text-slate-100">
                          <span>
                            {FEATURE_LABELS[typeGroup.kind] ?? typeGroup.kind} · {typeGroup.instances.length} 个实例
                          </span>
                          <span className="text-[9px] text-slate-500">
                            {
                              typeGroup.instances.filter(instance =>
                                instance.items.every(item => answers[item.clarificationId]?.trim())
                              ).length
                            }
                            /{typeGroup.instances.length}
                          </span>
                        </summary>

                        {inheritableCount > 0 && (
                          <div className="flex items-center justify-between gap-2 border-t border-white/8 px-2 py-1.5">
                            <span className="text-[9px] leading-4 text-slate-500">
                              先填写实例 1，再应用到同类型后续实例
                            </span>
                            <button
                              className="shrink-0 rounded-md border border-cyan-300/20 bg-cyan-500/10 px-2 py-1 text-[9px] text-cyan-100 hover:bg-cyan-500/20 disabled:cursor-not-allowed disabled:opacity-40"
                              disabled={!sourceReady || submitting}
                              onClick={() => inheritFeatureType(typeGroup.instances)}
                              type="button"
                            >
                              将实例 1 应用到后续 {inheritableCount} 个
                            </button>
                          </div>
                        )}

                        <div className="grid gap-2 border-t border-white/8 p-2">
                          {typeGroup.instances.map((instance, instanceIndex) => {
                            const featureEntry = featureById.get(instance.featureId)
                            const instanceAnswered = instance.items.every(item => answers[item.clarificationId]?.trim())

                            return (
                              <article
                                className={`grid gap-1.5 rounded-lg border p-2 ${
                                  instanceAnswered
                                    ? 'border-cyan-400/25 bg-cyan-500/[0.045]'
                                    : 'border-white/8 bg-black/10'
                                }`}
                                key={instance.featureId}
                                onFocus={() => onFeaturesHighlight([instance.featureId])}
                                onMouseEnter={() => onFeaturesHighlight([instance.featureId])}
                              >
                                <div className="flex items-center justify-between gap-2">
                                  <div>
                                    <div className="text-[10px] font-medium text-slate-100">
                                      {FEATURE_LABELS[typeGroup.kind] ?? typeGroup.kind} {instanceIndex + 1}
                                    </div>
                                    <div className="mt-0.5 text-[9px] text-slate-500">
                                      {featureEntry ? `模型特征 ${featureEntry.index + 1}` : instance.featureId}
                                    </div>
                                  </div>
                                  <span
                                    className={`text-[9px] ${instanceAnswered ? 'text-cyan-300' : 'text-slate-500'}`}
                                  >
                                    {inheritedFeatureIds.has(instance.featureId)
                                      ? '已继承，可修改'
                                      : instanceAnswered
                                        ? '已填写'
                                        : '待填写'}
                                  </span>
                                </div>
                                <div className="grid gap-1.5">{instance.items.map(renderQuestion)}</div>
                              </article>
                            )
                          })}
                        </div>
                      </details>
                    )
                  })}
                </div>
              </section>
            )}

            {inferredItems.length > 0 && (
              <section className="overflow-hidden rounded-lg border border-violet-300/15 bg-violet-500/[0.025]">
                <div className="border-b border-white/10 px-2.5 py-2">
                  <div className="text-[11px] font-medium text-violet-100">系统识别待确认</div>
                  <div className="mt-0.5 text-[9px] text-slate-500">仅显示需要人工确认的非前置项</div>
                </div>
                <div className="grid gap-1.5 p-2">{inferredItems.map(renderQuestion)}</div>
              </section>
            )}
          </div>
        </div>
      </div>

      <div className="border-t border-white/10 pt-2">
        {error && <p className="mb-2 text-[10px] text-red-200">{error}</p>}
        <button
          className="w-full rounded-md bg-cyan-500/20 px-3 py-2 text-[11px] font-medium text-cyan-50 hover:bg-cyan-500/30 disabled:cursor-not-allowed disabled:opacity-40"
          disabled={!allAnswered || submitting}
          type="submit"
        >
          {submitting ? '提交中…' : `提交全部并继续分析（${answeredCount}/${context.items.length}）`}
        </button>
      </div>
    </form>
  )
}

export function DfmViewerPane({ embedded = false, target }: { embedded?: boolean; target: DfmViewerTarget }) {
  const reviewActions = useRef<DfmReviewPanelActions>(null)
  const [reviewBusy, setReviewBusy] = useState(false)
  const [objectMenu, setObjectMenu] = useState<{ point: CanvasMenuPoint; candidates: FaceCandidate[]; target: FaceCandidate | null; faceIndex: number } | null>(null)
  const dismissObjectMenu = useCallback(() => setObjectMenu(null), [])
  const activeFeatureElementRef = useRef<HTMLDivElement | null>(null)
  const clarifyRequest = useStore($clarifyRequest)
  const [manifest, setManifest] = useState<ViewerManifest | null>(null)
  const projectId = target.projectId ?? manifest?.project_id
  const [scene, setScene] = useState<RenderSceneDocument | null>(null)
  const [topologyFaces, setTopologyFaces] = useState<TopologyFace[]>([])
  const [topologyEdges, setTopologyEdges] = useState<TopologyEdge[]>([])
  const [activeIssueId, setActiveIssueId] = useState<string | null>(null)
  const [activeFeatureId, setActiveFeatureId] = useState<string | null>(null)
  const [reviewNavigation, setReviewNavigation] = useState<ReviewNavigation | null>(null)
  const [highlightedFeatureIds, setHighlightedFeatureIds] = useState<string[]>([])
  const [highlightSelection, setHighlightSelection] = useState<DfmHighlightSelection | null>(null)
  const [reviewGeometryRefs, setReviewGeometryRefs] = useState<GeometryReference[]>([])
  const currentProjectId = useRef(projectId)
  currentProjectId.current = projectId
  const [clarificationPriorityFeatureId, setClarificationPriorityFeatureId] = useState<string | null>(null)
  const [pickedFaceIndex, setPickedFaceIndex] = useState<number | null>(null)
  const [editingFeatureId, setEditingFeatureId] = useState<string | null>(null)
  const [editingFeatureBaseline, setEditingFeatureBaseline] = useState<ViewerFeature | null>(null)
  const [editingScopeId, setEditingScopeId] = useState<string | null>(null)
  const [editingScopeRole, setEditingScopeRole] = useState<'forming' | 'undercut'>('forming')
  const [editingAnnotation, setEditingAnnotation] = useState<ReviewAnnotation | null>(null)
  const [editingSnapshotId, setEditingSnapshotId] = useState<string | undefined>()
  const [annotationValues, setAnnotationValues] = useState<string[]>([])
  const [draftFaceIndices, setDraftFaceIndices] = useState<Set<number> | null>(null)
  const [projectRevision, setProjectRevision] = useState<number | null>(null)
  const [latestReviewContext, setLatestReviewContext] = useState<ReviewContext | null>(null)
  const currentReviewContext = useRef<ReviewContext | null>(null)
  const [savingFeature, setSavingFeature] = useState(false)
  const [activeDirectionId, setActiveDirectionId] = useState<string | null>(null)
  const [axisOrigins, setAxisOrigins] = useState<Record<string, [number, number, number]>>({})
  const [directionDraft, setDirectionDraft] = useState<DirectionDraft | null>(null)
  const [directionError, setDirectionError] = useState('')
  const [preparingReview, setPreparingReview] = useState(false)
  const [scopePreparation, setScopePreparation] = useState<DfmScopePreparation | null>(null)
  const [editConflict, setEditConflict] = useState(false)
  const editBase = useRef<ReviewEditBase | null>(null)
  const [reviewError, setReviewError] = useState('')
  const [reviewNotice, setReviewNotice] = useState('')
  const [error, setError] = useState('')
  const [reloadRequest, setReloadRequest] = useState(0)
  const [fitRequest, setFitRequest] = useState(0)
  const [focusRequest, setFocusRequest] = useState({ sequence: 0, faceIndices: [] as number[] })
  const editingGeometry = Boolean(editingFeatureId || editingScopeId || editingAnnotation || directionDraft || preparingReview)
  const loadedTargetSignature = useRef('')
  const workingViewer = useRef<{ externalPath: string; path: string; projectId: string } | null>(null)
  const [reviewPaneTab, setReviewPaneTab] = useState<'features' | 'issues' | 'clarifications'>('features')

  const clearHighlight = useCallback(() => {
    setHighlightSelection(null)
    setHighlightedFeatureIds([])
    setActiveFeatureId(null)
    setActiveIssueId(null)
    setPickedFaceIndex(null)
    setReviewGeometryRefs([])
    setReviewNavigation(null)
    setActiveDirectionId(null)
    setClarificationPriorityFeatureId(null)
  }, [])

  const clearDisplaySelection = useCallback(() => {
    if (!editingGeometry && !savingFeature) {clearHighlight()}
  }, [editingGeometry, savingFeature, clearHighlight])

  useEffect(() => {
    setEditingFeatureId(null)
    setEditingFeatureBaseline(null)
    setEditingScopeId(null)
    setEditingAnnotation(null)
    setDirectionDraft(null)
    setPreparingReview(false)
    setScopePreparation(null)
    setEditConflict(false)
    editBase.current = null
    setActiveDirectionId(null)
    setAxisOrigins({})
    setDirectionError('')
    setSavingFeature(false)
    setDraftFaceIndices(null)
    setReviewGeometryRefs([])
  }, [projectId, target.manifestPath])

  useEffect(() => {
    if (editingGeometry) {return}

    if (workingViewer.current && (workingViewer.current.externalPath !== target.manifestPath ||
      (target.projectId && workingViewer.current.projectId !== target.projectId))) {workingViewer.current = null}

    const manifestPath = workingViewer.current?.path ?? target.manifestPath
    const signature = JSON.stringify([reloadRequest, manifestPath, target.projectId, target.revision, target.status])

    if (loadedTargetSignature.current === signature) {return}
    let cancelled = false

    async function load() {
      try {
        setError('')
        setManifest(null)
        setScene(null)
        setTopologyFaces([])
        setTopologyEdges([])
        setPickedFaceIndex(null)
        setActiveDirectionId(null)
        setLatestReviewContext(null)
        currentReviewContext.current = null
        setFocusRequest({ sequence: 0, faceIndices: [] })
        setActiveIssueId(null)
        setActiveFeatureId(null)
        setReviewNavigation(null)
        setHighlightedFeatureIds([])
        setHighlightSelection(null)
        setReviewGeometryRefs([])
        setClarificationPriorityFeatureId(null)
        setEditingFeatureId(null)
        setEditingFeatureBaseline(null)
        setEditingScopeId(null)
        setEditingAnnotation(null)
        setDraftFaceIndices(null)
        setReviewError('')

        if (!workingViewer.current) {setReviewNotice('')}
        const nextManifest = await readJson<ViewerManifest>(manifestPath)

        if (nextManifest.contract_version !== 'hermes.dfm.viewer/v2') {
          throw new Error('不支持的 DFM 查看器协议')
        }

        if (
          nextManifest.project_id &&
          (!/^dfm_[a-f0-9]{12,32}$/.test(nextManifest.project_id) ||
            (target.projectId && target.projectId !== nextManifest.project_id))
        ) {
          throw new Error('DFM 项目身份不一致，请重新打开对应项目。')
        }

        const artifactDir = dirname(manifestPath)

        const [nextScene, topology] = await Promise.all([
          readJson<RenderSceneDocument>(joinPath(artifactDir, nextManifest.scene_path)),
          readJson<TopologyDocument>(joinPath(artifactDir, nextManifest.topology_path))
        ])

        if (nextScene.schema_version !== 2 || topology.schema_version !== 2) {
          throw new Error('不支持的共享几何工件协议')
        }

        if (cancelled) {
          return
        }

        setManifest(nextManifest)
        loadedTargetSignature.current = signature
        setProjectRevision(nextManifest.project_revision ?? null)
        setScene(nextScene)
        setTopologyFaces(topology.faces)
        setTopologyEdges(topology.edges ?? [])
        const initialIssueId = nextManifest.issues[0]?.evaluation_id ?? null
        const initialFeatureId = initialIssueId ? null : (nextManifest.features?.[0]?.feature_id ?? null)
        setActiveIssueId(initialIssueId)
        setActiveFeatureId(initialFeatureId)
        setHighlightedFeatureIds(initialFeatureId ? [initialFeatureId] : [])
      } catch (cause) {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : String(cause))
        }
      }
    }

    void load()

    return () => {
      cancelled = true
    }
  }, [reloadRequest, target.manifestPath, target.projectId, target.revision, target.status, editingGeometry])

  const activeIssue = manifest?.issues.find(issue => issue.evaluation_id === activeIssueId) ?? null

  const activeFeatures = useMemo(() => {
    const highlighted = new Set(highlightedFeatureIds)

    return (manifest?.features ?? []).filter(feature => highlighted.has(feature.feature_id))
  }, [highlightedFeatureIds, manifest])

  const clarificationRequest =
    clarifyRequest?.context?.mode === 'dfm_analysis_batch' && clarifyRequest.context.projectId === projectId
      ? clarifyRequest
      : null

  const clarificationRequestId = clarificationRequest?.requestId
  const analysisCompleted = manifest?.status === 'completed'

  useEffect(() => {
    if (clarificationRequestId) {
      setReviewPaneTab('clarifications')
    }
  }, [clarificationRequestId])

  useEffect(() => {
    if (analysisCompleted && !clarificationRequestId) {
      setReviewPaneTab('issues')
    }
  }, [analysisCompleted, clarificationRequestId])

  const issueFacesById = useMemo(
    () =>
      new Map(
        (manifest?.issues ?? []).map(
          issue => [issue.evaluation_id, resolveGeometryRefFaceIndices(issue.geometry_refs)] as const
        )
      ),
    [manifest]
  )

  const featureFacesById = useMemo(
    () =>
      new Map(
        (manifest?.features ?? []).map(
          feature => [feature.feature_id, resolveGeometryRefFaceIndices(feature.geometry_refs)] as const
        )
      ),
    [manifest]
  )

  const issueTypeCounts = useMemo(() => {
    if (manifest?.issue_type_counts?.length) {
      return manifest.issue_type_counts.map(item => {
        const [, label] = classifyViewerIssueType({
          issue_type_id: item.issue_type_id,
          issue_type_label: item.label
        })

        return { ...item, label }
      })
    }

    return summarizeViewerIssueTypes(manifest?.issues ?? [])
  }, [manifest])

  const faceCandidates = useMemo(() => createFaceCandidateIndex(latestReviewContext ?? {
    features: (manifest?.features ?? []).map(feature => ({
      feature_id: feature.feature_id, kind: feature.kind, confidence: 0,
      properties: { ...feature.parameters, geometry_refs: feature.geometry_refs }
    })), annotations: [], forming_scopes: []
  }, topologyFaces, topologyEdges), [latestReviewContext, manifest, topologyFaces, topologyEdges])

  const faceNavigationIndex = useMemo(() => new Map([...faceCandidates].map(([face, items]) => [face, items[0]])), [faceCandidates])

  const handleFacePick = useCallback(
    (faceIndex: number, explicitTarget?: FaceCandidate) => {
      if ((directionDraft && !editingScopeId) || preparingReview || savingFeature || reviewBusy) {return}
      setPickedFaceIndex(faceIndex)

      if (directionDraft && editingScopeId) {
        const face = topologyFaces.find(item => item.geometry_ref.index === faceIndex)?.geometry_ref

        if (!face?.input_sha256 || !face.topology_snapshot_id || !face.entity_id) {
          setDirectionError('该面缺少可验证的拓扑身份，请刷新后重试。')

          return
        }

        const key = editingScopeRole === 'undercut' ? 'undercut_geometry_refs' : 'geometry_refs'
        setDirectionDraft(current => {
          if (!current) {return current}
          const refs = current.scope[key]

          const next = refs.some(ref => ref.kind === 'face' && ref.index === faceIndex)
            ? refs.filter(ref => ref.kind !== 'face' || ref.index !== faceIndex) : [...refs, face]

          return { ...current, scope: { ...current.scope, [key]: next } }
        })
        setDirectionError('')

        return
      }

      if (editingFeatureId || editingAnnotation) {
        setDraftFaceIndices(current => {
          const next = new Set(current ?? [])

          if (next.has(faceIndex)) {
            next.delete(faceIndex)
          } else {
            next.add(faceIndex)
          }

          return next
        })

        return
      }

      const match = explicitTarget ?? faceNavigationIndex.get(faceIndex)

      const selection = match ? objectHighlightSelection(match.category, match.objectId, latestReviewContext,
        manifest?.features?.find(feature => feature.feature_id === match.objectId)?.kind) : null

      if (!explicitTarget && selection && sameHighlightSelection(highlightSelection, selection)) {
        clearHighlight()

        return
      }

      setHighlightSelection(selection)
      setActiveDirectionId(match?.category === 'forming' ? match.objectId : null)
      setReviewGeometryRefs([])
      setActiveIssueId(null)
      setActiveFeatureId(match?.category === 'structure' ? match.objectId : null)
      setHighlightedFeatureIds(match?.category === 'structure' ? [match.objectId] : [])
      setClarificationPriorityFeatureId(match?.category === 'structure' ? match.objectId : null)

      if (match) {
        setReviewPaneTab('features')
        setReviewNavigation(previous => ({ ...match, sequence: (previous?.sequence ?? 0) + 1 }))

        if (match.category !== 'structure' && latestReviewContext) {
          const refs = match.category === 'annotation'
            ? latestReviewContext.annotations.find(item => item.annotation_id === match.objectId)?.geometry_refs
            : latestReviewContext.forming_scopes.filter(item => item.forming_scope_id === match.objectId).flatMap(scopeDisplayGeometry)

          setReviewGeometryRefs(resolveReviewDisplayRefs(refs ?? [], topologyFaces, topologyEdges))
        }

        return
      }

      setReviewNavigation(null)
      const issue = manifest?.issues.find(item => issueFacesById.get(item.evaluation_id)?.has(faceIndex))

      if (issue) {
        setActiveIssueId(issue.evaluation_id)
        setReviewPaneTab('issues')
      }
    },
    [directionDraft, preparingReview, savingFeature, reviewBusy, editingFeatureId, editingScopeId, editingScopeRole, editingAnnotation, faceNavigationIndex, issueFacesById, manifest,
      latestReviewContext, topologyFaces, topologyEdges, highlightSelection, clearHighlight]
  )

  useEffect(() => { setObjectMenu(null) }, [projectId, target.manifestPath, latestReviewContext?.revision, latestReviewContext?.snapshot_id, editingGeometry, savingFeature, reviewBusy])

  const objectMenuItem = (candidate: FaceCandidate): ObjectMenuItem => {
    const context = latestReviewContext
    const feature = context?.features.find(item => item.feature_id === candidate.objectId)
    const annotation = context?.annotations.find(item => item.annotation_id === candidate.objectId)
    const scope = context?.forming_scopes.find(item => item.forming_scope_id === candidate.objectId)

    const label = candidate.category === 'structure' ? (FEATURE_LABELS[feature?.kind ?? ''] ?? feature?.kind ?? '结构特征')
      : candidate.category === 'annotation' ? (annotation?.kind ?? '属性标注')
        : scope?.mode === 'main' ? '主出模方向' : '倒扣范围'

    const pending = context?.review_groups.some(group => group.target_refs.includes(candidate.objectId) && group.status === 'pending_review')

    return { target: candidate, label: `${label} · ${candidate.objectId}`, status: pending ? '待确认' : '已确认',
      removable: Boolean(context && removableObjectReviewGroup(context, candidate)) }
  }

  const openObjectMenu = (faceIndex: number, point: CanvasMenuPoint) => {
    if (editingGeometry || savingFeature || reviewBusy || !latestReviewContext) {return}
    const candidates = faceCandidates.get(faceIndex) ?? []

    if (!candidates.length) {return}

    const selected = candidates.find(item => item.objectId === highlightSelection?.objectId && item.category === highlightSelection.category)
      ?? (candidates.length === 1 ? candidates[0] : null)

    if (selected) {handleFacePick(faceIndex, selected)}
    setObjectMenu({ point, candidates, target: selected, faceIndex })
  }

  const requestFocus = useCallback((refs: GeometryReference[]) => {
    setFocusRequest(current => ({ sequence: current.sequence + 1, faceIndices: [...resolveGeometryRefFaceIndices(refs)] }))
  }, [])

  const handleIssueSelect = useCallback(
    (issue: ViewerIssue) => {
      setHighlightSelection(null)
      setReviewNavigation(null)
      setActiveDirectionId(null)
      setReviewGeometryRefs([])
      setActiveIssueId(issue.evaluation_id)
      setActiveFeatureId(null)
      setHighlightedFeatureIds([])
      setClarificationPriorityFeatureId(null)
      const firstFace = Array.from(issueFacesById.get(issue.evaluation_id) ?? [])[0]
      setPickedFaceIndex(firstFace ?? null)
      requestFocus(issue.geometry_refs)
    },
    [issueFacesById, requestFocus]
  )

  const handleFeatureSelect = useCallback(
    (feature: ViewerFeature) => {
      setHighlightSelection(objectHighlightSelection('structure', feature.feature_id, latestReviewContext, feature.kind))
      setReviewNavigation(null)
      setActiveDirectionId(null)
      setReviewGeometryRefs([])
      setActiveFeatureId(feature.feature_id)
      setHighlightedFeatureIds([feature.feature_id])
      setClarificationPriorityFeatureId(feature.feature_id)
      setActiveIssueId(null)
      const firstFace = Array.from(featureFacesById.get(feature.feature_id) ?? [])[0]
      setPickedFaceIndex(firstFace ?? null)
      requestFocus(feature.geometry_refs)
    },
    [featureFacesById, requestFocus, latestReviewContext]
  )

  const toggleFeatureSelection = useCallback((feature: ViewerFeature) => {
    if (editingGeometry || savingFeature) {return}
    const selection = objectHighlightSelection('structure', feature.feature_id, latestReviewContext, feature.kind)

    if (sameHighlightSelection(highlightSelection, selection)) {clearHighlight()}
    else {handleFeatureSelect(feature)}
  }, [editingGeometry, savingFeature, latestReviewContext, highlightSelection, clearHighlight, handleFeatureSelect])

  const toggleFeatureKind = useCallback((kind: string, featureIds: string[]) => {
    if (editingGeometry || savingFeature) {return}
    const selection: DfmHighlightSelection = { bucketId: `confirmed:${kind}`, category: 'structure' }

    if (sameHighlightSelection(highlightSelection, selection)) {
      clearHighlight()

      return
    }

    clearHighlight()
    setHighlightSelection(selection)
    setHighlightedFeatureIds(featureIds)
    requestFocus((manifest?.features ?? []).filter(feature => featureIds.includes(feature.feature_id)).flatMap(feature => feature.geometry_refs))
  }, [editingGeometry, savingFeature, highlightSelection, clearHighlight, requestFocus, manifest])

  const handleFeaturesHighlight = useCallback(
    (featureRefs: string[]) => {
      clearHighlight()
      setReviewGeometryRefs([])
      setHighlightedFeatureIds([...new Set(featureRefs.filter(ref => featureFacesById.has(ref)))])
      setActiveIssueId(null)
    },
    [featureFacesById, clearHighlight]
  )

  const startFeatureEdit = useCallback(
    (feature: ViewerFeature, context?: ReviewContext) => {
      const source = context ?? latestReviewContext
      editBase.current = source ? captureReviewEdit(source, 'feature', feature.feature_id) : null
      setEditConflict(false)
      handleFeatureSelect(feature)
      setEditingFeatureId(feature.feature_id)
      setEditingFeatureBaseline(feature)
      setEditingSnapshotId(context?.snapshot_id ?? manifest?.snapshot_id)

      if (context) {setProjectRevision(context.revision)}
      setEditingScopeId(null)
      setEditingAnnotation(null)
      setDraftFaceIndices(resolveGeometryRefFaceIndices(feature.geometry_refs))
      setReviewError('')
    },
    [handleFeatureSelect, manifest?.snapshot_id, latestReviewContext]
  )

  const cancelFeatureEdit = useCallback(() => {
    editBase.current = null
    setEditConflict(false)
    setEditingFeatureId(null)
    setEditingFeatureBaseline(null)
    setEditingScopeId(null)
    setEditingAnnotation(null)
    setDraftFaceIndices(null)
    setReviewError('')
  }, [])

  const applyReviewContext = useCallback((context: ReviewContext) => {
    if (currentProjectId.current !== context.project_id) {return}
    const previous = currentReviewContext.current

    if (previous?.project_id === context.project_id && previous.revision > context.revision) {return}

    const next = previous?.project_id === context.project_id && previous.revision === context.revision && !context.viewer_manifest
      ? { ...context, viewer_manifest: previous.viewer_manifest } : context

    if (editBase.current && !reviewEditUnchanged(editBase.current, next)) {setEditConflict(true)}

    currentReviewContext.current = next
    setLatestReviewContext(next)
    setProjectRevision(previous => (previous != null && previous > context.revision ? previous : context.revision))

    if (context.viewer_manifest) {
      workingViewer.current = { externalPath: target.manifestPath, path: context.viewer_manifest, projectId: context.project_id }
      setReviewPaneTab('features')
    }

    // Only semantic state changed. Keep the already loaded scene and mesh.
    setManifest(current =>
      current && (current.project_revision ?? 0) > context.revision
        ? current
        : current
          ? {
              ...current,
              ...(context.viewer_manifest ? { status: 'discovery' as const, issues: [], issue_count: 0, issue_type_counts: [], manual_review_results: [], manual_review_count: 0 } : {}),
              project_revision: context.revision,
              snapshot_id: context.snapshot_id,
              review_groups: context.review_groups,
              review_status: context.review_groups.some(group => group.status === 'pending_review')
                ? 'pending'
                : 'confirmed',
              features: context.features.map(feature => ({
                feature_id: feature.feature_id,
                kind: feature.kind,
                confidence: feature.confidence,
                subtype: String(feature.properties.subtype ?? ''),
                geometry_refs: feature.properties.geometry_refs ?? [],
                parameters: feature.properties,
                method: String(feature.properties.method ?? ''),
                diagnostics: feature.properties.diagnostics ?? {}
              }))
            }
          : current
    )
  }, [target.manifestPath])

  const highlightReview = useCallback(
    (refs: GeometryReference[], selection?: DfmHighlightSelection, force = false) => {
      if (selection && (editingGeometry || savingFeature)) {return}

      if (selection && !force && sameHighlightSelection(highlightSelection, selection)) {
        clearHighlight()

        return
      }

      clearHighlight()
      setHighlightSelection(selection ?? null)
      setActiveDirectionId(selection?.category === 'forming' ? selection.objectId ?? null : null)
      setActiveIssueId(null)
      setActiveFeatureId(null)
      setHighlightedFeatureIds([])
      const displayRefs = resolveReviewDisplayRefs(refs, topologyFaces, topologyEdges)
      setReviewError(displayRefs.length ? '' : '该项没有可用于定位的面引用，请刷新或检查识别结果。')
      setReviewGeometryRefs(displayRefs)
      requestFocus(displayRefs)
    },
    [topologyFaces, topologyEdges, requestFocus, editingGeometry, savingFeature, highlightSelection, clearHighlight]
  )

  useEffect(() => {
    if (!manifest || editingGeometry || savingFeature) {return}

    if ((activeIssueId && !manifest.issues.some(issue => issue.evaluation_id === activeIssueId)) ||
      (activeFeatureId && !manifest.features?.some(feature => feature.feature_id === activeFeatureId))) {
      clearHighlight()
    }
  }, [manifest, activeIssueId, activeFeatureId, editingGeometry, savingFeature, clearHighlight])

  useEffect(() => {
    if (!highlightSelection || !latestReviewContext || editingGeometry || savingFeature) {return}

    if (highlightSelection.bucketId.startsWith('confirmed:')) {
      const kind = highlightSelection.bucketId.slice('confirmed:'.length)
      const confirmed = confirmedFeatureIds(latestReviewContext.review_groups)
      const features = latestReviewContext.features.filter(feature => feature.kind === kind && confirmed.has(feature.feature_id))
      const ids = features.map(feature => feature.feature_id)

      if (!ids.length || (highlightSelection.objectId && !ids.includes(highlightSelection.objectId))) {clearHighlight()}
      else {setHighlightedFeatureIds(highlightSelection.objectId ? [highlightSelection.objectId] : ids)}

      return
    }

    const bucket = reviewBuckets(latestReviewContext, true).find(item => item.id === highlightSelection.bucketId)
    const groups = bucket?.groups.filter(group => !highlightSelection.objectId || group.target_refs.includes(highlightSelection.objectId)) ?? []

    if (!groups.length) {clearHighlight();

 return}

    const refs = resolveReviewDisplayRefs(groups.flatMap(group => reviewGeometry(latestReviewContext, group)), topologyFaces, topologyEdges)

    if (!refs.length) {clearHighlight();

 return}

    setReviewGeometryRefs(refs)
  }, [highlightSelection, latestReviewContext, topologyFaces, topologyEdges, editingGeometry, savingFeature, clearHighlight])

  const prepareReviewForEdit = useCallback(async (pins: Pick<ReviewContext, 'revision' | 'snapshot_id' | 'edit_timeout_ms'>) => {
    if (!projectId) {throw new Error('请先打开当前项目。')}

    const context = await window.hermesDesktop.api<ReviewContext & { session_restored?: boolean; review_refreshed?: boolean; viewer_manifest?: string | null }>({
      path: `/api/dfm/projects/${encodeURIComponent(projectId)}/discovery/review/prepare`, method: 'POST',
      timeoutMs: reviewEditTimeout(pins),
      body: { expected_revision: pins.revision, expected_snapshot_id: pins.snapshot_id }
    })

    if (currentProjectId.current !== projectId) {return null}
    applyReviewContext(context)

    if (context.session_restored) {
      if (context.viewer_manifest) {workingViewer.current = { externalPath: target.manifestPath, path: context.viewer_manifest, projectId }}
      setManifest(current => current ? { ...current, status: 'discovery', issues: [], issue_count: 0, issue_type_counts: [], manual_review_results: [], manual_review_count: 0 } : current)
      setReviewNotice('几何会话已恢复，请按当前识别结果继续编辑和确认。')
    } else if (context.review_refreshed) {
      setReviewNotice('审核状态已更新，已使用当前区域打开编辑。')
    }

    return context
  }, [projectId, target.manifestPath, applyReviewContext])

  const withCurrentReview = useCallback(async <T,>(send: (pins: { expected_revision: number; expected_snapshot_id?: string }, timeoutMs: number) => Promise<T>): Promise<T> => {
    const base = editBase.current

    if (!base || !projectId || editConflict) {throw new Error('请重新载入最新区域后编辑。')}

    const refresh = async () => {
      const current = await window.hermesDesktop.api<ReviewContext>({ path: `/api/dfm/projects/${encodeURIComponent(projectId)}/discovery/review` })

      if (currentProjectId.current !== projectId) {throw new Error('项目已切换。')}
      applyReviewContext(current)

      if (!reviewEditUnchanged(base, current)) {
        setEditConflict(true)
        throw new Error('当前区域已被修改或移除，草稿已保留。请重新载入最新区域后再编辑。')
      }

      editBase.current = captureReviewEdit(current, base.kind, base.id)
      setEditingSnapshotId(current.snapshot_id)
      setProjectRevision(current.revision)
      setDirectionDraft(draft => draft ? { ...draft, revision: current.revision, snapshotId: current.snapshot_id } : null)

      return { pins: { expected_revision: current.revision, expected_snapshot_id: current.snapshot_id }, timeoutMs: reviewEditTimeout(current) }
    }

    const current = await refresh()

    try { return await send(current.pins, current.timeoutMs) }
    catch (cause) {
      if (!isReviewConflict(cause)) {throw cause}
      // One retry is safe only after verifying that this target did not change.
      const next = await refresh()

      try { return await send(next.pins, next.timeoutMs) }
      catch (last) {
        if (isReviewConflict(last)) {throw new Error('审核状态仍在更新，草稿已保留，请稍后重试保存。')}
        throw last
      }
    }
  }, [projectId, applyReviewContext, editConflict])

  const reloadEdit = async () => {
    const base = editBase.current

    if (!base || savingFeature || preparingReview) {return}
    setPreparingReview(true)

    try {
      const current = await window.hermesDesktop.api<ReviewContext>({ path: `/api/dfm/projects/${encodeURIComponent(base.projectId)}/discovery/review` })
      const fresh = await prepareReviewForEdit(current)

      if (!fresh) {return}
      const latestBase = captureReviewEdit(fresh, base.kind, base.id)

      if (latestBase.model !== base.model || latestBase.content === 'null' && base.content !== 'null') {
        if (fresh.viewer_manifest && projectId) {workingViewer.current = { externalPath: target.manifestPath, path: fresh.viewer_manifest, projectId }}
        cancelFeatureEdit()
        setDirectionDraft(null)
        setReloadRequest(value => value + 1)
        setReviewNotice('模型或对象已更新，请从当前列表重新选择。')

        return
      }

      if (directionDraft) {
        const scope = fresh.forming_scopes.find(item => item.forming_scope_id === base.id)!
        setDirectionDraft({ ...directionDraft, scope, text: scope.draw_direction ? JSON.stringify(scope.draw_direction) : '',
          revision: fresh.revision, snapshotId: fresh.snapshot_id, origin: undefined,
          mechanismKind: scope.mechanism_kind })
      } else if (base.kind === 'annotation') {
        const annotation = fresh.annotations.find(item => item.annotation_id === base.id)

        if (annotation) { setEditingAnnotation(annotation); setDraftFaceIndices(resolveGeometryRefFaceIndices(annotation.geometry_refs)) }
      } else if (base.kind === 'feature') {
        const feature = fresh.features.find(item => item.feature_id === base.id)!
        setEditingFeatureBaseline({ feature_id: feature.feature_id, kind: feature.kind, confidence: feature.confidence,
          subtype: String(feature.properties.subtype ?? ''), geometry_refs: feature.properties.geometry_refs ?? [],
          parameters: feature.properties, method: String(feature.properties.method ?? ''), diagnostics: feature.properties.diagnostics ?? {} })
        setDraftFaceIndices(resolveGeometryRefFaceIndices(feature.properties.geometry_refs))
      }

      editBase.current = latestBase
      setEditingSnapshotId(fresh.snapshot_id)
      setProjectRevision(fresh.revision)
      setEditConflict(false)
      setReviewError('')
      setDirectionError('')
    } catch (cause) { if (currentProjectId.current === base.projectId) {setReviewError(cause instanceof Error ? cause.message : String(cause))} }
    finally { if (currentProjectId.current === base.projectId) {setPreparingReview(false)} }
  }

  const startDirectionEdit = useCallback(async (scope: ReviewScope, context: ReviewContext) => {
    if (savingFeature || preparingReview || editingGeometry) {return}

    setPreparingReview(true)
    setScopePreparation(scope.mode === 'local' ? { scopeId: scope.forming_scope_id, startedAt: Date.now() } : null)
    setReviewError('')

    try {
      const fresh = await prepareReviewForEdit(context)

      if (!fresh) {return}
      const current = fresh.forming_scopes.find(item => item.forming_scope_id === scope.forming_scope_id && item.status !== 'stale')

      if (!current) {throw new Error('成型方案已变化，请从当前列表重新选择。')}
      editBase.current = captureReviewEdit(fresh, 'scope', current.forming_scope_id)
      setEditConflict(false)
      highlightReview(scopeDisplayGeometry(current), objectHighlightSelection('forming', current.forming_scope_id, fresh), true)
      setEditingScopeId(null)
      setDraftFaceIndices(null)
      setActiveDirectionId(current.forming_scope_id)
      setDirectionError('')
      setDirectionDraft({ scope: current, revision: fresh.revision, snapshotId: fresh.snapshot_id,
        text: current.draw_direction ? JSON.stringify(current.draw_direction) : '', mode: 'rotate', origin: axisOrigins[current.forming_scope_id],
        mechanismKind: current.mechanism_kind })
      setScopePreparation(null)
    } catch (cause) {
      if (currentProjectId.current === projectId) {
        const message = cause instanceof Error ? cause.message : String(cause)
        setReviewError(message)
        setScopePreparation(current => current ? { ...current, error: message } : null)
      }
    } finally {
      if (currentProjectId.current === projectId) {setPreparingReview(false)}
    }
  }, [savingFeature, preparingReview, editingGeometry, prepareReviewForEdit, highlightReview, axisOrigins, projectId])

  const saveDirectionEdit = async (action: 'stage' | 'confirm' = 'stage') => {
    if (!directionDraft || !projectId || savingFeature || editConflict) {return}
    const vector = parseDirectionVector(directionDraft.text)

    if (!vector && !(action === 'stage' && directionDraft.scope.mode === 'local' && directionDraft.text.trim() === '')) {return}

    if (directionDraft.scope.mode === 'local' && !directionDraft.scope.geometry_refs.length && !directionDraft.scope.undercut_geometry_refs.length) {
      setDirectionError('至少保留一类面的选面；删除整个方案请取消修改后使用删除入口。')

      return
    }

    setSavingFeature(true)
    setDirectionError('')

    try {
      const context = await withCurrentReview((pins, timeoutMs) => window.hermesDesktop.api<ReviewContext & { viewer_manifest?: string | null; direction_status?: string }>({
        path: `/api/dfm/projects/${encodeURIComponent(projectId)}/discovery/review/direction`, method: 'POST',
        timeoutMs,
        body: { action, forming_scope_id: directionDraft.scope.forming_scope_id, draw_direction: vector,
          mechanism_kind: directionDraft.mechanismKind, geometry_refs: directionDraft.scope.geometry_refs,
          undercut_geometry_refs: directionDraft.scope.undercut_geometry_refs,
          ...pins }
      }))

      if (currentProjectId.current !== projectId) {return}

      if (context.viewer_manifest) {workingViewer.current = { externalPath: target.manifestPath, path: context.viewer_manifest, projectId }}
      applyReviewContext(context)
      setAxisOrigins(previous => directionDraft.origin ? { ...previous, [directionDraft.scope.forming_scope_id]: directionDraft.origin } :
        Object.fromEntries(Object.entries(previous).filter(([id]) => id !== directionDraft.scope.forming_scope_id)))

      if (context.direction_status !== 'forming_direction_unchanged') {
        setManifest(current => current ? { ...current, status: 'discovery', issues: [], issue_count: 0, issue_type_counts: [], manual_review_results: [], manual_review_count: 0 } : current)
      }

      setDirectionDraft(null)
      setEditingScopeId(null)
      setDraftFaceIndices(null)
      editBase.current = null
      setEditConflict(false)
      setReviewNotice(context.direction_status === 'forming_direction_unchanged' ? '方案内容未改变，保留原确认和计算结果。' : context.direction_status === 'forming_direction_staged' ? '方案草稿已保存。请核对倒扣面、成型面、机构和方向，再确认完整方案。' : directionDraft.scope.mode === 'local' ? '方案已整体确认，相关计算按依赖继续。' : '主出模方向已更新，原报告保留为历史结果。')
    } catch (cause) {
      if (currentProjectId.current === projectId) {setDirectionError(cause instanceof Error ? cause.message : String(cause))}
    } finally {
      if (currentProjectId.current === projectId) {setSavingFeature(false)}
    }
  }

  const activeDirectionScope = latestReviewContext?.forming_scopes.find(scope => scope.forming_scope_id === activeDirectionId && scope.status !== 'stale')

  const axisState: DirectionAxisState | null = directionDraft ? {
    scopeId: directionDraft.scope.forming_scope_id, direction: parseDirectionVector(directionDraft.text),
    geometryRefs: scopeDisplayGeometry(directionDraft.scope), editing: !editingScopeId, mode: directionDraft.mode, origin: directionDraft.origin
  } : activeDirectionScope ? { scopeId: activeDirectionScope.forming_scope_id, direction: activeDirectionScope.draw_direction,
    geometryRefs: scopeDisplayGeometry(activeDirectionScope), editing: false, mode: 'rotate', origin: axisOrigins[activeDirectionScope.forming_scope_id] } : null

  const displayFormingScope = directionDraft?.scope.mode === 'local' ? directionDraft.scope
    : activeDirectionScope?.mode === 'local' ? activeDirectionScope : null

  const directionEditor = directionDraft && <DfmDirectionEditor busy={savingFeature} draft={directionDraft}
    error={directionError} faceRole={editingScopeId ? editingScopeRole : null}
    onCancel={() => {
      setDirectionDraft(null); setEditingScopeId(null); setDraftFaceIndices(null)
      setDirectionError(''); setEditConflict(false); editBase.current = null
    }}
    onChange={setDirectionDraft}
    onConfirm={() => void saveDirectionEdit('confirm')}
    onEditFaces={role => { setEditingScopeId(directionDraft.scope.forming_scope_id); setEditingScopeRole(role); setDirectionError('') }}
    onFinishFaces={() => setEditingScopeId(null)}
    onSave={() => void saveDirectionEdit('stage')} saveBlocked={editConflict} />

  const saveFeatureEdit = useCallback(async () => {
    if (
      editConflict || (!editingFeatureId && !editingAnnotation) ||
      !draftFaceIndices?.size ||
      !projectId ||
      projectRevision == null
    ) {
      setReviewError('区域至少需要保留一个面。')

      return
    }

    setSavingFeature(true)
    setReviewError('')

    try {
      const faceIndices = Array.from(draftFaceIndices).sort((left, right) => left - right)

      const refsByIndex = new Map(topologyFaces.map(face => [face.geometry_ref.index, face.geometry_ref] as const))
      const geometryRefs = faceIndices.map(index => refsByIndex.get(index))

      if (geometryRefs.some(ref => !ref?.input_sha256 || !ref.topology_snapshot_id || !ref.entity_id))
        {throw new Error('选择中包含无法验证拓扑身份的面，请刷新后重试。')}

      const result = await withCurrentReview((pins, timeoutMs) => window.hermesDesktop.api<{ revision: number; geometry_refs?: GeometryReference[]; unchanged?: boolean; viewer_manifest?: string | null }>({
        path: `/api/dfm/projects/${encodeURIComponent(projectId)}/discovery/${editingAnnotation ? 'annotations' : `features/${encodeURIComponent(editingFeatureId!)}`}`,
        method: 'PATCH',
        timeoutMs,
        body: editingAnnotation
          ? {
              ...pins,
              annotations: [
                {
                  annotation_id: editingAnnotation.annotation_id,
                  kind: editingAnnotation.kind,
                  value: editingAnnotation.value,
                  geometry_refs: geometryRefs
                }
              ]
            }
          : { ...pins, geometry_refs: geometryRefs }
      }))

      if (currentProjectId.current !== projectId) {return}

      if (result.viewer_manifest && !result.unchanged) {
        workingViewer.current = { externalPath: target.manifestPath, path: result.viewer_manifest, projectId }
      }

      setManifest(current =>
        current
          ? {
              ...current,
              project_revision: result.revision,
              ...(result.unchanged ? {} : { status: 'discovery' as const, review_status: 'pending' as const,
                issues: [], issue_count: 0, issue_type_counts: [], manual_review_results: [], manual_review_count: 0 }),
              features: current.features?.map(feature =>
                feature.feature_id === editingFeatureId && result.geometry_refs
                  ? {
                      ...feature,
                      geometry_refs: result.geometry_refs
                    }
                  : feature
              )
            }
          : current
      )
      setProjectRevision(result.revision)

      if (!result.unchanged) {setReviewNotice('识别结果已更新。请重新确认受影响的条目，再更新分析；原报告保留为历史结果。')}
      setEditingFeatureId(null)
      setEditingFeatureBaseline(null)
      setEditingScopeId(null)
      setEditingAnnotation(null)
      setDraftFaceIndices(null)
      editBase.current = null
      setEditConflict(false)

      const context = await window.hermesDesktop.api<ReviewContext>({
        path: `/api/dfm/projects/${encodeURIComponent(projectId)}/discovery/review`
      })

      applyReviewContext(context)
    } catch (cause) {
      if (currentProjectId.current === projectId) {
        const message = cause instanceof Error ? cause.message : String(cause)
        setReviewError(/geometry_edit_conflict|snapshot_conflict|manifest_conflict/.test(message)
          ? '审核状态已更新，草稿已保留，请重试。'
          : message)
      }
    } finally {
      if (currentProjectId.current === projectId) {setSavingFeature(false)}
    }
  }, [
    applyReviewContext,
    withCurrentReview,
    editConflict,
    draftFaceIndices,
    editingFeatureId,
    editingAnnotation,
    projectRevision,
    projectId,
    topologyFaces,
    target.manifestPath
  ])

  const startAnnotationEdit = useCallback(
    (annotation: ReviewAnnotation, context: ReviewContext, values: string[]) => {
      if (savingFeature) {return}
      editBase.current = captureReviewEdit(context, 'annotation', annotation.annotation_id)
      setEditConflict(false)
      highlightReview(annotation.geometry_refs)
      setEditingFeatureId(null)
      setEditingFeatureBaseline(null)
      setEditingScopeId(null)
      setEditingAnnotation(annotation)
      setEditingSnapshotId(context.snapshot_id)
      setAnnotationValues(values)
      setDraftFaceIndices(new Set(annotation.geometry_refs.filter(ref => ref.kind === 'face').map(ref => ref.index)))
      setProjectRevision(context.revision)
      setReviewError('')
    },
    [highlightReview, savingFeature]
  )

  const createAnnotation = useCallback(
    (kind: AnnotationKind, context: ReviewContext) => {
      const values = kind.value_schema.enum ?? []

      if (!values.length) {return}
      startAnnotationEdit(
        {
          annotation_id: `annotation_${crypto.randomUUID()}`,
          kind: kind.kind,
          value: values[0],
          status: 'active',
          geometry_refs: []
        },
        context,
        values
      )
    },
    [startAnnotationEdit]
  )

  const status = manifest?.status ?? target.status
  const canEditDiscovery = status !== 'preview' && Boolean(projectId && manifest?.snapshot_id)
  const confirmedIds = confirmedFeatureIds(manifest?.review_groups ?? [])

  const confirmedFeatures = (manifest?.features ?? []).filter(feature =>
    manifest?.review_groups ? confirmedIds.has(feature.feature_id) : status !== 'discovery'
  )

  const editedFeature = editingFeatureBaseline
  const originalEditedFaces = resolveGeometryRefFaceIndices(editedFeature?.geometry_refs)

  const featureResults = ((manifest?.features?.length ?? 0) === 0 ? (
              <div className="rounded-lg border border-amber-400/20 bg-amber-400/10 p-3 text-xs leading-5 text-amber-100">
                {status === 'preview'
                  ? '分析完成后，识别到的孔、圆角、倒角等特征会显示在这里。'
                  : '当前没有可显示的结构化特征。'}
              </div>
            ) : (
              <DfmFeatureGroups
                activeFeatureElementRef={activeFeatureElementRef}
                activeFeatureId={activeFeatureId}
                editingFeatureId={editingFeatureId}
                features={confirmedFeatures}
                highlightSelection={highlightSelection}
                key={projectId ?? target.manifestPath}
                labels={FEATURE_LABELS}
                navigationSequence={reviewNavigation?.category === 'structure' ? reviewNavigation.sequence : undefined}
                onToggleKind={toggleFeatureKind}
                renderFeature={(feature, index) => {
                  const selected = feature.feature_id === activeFeatureId
                  const editing = feature.feature_id === editingFeatureId

                  const visibleRefs = editing
                    ? Array.from(draftFaceIndices ?? []).sort((left, right) => left - right)
                    : feature.geometry_refs.map(ref => ref.index)

                  const refs = visibleRefs.map(indexValue => 'face #' + indexValue).join('、') || '无拓扑引用'

                  const confidence =
                    typeof feature.confidence === 'number' ? Math.round(feature.confidence * 100) + '%' : '—'

                  return (
                    <div
                      className={
                        'flex w-full items-start gap-2 rounded-lg border p-2.5 text-left transition ' +
                        (selected
                          ? 'border-amber-400/70 bg-amber-500/18 shadow-[0_0_0_1px_rgba(251,191,36,0.12)]'
                          : 'border-white/10 bg-white/[0.035] hover:border-white/20 hover:bg-white/[0.07]')
                      }
                      data-review-object={feature.feature_id}
                      key={feature.feature_id}
                      ref={selected ? activeFeatureElementRef : undefined}
                    >
                      <button
                        aria-pressed={selected}
                        className="flex min-w-0 flex-1 items-start gap-2 text-left"
                        disabled={editingGeometry || savingFeature}
                        onClick={() => toggleFeatureSelection(feature)}
                        type="button"
                      >
                        <span className="mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-amber-500/20 text-[10px] text-amber-100">
                          {index + 1}
                        </span>
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <h2 className="text-xs font-medium text-slate-100">
                              {FEATURE_LABELS[feature.kind] ?? feature.kind}
                            </h2>
                            <span className="rounded bg-white/5 px-1.5 py-0.5 text-[9px] text-slate-400">
                              {FEATURE_SUBTYPE_LABELS[feature.subtype] ?? feature.subtype}
                            </span>
                            {Array.isArray(feature.diagnostics.unassigned_geometry_refs) && feature.diagnostics.unassigned_geometry_refs.length > 0 && (
                              <span className="text-[9px] text-amber-200">新增面待分区</span>
                            )}
                          </div>
                          <p className="mt-1.5 text-[11px] text-slate-300">
                            {formatFeatureParameters(feature.parameters)}
                          </p>
                          <p className="mt-1 text-[10px] text-slate-500">
                            置信度 {confidence} · {refs}
                          </p>
                        </div>
                      </button>
                      {canEditDiscovery && (
                        <div className="flex shrink-0 gap-1">
                          {editing ? (
                            <span className="text-[10px] text-amber-100">上方编辑中</span>
                          ) : (
                            <button
                              className="rounded bg-white/5 px-1.5 py-1 text-[10px] text-slate-300 hover:bg-white/10 disabled:opacity-50"
                              disabled={
                                editingGeometry || savingFeature
                              }
                              onClick={() => startFeatureEdit(feature)}
                              type="button"
                            >
                              编辑
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  )
                }}
                selectionDisabled={editingGeometry || savingFeature}
              />
            ))

  if (error) {
    return (
      <main className="grid h-full place-items-center bg-[#10151d] p-5 text-slate-100">
        <div className="max-w-md rounded-xl border border-red-500/30 bg-red-500/10 p-5">
          <h1 className="mb-2 text-sm font-semibold">DFM 三维结果加载失败</h1>
          <p className="break-words text-xs text-red-100/80">{error}</p>
          <button
            className="mt-4 rounded-md bg-white/10 px-3 py-1.5 text-xs hover:bg-white/15"
            onClick={() => setReloadRequest(value => value + 1)}
            type="button"
          >
            重试
          </button>
        </div>
      </main>
    )
  }

  if (!manifest || !scene) {
    return (
      <main className="grid h-full place-items-center bg-[#10151d] text-xs text-slate-300">正在生成 STEP 预览…</main>
    )
  }

  return (
    <main className="grid h-full min-h-0 grid-rows-[auto_1fr] overflow-hidden bg-[#10151d] text-slate-100">
      <header className="flex min-w-0 items-center justify-between gap-3 border-b border-white/10 px-3 py-2.5">
        <div className="min-w-0">
          <h1 className="truncate text-sm font-semibold">DFM 三维模型</h1>
          <p className="mt-0.5 truncate text-[11px] text-slate-400">
            {target.failure
              ? '分析未完成 · 保留当前模型'
              : status === 'preview'
                ? 'STEP 预览 · 等待风险分析'
                : `${manifest.scope_id} · ${manifest.verification_level}`}{' '}
            · {scene.render_mesh_snapshot.triangle_count.toLocaleString()} 个三角形
          </p>
          {target.failure && (
            <p role="alert" className="mt-1 break-words text-xs text-red-300" title={target.failure.code}>
              {target.failure.message}
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <button
            className="rounded-md border border-white/10 bg-white/5 px-2 py-1 text-[11px] text-slate-200 hover:bg-white/10"
            onClick={() => setFitRequest(value => value + 1)}
            type="button"
          >
            适合窗口
          </button>
          {embedded && (
            <button
              className="rounded-md border border-white/10 bg-white/5 px-2 py-1 text-[11px] text-slate-200 hover:bg-white/10"
              onClick={() => void openDfmViewerWindow(target.manifestPath)}
              type="button"
            >
              弹出
            </button>
          )}
          <span
            className={`rounded-full px-2 py-1 text-[11px] ${
              status === 'preview'
                ? 'bg-sky-500/15 text-sky-200'
                : status === 'discovery'
                  ? 'bg-amber-500/15 text-amber-200'
                  : 'bg-red-500/15 text-red-200'
            }`}
          >
            {status === 'preview'
              ? '预览'
              : status === 'discovery'
                ? `${manifest.features?.length ?? 0} 个候选 · ${confirmedFeatures.length} 已确认`
                : manifest.issue_count + ' 问题 · ' + (manifest.features?.length ?? 0) + ' 特征'}
          </span>
        </div>
      </header>
      <section
        className="grid min-h-0"
        style={
          embedded
            ? { gridTemplateRows: 'minmax(14rem, 1fr) minmax(14rem, 1fr)' }
            : { gridTemplateColumns: 'minmax(0, 1fr) 20rem' }
        }
      >
        <div className="relative min-h-0 overflow-hidden">
          <ModelCanvas
            activeFeatures={
              reviewGeometryRefs.length
                ? [
                    {
                      feature_id: 'review-selection',
                      kind: 'review_scope',
                      subtype: '',
                      confidence: 1,
                      geometry_refs: reviewGeometryRefs,
                      parameters: {},
                      diagnostics: {},
                      method: 'user_review'
                    }
                  ]
                : activeFeatures
            }
            activeIssue={activeIssue}
            axisSelectable={!editingGeometry}
            directionAxis={axisState}
            document={scene}
            fitRequest={fitRequest}
            focusRequest={focusRequest}
            formingScope={displayFormingScope}
            onAxisSelected={() => {
              if (editingGeometry || !activeDirectionScope || !latestReviewContext) {return}

              void startDirectionEdit(activeDirectionScope, latestReviewContext)
            }}
            onBackgroundClick={clearDisplaySelection}
            onDirectionChange={(direction, origin) => setDirectionDraft(current => current ? { ...current, text: JSON.stringify(direction), origin } : current)}
            onDismissMenu={dismissObjectMenu}
            onFacePick={handleFacePick}
            onObjectMenu={openObjectMenu}
            pickedFaceIndex={pickedFaceIndex}
            selectedFaceIndices={editingFeatureId || editingAnnotation ? draftFaceIndices : null}
            topologyFaces={topologyFaces}
          />
          {objectMenu && <DfmObjectMenu busy={editingGeometry || savingFeature || reviewBusy} items={objectMenu.candidates.map(objectMenuItem)}
            onClear={() => { clearDisplaySelection(); dismissObjectMenu() }}
            onClose={dismissObjectMenu} onRemove={item => { dismissObjectMenu(); void reviewActions.current?.removeObject(item.target) }}
            onSelect={item => { handleFacePick(objectMenu.faceIndex, item.target); setObjectMenu({ ...objectMenu, target: item.target }) }}
            point={objectMenu.point}
            selected={objectMenu.target ? objectMenuItem(objectMenu.target) : null} />}
          <div className="pointer-events-none absolute bottom-2 left-2 rounded-md bg-black/55 px-2 py-1 text-[10px] text-slate-300 backdrop-blur">
            {editingFeatureId || editingScopeId || editingAnnotation
              ? '单击面切换选中 · 中键平移 · 滚轮缩放'
              : '左键旋转 · 滚轮缩放 · 中键平移 · 空白处右键滑移 · 单击选择面，再点取消'}
          </div>
          {displayFormingScope && <div aria-label="成型方案图例" className="pointer-events-none absolute left-2 top-2 rounded-md bg-black/65 px-2 py-1.5 text-[10px] text-slate-200">
            <span className="text-amber-300">■ 倒扣面 {displayFormingScope.undercut_geometry_refs.length}</span>
            <span className="ml-3 text-cyan-200">□ 成型面 {displayFormingScope.geometry_refs.length}</span>
            <p className="mt-1">两类面可重叠 · {axisState?.direction ? displayFormingScope.status === 'confirmed' && !directionDraft ? '箭头：已确认方向' : '箭头：候选退出方向' : '退出方向待补充'}</p>
          </div>}
          {pickedFaceIndex != null && (
            <div className="pointer-events-none absolute right-2 top-2 rounded-md border border-cyan-300/20 bg-black/60 px-2 py-1 text-[10px] text-cyan-100 backdrop-blur">
              当前面 #{pickedFaceIndex}
            </div>
          )}
        </div>
        <aside
          className={`grid min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-2 overflow-hidden bg-[#151b24] p-2.5 ${
            embedded ? 'border-t border-white/10' : 'border-l border-white/10'
          }`}
        >
          <div className="grid grid-cols-3 rounded-lg border border-white/10 bg-black/15 p-1" role="tablist">
            <button
              aria-selected={reviewPaneTab === 'features'}
              className={`flex items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-[11px] transition ${
                reviewPaneTab === 'features'
                  ? 'bg-amber-500/15 text-amber-100 shadow-sm'
                  : 'text-slate-400 hover:bg-white/5 hover:text-slate-200'
              }`}
              onClick={() => setReviewPaneTab('features')}
              role="tab"
              type="button"
            >
              特征点
              <span className="rounded-full bg-black/20 px-1.5 py-0.5 text-[9px]">
                {manifest.features?.length ?? 0}
              </span>
            </button>
            <button
              aria-selected={reviewPaneTab === 'clarifications'}
              className={`flex items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-[11px] transition ${
                reviewPaneTab === 'clarifications'
                  ? 'bg-cyan-500/15 text-cyan-100 shadow-sm'
                  : 'text-slate-400 hover:bg-white/5 hover:text-slate-200'
              }`}
              onClick={() => setReviewPaneTab('clarifications')}
              role="tab"
              type="button"
            >
              补充信息
              <span className="rounded-full bg-cyan-500/15 px-1.5 py-0.5 text-[9px] text-cyan-100">
                {clarificationRequest?.context?.items.length ?? 0}
              </span>
            </button>
            <button
              aria-selected={reviewPaneTab === 'issues'}
              className={`flex items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-[11px] transition ${
                reviewPaneTab === 'issues'
                  ? 'bg-red-500/15 text-red-100 shadow-sm'
                  : 'text-slate-400 hover:bg-white/5 hover:text-slate-200'
              }`}
              onClick={() => setReviewPaneTab('issues')}
              role="tab"
              type="button"
            >
              问题明细
              <span className="rounded-full bg-red-500/15 px-1.5 py-0.5 text-[9px] text-red-100">
                {manifest.issue_count}
              </span>
            </button>
          </div>
          <section
            className={
              reviewPaneTab === 'clarifications'
                ? 'order-1 min-h-0 overflow-hidden'
                : reviewPaneTab === 'issues'
                  ? 'order-1 min-h-0 overflow-y-auto pr-1'
                  : 'hidden'
            }
          >
            {reviewPaneTab === 'clarifications' ? (
              clarificationRequest ? (
                <DfmClarificationPanel
                  features={manifest.features ?? []}
                  onFeaturesHighlight={handleFeaturesHighlight}
                  priorityFeatureId={clarificationPriorityFeatureId}
                  request={clarificationRequest}
                />
              ) : (
                <div className="rounded-lg border border-cyan-400/15 bg-cyan-500/[0.06] p-3 text-xs leading-5 text-cyan-100">
                  当前没有待补充的信息。
                </div>
              )
            ) : (
              <>
                <div className="mb-2 flex items-center justify-between">
                  <h2 className="text-xs font-semibold text-red-100">问题分类与明细</h2>
                  <span className="rounded-full bg-red-500/15 px-2 py-0.5 text-[10px] text-red-200">
                    {manifest.issue_count}
                  </span>
                </div>
                <DfmManualReviewNotice results={manifest.manual_review_results ?? []}
                  onSelectFeature={featureId => handleFeaturesHighlight([featureId])} />
                {issueTypeCounts.length > 0 && (
                  <div className="mb-2 flex flex-wrap gap-1.5">
                    {issueTypeCounts.map(item => (
                      <span
                        className="rounded-full border border-red-300/15 bg-red-500/10 px-2 py-1 text-[10px] text-red-100"
                        key={item.issue_type_id}
                      >
                        {item.label} <strong className="ml-1 font-semibold">{item.count}</strong>
                      </span>
                    ))}
                  </div>
                )}
                {manifest.issues.length === 0 ? (
                  <div className="rounded-lg border border-sky-400/20 bg-sky-400/10 p-3 text-xs leading-5 text-sky-100">
                    {status === 'preview'
                      ? '模型已加载。DFM 分析完成后，风险项会自动更新到这里。'
                      : (manifest.manual_review_count ?? 0) > 0 ? '存在待复核测量，请先处理橙色提示。' : '当前规则未发现需要高亮的问题。'}
                  </div>
                ) : (
                  <div className="grid gap-2">
                    {manifest.issues.map((issue, index) => {
                      const selected = issue.evaluation_id === activeIssueId

                      const refs =
                        issue.geometry_refs.map(ref => `${ref.kind} #${ref.index}`).join('、') || '无拓扑引用'

                      const [, issueTypeLabel] = classifyViewerIssueType(issue)

                      return (
                        <button
                          aria-pressed={selected}
                          className={`w-full rounded-lg border p-2.5 text-left transition ${
                            selected
                              ? 'border-red-400/70 bg-red-500/18 shadow-[0_0_0_1px_rgba(248,113,113,0.12)]'
                              : 'border-white/10 bg-white/[0.035] hover:border-white/20 hover:bg-white/[0.07]'
                          }`}
                          key={issue.evaluation_id}
                          onClick={() => handleIssueSelect(issue)}
                          type="button"
                        >
                          <div className="flex items-start gap-2">
                            <span className="mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-red-500/20 text-[10px] text-red-100">
                              {index + 1}
                            </span>
                            <div className="min-w-0">
                              <h2 className="text-xs font-medium text-slate-100">{issue.title}</h2>
                              <p className="mt-1 text-[11px] text-red-200">{issueTypeLabel}</p>
                              <p className="mt-0.5 text-[10px] text-slate-500">{issue.check_id || issue.metric_id}</p>
                              <p className="mt-1.5 text-[11px] text-slate-300">
                                未满足：实际 {formatViewerValue(issue.actual)}{' '}
                                {issue.operator === 'between' ? '∈' : issue.operator} 目标{' '}
                                {formatViewerValue(issue.expected)}
                              </p>
                              <p className="mt-1 break-words text-[10px] text-slate-500">{refs}</p>
                            </div>
                          </div>
                        </button>
                      )
                    })}
                  </div>
                )}
              </>
            )}
          </section>
          <section className={reviewPaneTab === 'features' ? 'order-1 min-h-0 overflow-y-auto pr-1' : 'hidden'}>
            {preparingReview && <p className="mb-2 text-[11px] text-cyan-100" role="status">正在准备编辑…</p>}
            {reviewNotice && <p className="mb-2 rounded border border-cyan-300/20 bg-cyan-500/10 p-2 text-[10px] text-cyan-100">{reviewNotice}</p>}
            {editConflict && <div className="mb-2 rounded border border-amber-400/40 p-2 text-[11px] text-amber-100" role="alert">
              <p>当前区域已修改或移除，草稿仍保留。请取消编辑，或重新载入最新区域。</p>
              <button className="mt-1 text-cyan-100" disabled={savingFeature || preparingReview} onClick={() => void reloadEdit()} type="button">重新载入最新区域</button>
            </div>}
            {editedFeature && (
              <DfmFeatureEditor
                addedCount={[...(draftFaceIndices ?? [])].filter(index => !originalEditedFaces.has(index)).length}
                busy={savingFeature}
                faceCount={draftFaceIndices?.size ?? 0}
                identity={editedFeature.feature_id}
                label={FEATURE_LABELS[editedFeature.kind] ?? editedFeature.kind}
                onCancel={cancelFeatureEdit}
                onRestore={() => setDraftFaceIndices(new Set(originalEditedFaces))}
                onSave={() => void saveFeatureEdit()}
                removedCount={[...originalEditedFaces].filter(index => !draftFaceIndices?.has(index)).length}
                saveBlocked={editConflict}
              />
            )}
            {editingAnnotation && (
              <DfmAnnotationEditor
                busy={savingFeature}
                faceCount={draftFaceIndices?.size ?? 0}
                identity={editingAnnotation.annotation_id}
                onCancel={cancelFeatureEdit}
                onSave={() => void saveFeatureEdit()}
                onValue={value => setEditingAnnotation(current => (current ? { ...current, value } : null))}
                saveBlocked={editConflict}
                value={editingAnnotation.value}
                values={annotationValues}
              />
            )}
            {directionDraft?.scope.mode === 'main' && directionEditor}
            {projectId && status !== 'preview' && (
              <DfmReviewPanel
                actionBusy={savingFeature || preparingReview}
                actionsRef={reviewActions}
                confirmedFeaturesContent={featureResults}
                editing={Boolean(editingGeometry || savingFeature)}
                editingScopeId={directionDraft?.scope.mode === 'local' ? directionDraft.scope.forming_scope_id : null}
                highlightSelection={highlightSelection}
                latestContext={latestReviewContext}
                navigation={reviewNavigation}
                onBusyChange={setReviewBusy}
                onChanged={applyReviewContext}
                onCreateAnnotation={createAnnotation}
                onEditAnnotation={startAnnotationEdit}
                onEditDirection={startDirectionEdit}
                onEditFeature={canEditDiscovery ? (feature, context) => {
                  applyReviewContext(context)
                  startFeatureEdit({ feature_id: feature.feature_id, kind: feature.kind, confidence: feature.confidence,
                    geometry_refs: feature.properties.geometry_refs ?? [], subtype: String(feature.properties.subtype ?? ''),
                    parameters: feature.properties, diagnostics: feature.properties.diagnostics ?? {},
                    method: String(feature.properties.method ?? '') }, context)
                } : undefined}
                onHighlight={highlightReview}
                onRefreshed={applyReviewContext}
                onSelectDirection={scope => setActiveDirectionId(scope.forming_scope_id)}
                projectId={projectId}
                revision={projectRevision}
                scopeEditor={directionDraft?.scope.mode === 'local' ? directionEditor : null}
                scopePreparation={scopePreparation}
              />
            )}
            {reviewError && (
              <div className="mb-2 rounded-md border border-red-400/25 bg-red-500/10 px-2 py-1.5 text-[10px] text-red-100">
                {reviewError}
              </div>
            )}
            {(!projectId || status === 'preview') && featureResults}
          </section>
        </aside>
      </section>
    </main>
  )
}
