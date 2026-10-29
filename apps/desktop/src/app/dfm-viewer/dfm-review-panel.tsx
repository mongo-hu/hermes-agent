import { useCallback, useEffect, useRef, useState } from 'react'

import {
  directionUnlocked,
  featureNeedsPartReview,
  parseReviewDirection,
  reviewBuckets,
  type ReviewContext,
  type ReviewFeature,
  reviewGeometry,
  type ReviewGroup,
  type ReviewScope
} from './dfm-review'
import type { AnnotationKind, ReviewAnnotation } from './dfm-review'
import type { GeometryReference } from './dfm-viewer-geometry'

interface DfmReviewPanelProps {
  projectId: string
  revision: number | null
  onChanged: (context: ReviewContext) => void
  onRefreshed?: (context: ReviewContext) => void
  onHighlight: (refs: GeometryReference[]) => void
  onEditScope?: (scope: ReviewScope, revision: number) => void
  onEditAnnotation?: (annotation: ReviewAnnotation, revision: number, values: string[]) => void
  onCreateAnnotation?: (kind: AnnotationKind, revision: number) => void
  onEditFeature?: (feature: ReviewFeature, context: ReviewContext) => void
  editing?: boolean
  latestContext?: ReviewContext | null
}

export function DfmReviewPanel({
  projectId,
  revision,
  onChanged,
  onRefreshed,
  onHighlight,
  onEditScope,
  onEditAnnotation,
  onCreateAnnotation,
  onEditFeature,
  editing = false,
  latestContext
}: DfmReviewPanelProps) {
  const [loadedContext, setContext] = useState<ReviewContext | null>(null)
  const localContext = loadedContext?.project_id === projectId ? loadedContext : null

  const context =
    latestContext?.project_id === projectId && (!localContext || latestContext.revision >= localContext.revision)
      ? latestContext
      : localContext

  const [selected, setSelected] = useState<string[]>([])
  const [directions, setDirections] = useState<Record<string, string[]>>({})
  const [busy, setBusy] = useState(false)
  const [openBuckets, setOpenBuckets] = useState<Set<string>>(new Set())
  const [limits, setLimits] = useState<Record<string, number>>({})
  const [error, setError] = useState('')
  const currentProject = useRef(projectId)
  currentProject.current = projectId
  const onRefreshedRef = useRef(onRefreshed)
  onRefreshedRef.current = onRefreshed
  const base = `/api/dfm/projects/${encodeURIComponent(projectId)}/discovery/review`

  useEffect(() => {
    setBusy(false)
    setSelected([])
    setDirections({})
    setOpenBuckets(new Set())
    setLimits({})
  }, [projectId])

  const refresh = useCallback(async () => {
    const next = await window.hermesDesktop.api<ReviewContext>({ path: base })

    if (currentProject.current !== projectId || next.project_id !== projectId) {return}
    setContext(previous => (previous?.project_id === projectId && previous.revision > next.revision ? previous : next))
    onRefreshedRef.current?.(next)
    setSelected(previous =>
      previous.filter(id =>
        next.review_groups.some(group => group.review_group_id === id && group.status === 'pending_review')
      )
    )
  }, [base, projectId])

  useEffect(() => {
    setContext(previous => (previous?.project_id === projectId ? previous : null))
    setError('')
    void refresh().catch(cause => {
      if (currentProject.current === projectId) {setError(cause instanceof Error ? cause.message : String(cause))}
    })
  }, [refresh, revision, projectId])

  const submit = async (path: string, body: Record<string, unknown>) => {
    if (!context || busy || editing) {return}
    setBusy(true)
    setError('')

    try {
      const next = await window.hermesDesktop.api<ReviewContext>({
        path: `${base}/${path}`,
        method: 'POST',
        body: { ...body, expected_revision: context.revision, expected_snapshot_id: context.snapshot_id }
      })

      if (currentProject.current !== projectId || next.project_id !== projectId) {return}
      setContext(next)
      setSelected([])
      onChanged(next)
    } catch (cause) {
      if (currentProject.current !== projectId) {return}
      setError(cause instanceof Error ? cause.message : String(cause))
      await refresh().catch(() => undefined)
    } finally {
      if (currentProject.current === projectId) {setBusy(false)}
    }
  }

  const submitDirection = (group: ReviewGroup) => {
    const direction = parseReviewDirection(directions[group.review_group_id] ?? ['', '', ''])

    if (!direction) {
      setError('请输入三个有限数值，且方向不能全为零。')

      return
    }

    void submit('direction', { forming_scope_id: group.target_refs[0], draw_direction: direction })
  }

  return (
    <section
      aria-label="DFM 分组确认"
      className="mb-3 rounded-lg border border-cyan-300/20 bg-cyan-500/[0.05] p-2 text-[11px]"
    >
      <div className="mb-2 flex items-center justify-between">
        <h2 className="font-semibold text-cyan-100">
          待确认 · {context?.review_groups.filter(group => group.status === 'pending_review').length ?? 0} 组
        </h2>
        <button
          className="text-cyan-200"
          disabled={busy}
          onClick={() => void refresh().catch(cause => setError(String(cause)))}
          type="button"
        >
          刷新
        </button>
      </div>
      <p className="mb-2 text-[10px] text-slate-400">每组独立确认；局部方向未提供，不阻塞已就绪的其他组。</p>
      {editing && <p className="mb-2 text-amber-200">正在编辑选面，请先保存或取消，再确认其他组。</p>}
      {context && onCreateAnnotation && (
        <div className="mb-2 flex flex-wrap gap-2">
          {(context.annotation_kinds ?? [])
            .filter(kind => kind.geometry_kinds.includes('face'))
            .map(kind => (
              <button
                className="text-[10px] text-cyan-200"
                disabled={busy || editing}
                key={kind.kind}
                onClick={() => onCreateAnnotation(kind, context.revision)}
                type="button"
              >
                标记
                {(
                  { appearance: '外观', mold_side: '定／动模', forming_side: '型腔／型芯侧' } as Record<string, string>
                )[kind.kind] ?? kind.kind}
              </button>
            ))}
        </div>
      )}
      {error && (
        <p className="mb-2 break-words text-red-200" role="alert">
          {error}
        </p>
      )}
      {!context ? (
        <p className="text-slate-400">正在读取确认状态…</p>
      ) : (
        reviewBuckets(context).map(bucket => (
          <details
            className="mb-1 rounded border border-white/10 p-1.5"
            key={bucket.id}
            onToggle={event => {
              const open = event.currentTarget.open
              setOpenBuckets(previous => {
                if (previous.has(bucket.id) === open) {return previous}
                const next = new Set(previous)

                if (open) {next.add(bucket.id)}
                else {next.delete(bucket.id)}

                return next
              })
            }}
            open={openBuckets.has(bucket.id)}
          >
            <summary className="cursor-pointer text-slate-200">
              {bucket.label} · {bucket.groups.length} 组待确认
            </summary>
            {openBuckets.has(bucket.id) &&
              bucket.groups.slice(0, limits[bucket.id] ?? 50).map(group => (
                <div className="mt-2 rounded bg-black/10 p-1.5" key={group.review_group_id}>
                  <div className="flex items-center gap-1.5">
                    {group.kind !== 'local_direction' && (
                      <input
                        aria-label={`选择 ${group.target_refs[0]}`}
                        checked={selected.includes(group.review_group_id)}
                        disabled={busy || editing || group.status !== 'pending_review'}
                        onChange={event =>
                          setSelected(previous =>
                            event.target.checked
                              ? [...previous, group.review_group_id]
                              : previous.filter(id => id !== group.review_group_id)
                          )
                        }
                        type="checkbox"
                      />
                    )}
                    <span
                      className="min-w-0 flex-1 truncate text-[10px] text-slate-300"
                      title={group.target_refs.join(', ')}
                    >
                      {group.target_refs[0]}
                      {group.kind === 'surface_attribute' &&
                        ` · ${context.annotations.find(item => item.annotation_id === group.target_refs[0])?.value ?? ''}`}
                    </span>
                    {group.kind === 'ordinary_structure' &&
                      featureNeedsPartReview(
                        context.features.find(feature => group.target_refs.includes(feature.feature_id))
                      ) && (
                        <span
                          className="text-[9px] text-amber-200"
                          title="新增面尚未归入外壁、孔壁等部位，相关指标暂不计算。"
                        >
                          新增面待分区
                        </span>
                      )}
                    <span className="text-[9px] text-slate-400">
                      {group.status === 'confirmed' ? '已确认' : '待确认'}
                    </span>
                    {group.kind === 'ordinary_structure' && onEditFeature && (
                      <button
                        className="text-[10px] text-cyan-200 disabled:opacity-40"
                        disabled={busy || editing}
                        onClick={() => {
                          const feature = context.features.find(item => group.target_refs.includes(item.feature_id))

                          if (feature) {onEditFeature(feature, context)}
                        }}
                        type="button"
                      >
                        编辑选面
                      </button>
                    )}
                    {group.kind === 'undercut_scope' && onEditScope && (
                      <button
                        className="text-[10px] text-cyan-200"
                        disabled={busy || editing}
                        onClick={() => {
                          const scope = context.forming_scopes.find(
                            item => item.forming_scope_id === group.target_refs[0]
                          )

                          if (scope) {onEditScope(scope, context.revision)}
                        }}
                        type="button"
                      >
                        编辑范围
                      </button>
                    )}
                    {group.kind === 'surface_attribute' && onEditAnnotation && (
                      <button
                        className="text-[10px] text-cyan-200"
                        disabled={
                          busy ||
                          editing ||
                          context.annotations
                            .find(item => item.annotation_id === group.target_refs[0])
                            ?.geometry_refs.some(ref => ref.kind !== 'face')
                        }
                        onClick={() => {
                          const annotation = context.annotations.find(
                            item => item.annotation_id === group.target_refs[0]
                          )

                          if (annotation)
                            {onEditAnnotation(
                              annotation,
                              context.revision,
                              context.annotation_kinds?.find(kind => kind.kind === annotation.kind)?.value_schema
                                .enum ?? [annotation.value]
                            )}
                        }}
                        type="button"
                      >
                        编辑属性
                      </button>
                    )}
                    {group.kind === 'surface_attribute' &&
                      context.annotations.find(item => item.annotation_id === group.target_refs[0])?.kind ===
                        'parting_boundary' && <span className="text-[9px] text-slate-400">定位关联面（粗定位）</span>}
                    <button
                      className="text-[10px] text-cyan-200"
                      disabled={busy || editing}
                      onClick={() => onHighlight(reviewGeometry(context, group))}
                      type="button"
                    >
                      定位
                    </button>
                  </div>
                  {group.kind === 'local_direction' && group.status === 'pending_review' && (
                    <div className="mt-2">
                      {!directionUnlocked(context, group) && (
                        <p className="mb-1 text-[10px] text-amber-200">先确认该倒扣范围和局部结构。</p>
                      )}
                      <div className="flex gap-1">
                        {['X', 'Y', 'Z'].map((axis, index) => (
                          <input
                            aria-label={`${group.target_refs[0]} 方向 ${axis}`}
                            className="min-w-0 flex-1 rounded bg-black/25 px-1 py-1 text-slate-100"
                            inputMode="decimal"
                            key={axis}
                            onChange={event =>
                              setDirections(previous => {
                                const value = [...(previous[group.review_group_id] ?? ['', '', ''])]
                                value[index] = event.target.value

                                return { ...previous, [group.review_group_id]: value }
                              })
                            }
                            placeholder={axis}
                            value={directions[group.review_group_id]?.[index] ?? ''}
                          />
                        ))}
                      </div>
                      <button
                        className="mt-1 text-cyan-100 disabled:opacity-40"
                        disabled={busy || editing || !directionUnlocked(context, group)}
                        onClick={() => submitDirection(group)}
                        type="button"
                      >
                        确认此方向
                      </button>
                    </div>
                  )}
                </div>
              ))}
            {openBuckets.has(bucket.id) && bucket.groups.length > (limits[bucket.id] ?? 50) && (
              <button
                className="mt-2 text-cyan-200"
                onClick={() => setLimits(previous => ({ ...previous, [bucket.id]: (previous[bucket.id] ?? 50) + 50 }))}
                type="button"
              >
                再显示 50 组
              </button>
            )}
          </details>
        ))
      )}
      {context && !context.review_groups.some(group => group.status === 'pending_review') && (
        <p className="my-2 text-slate-400">当前没有待确认项；已确认的结构已移入下方结果。</p>
      )}
      {context &&
        context.review_groups.some(group => group.status === 'confirmed' && group.kind !== 'ordinary_structure') && (
          <details className="my-2 rounded border border-white/10 p-1.5">
            <summary className="cursor-pointer text-slate-400">已确认的范围与属性</summary>
            {context.review_groups
              .filter(group => group.status === 'confirmed' && group.kind !== 'ordinary_structure')
              .map(group => (
                <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px]" key={group.review_group_id}>
                  <span className="min-w-0 flex-1 truncate">{group.target_refs[0]}</span>
                  <button
                    disabled={editing || busy}
                    onClick={() => onHighlight(reviewGeometry(context, group))}
                    type="button"
                  >
                    定位
                  </button>
                  {group.kind === 'undercut_scope' && onEditScope && (
                    <button
                      disabled={editing || busy}
                      onClick={() => {
                        const scope = context.forming_scopes.find(
                          item => item.forming_scope_id === group.target_refs[0]
                        )

                        if (scope) {onEditScope(scope, context.revision)}
                      }}
                      type="button"
                    >
                      编辑范围
                    </button>
                  )}
                  {group.kind === 'surface_attribute' && onEditAnnotation && (
                    <button
                      disabled={
                        editing ||
                        busy ||
                        context.annotations
                          .find(item => item.annotation_id === group.target_refs[0])
                          ?.geometry_refs.some(ref => ref.kind !== 'face')
                      }
                      onClick={() => {
                        const annotation = context.annotations.find(item => item.annotation_id === group.target_refs[0])

                        if (annotation)
                          {onEditAnnotation(
                            annotation,
                            context.revision,
                            context.annotation_kinds?.find(kind => kind.kind === annotation.kind)?.value_schema
                              .enum ?? [annotation.value]
                          )}
                      }}
                      type="button"
                    >
                      编辑属性
                    </button>
                  )}
                </div>
              ))}
          </details>
        )}
      <button
        className="mt-2 w-full rounded bg-cyan-500/20 p-1.5 text-cyan-100 disabled:opacity-40"
        disabled={busy || editing || !selected.length}
        onClick={() => void submit('confirm', { review_group_ids: selected })}
        type="button"
      >
        {busy ? '提交中…' : `确认选中组（${selected.length}）`}
      </button>
    </section>
  )
}
