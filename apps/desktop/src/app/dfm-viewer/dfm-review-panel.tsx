import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  directionUnlocked,
  featureNeedsPartReview,
  REVIEW_CATEGORIES,
  reviewBuckets,
  reviewCategory,
  type ReviewCategory,
  type ReviewContext,
  reviewEditTimeout,
  type ReviewFeature,
  reviewGeometry,
  type ReviewGroup,
  reviewGroupNote,
  type ReviewNavigation,
  type ReviewScope,
  reviewTargetLabel
} from './dfm-review'
import type { AnnotationKind, ReviewAnnotation } from './dfm-review'
import { isReviewConflict } from './dfm-review-edits'
import type { GeometryReference } from './dfm-viewer-geometry'

interface DfmReviewPanelProps {
  projectId: string
  revision: number | null
  onChanged: (context: ReviewContext) => void
  onRefreshed?: (context: ReviewContext) => void
  onHighlight: (refs: GeometryReference[]) => void
  onEditScope?: (scope: ReviewScope, context: ReviewContext) => void
  onEditAnnotation?: (annotation: ReviewAnnotation, context: ReviewContext, values: string[]) => void
  onCreateAnnotation?: (kind: AnnotationKind, context: ReviewContext) => void
  onEditFeature?: (feature: ReviewFeature, context: ReviewContext) => void
  editing?: boolean
  latestContext?: ReviewContext | null
  confirmedFeaturesContent?: ReactNode
  navigation?: ReviewNavigation | null
  onSelectDirection?: (scope: ReviewScope) => void
  onEditDirection?: (scope: ReviewScope, context: ReviewContext) => void
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
  latestContext,
  confirmedFeaturesContent,
  navigation,
  onSelectDirection,
  onEditDirection
}: DfmReviewPanelProps) {
  const [loadedContext, setContext] = useState<ReviewContext | null>(null)
  const localContext = loadedContext?.project_id === projectId ? loadedContext : null

  const context =
    latestContext?.project_id === projectId && (!localContext || latestContext.revision >= localContext.revision)
      ? latestContext
      : localContext

  const [selected, setSelected] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [openBuckets, setOpenBuckets] = useState<Set<string>>(new Set())
  const [limits, setLimits] = useState<Record<string, number>>({})
  const [error, setError] = useState('')
  const [openCategories, setOpenCategories] = useState<Set<ReviewCategory>>(new Set(['structure']))
  const panelRef = useRef<HTMLElement | null>(null)
  const currentProject = useRef(projectId)
  currentProject.current = projectId
  const onRefreshedRef = useRef(onRefreshed)
  onRefreshedRef.current = onRefreshed
  const base = `/api/dfm/projects/${encodeURIComponent(projectId)}/discovery/review`
  const buckets = useMemo(() => (context ? reviewBuckets(context, true) : []), [context])

  useEffect(() => {
    setBusy(false)
    setSelected([])
    setOpenBuckets(new Set())
    setLimits({})
    setOpenCategories(new Set(['structure']))
  }, [projectId])

  const refresh = useCallback(async () => {
    const next = await window.hermesDesktop.api<ReviewContext>({ path: base })

    if (currentProject.current !== projectId || next.project_id !== projectId) {
      return
    }

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
      if (currentProject.current === projectId) {
        setError(cause instanceof Error ? cause.message : String(cause))
      }
    })
  }, [refresh, revision, projectId])

  useEffect(() => {
    if (!navigation || !context) {
      return
    }

    const bucket = buckets.find(
      item =>
        item.category === navigation.category &&
        item.groups.some(group => group.target_refs.includes(navigation.objectId))
    )

    setOpenCategories(previous =>
      previous.has(navigation.category) ? previous : new Set([...previous, navigation.category])
    )

    if (bucket) {
      setOpenBuckets(previous => (previous.has(bucket.id) ? previous : new Set([...previous, bucket.id])))
      const position = bucket.groups.findIndex(group => group.target_refs.includes(navigation.objectId))
      setLimits(previous => ({ ...previous, [bucket.id]: Math.max(previous[bucket.id] ?? 50, position + 1) }))
    }
  }, [navigation, context, buckets])

  useEffect(() => {
    if (!navigation || !openCategories.has(navigation.category)) {
      return
    }

    const element = Array.from(panelRef.current?.querySelectorAll<HTMLElement>('[data-review-object]') ?? []).find(
      item => item.dataset.reviewObject === navigation.objectId
    )

    element?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [navigation, openCategories, openBuckets, limits])

  const submit = async (path: string, body: Record<string, unknown>) => {
    if (!context || busy || editing) {
      return
    }

    setBusy(true)
    setError('')

    try {
      const next = await window.hermesDesktop.api<ReviewContext>({
        path: `${base}/${path}`,
        method: 'POST',
        timeoutMs: reviewEditTimeout(context),
        body: { ...body, expected_revision: context.revision, expected_snapshot_id: context.snapshot_id }
      })

      if (currentProject.current !== projectId || next.project_id !== projectId) {
        return
      }

      setContext(next)
      setSelected([])
      onChanged(next)
    } catch (cause) {
      if (currentProject.current !== projectId) {
        return
      }

      setError(isReviewConflict(cause) ? '审核状态已更新，已刷新列表。请核对后重新确认选中的组。' : cause instanceof Error ? cause.message : String(cause))
      await refresh().catch(() => undefined)
    } finally {
      if (currentProject.current === projectId) {
        setBusy(false)
      }
    }
  }

  const pendingCount = context?.review_groups.filter(group => group.status === 'pending_review').length ?? 0

  const selectForming = (group: ReviewGroup) => {
    if (!context) {
      return
    }

    onHighlight(reviewGeometry(context, group))

    if (reviewCategory(group) === 'forming') {
      const scope = context.forming_scopes.find(item => item.forming_scope_id === group.target_refs[0])

      if (scope) {
        onSelectDirection?.(scope)
      }
    }
  }

  return (
    <section
      aria-label="DFM 分组确认"
      className="mb-3 rounded-lg border border-cyan-300/20 bg-cyan-500/[0.05] p-2 text-[11px]"
      ref={panelRef}
    >
      <div className="mb-2 flex items-center justify-between">
        <h2 className="font-semibold text-cyan-100">特征与出模审核</h2>
        <button
          className="text-cyan-200"
          disabled={busy}
          onClick={() => void refresh().catch(cause => setError(String(cause)))}
          type="button"
        >
          刷新
        </button>
      </div>
      {pendingCount > 0 && (
        <p className="mb-2 text-[10px] text-slate-400">待确认 · {pendingCount} 组；每组独立确认。</p>
      )}
      {editing && <p className="mb-2 text-amber-200">正在编辑选面，请先保存或取消，再确认其他组。</p>}
      {error && (
        <p className="mb-2 break-words text-red-200" role="alert">
          {error}
        </p>
      )}
      {!context ? (
        <p className="text-slate-400">正在读取确认状态…</p>
      ) : (
        REVIEW_CATEGORIES.map(category => (
          <details
            className="mb-2 rounded-lg border border-white/10 p-2"
            key={category.id}
            onToggle={event => {
              const open = event.currentTarget.open
              setOpenCategories(previous => {
                if (previous.has(category.id) === open) {
                  return previous
                }

                const next = new Set(previous)

                if (open) {
                  next.add(category.id)
                } else {
                  next.delete(category.id)
                }

                return next
              })
            }}
            open={openCategories.has(category.id)}
          >
            <summary className="cursor-pointer font-semibold text-slate-100">
              {category.label}
              <span className="ml-2 text-[10px] font-normal text-slate-400">
                {
                  context.review_groups.filter(
                    group => reviewCategory(group) === category.id && group.status === 'confirmed'
                  ).length
                }{' '}
                组已确认
              </span>
            </summary>
            <div className="mt-2">
              {category.id === 'annotation' && (
                <>
                  {context && onCreateAnnotation && (
                    <div className="mb-2 flex flex-wrap gap-2">
                      {(context.annotation_kinds ?? [])
                        .filter(kind => kind.geometry_kinds.includes('face'))
                        .map(kind => (
                          <button
                            className="text-[10px] text-cyan-200"
                            disabled={busy || editing}
                            key={kind.kind}
                              onClick={() => onCreateAnnotation(kind, context)}
                            type="button"
                          >
                            标记
                            {(
                              { appearance: '外观', mold_side: '定／动模', forming_side: '型腔／型芯侧' } as Record<
                                string,
                                string
                              >
                            )[kind.kind] ?? kind.kind}
                          </button>
                        ))}
                    </div>
                  )}
                </>
              )}
              {category.id === 'forming' && (
                <p className="mb-2 text-[10px] text-slate-400">
                  全局方向与各倒扣范围的局部设置。局部结构引用已有特征。
                </p>
              )}
              {buckets
                .filter(bucket => bucket.category === category.id)
                .map((bucket, index, categoryBuckets) => (
                  <div key={bucket.id}>
                    {(index === 0 || bucket.groups[0].status !== categoryBuckets[index - 1].groups[0].status) && (
                      <h3 className="mb-1 mt-2 font-medium text-slate-300">
                        {bucket.groups[0].status === 'confirmed' ? '已确认组' : '待确认组'}
                      </h3>
                    )}
                    <details
                      className="mb-1 rounded border border-white/10 p-1.5"
                      key={bucket.id}
                      onToggle={event => {
                        const open = event.currentTarget.open
                        setOpenBuckets(previous => {
                          if (previous.has(bucket.id) === open) {
                            return previous
                          }

                          const next = new Set(previous)

                          if (open) {
                            next.add(bucket.id)
                          } else {
                            next.delete(bucket.id)
                          }

                          return next
                        })
                      }}
                      open={openBuckets.has(bucket.id)}
                    >
                      <summary className="cursor-pointer text-slate-200">
                        {bucket.label} · {bucket.groups.length}{' '}
                        {bucket.groups[0].status === 'confirmed' ? '组已确认' : '组待确认'}
                      </summary>
                      {openBuckets.has(bucket.id) &&
                        bucket.groups.slice(0, limits[bucket.id] ?? 50).map(group => (
                          <div
                            className={`mt-2 rounded border p-1.5 ${navigation?.category === category.id && group.target_refs.includes(navigation.objectId) ? 'border-amber-400/70 bg-amber-500/10' : 'border-transparent bg-black/10'}`}
                            data-review-object={group.target_refs[0]}
                            key={group.review_group_id}
                          >
                            <div className="flex items-center gap-1.5">
                              {group.kind !== 'local_direction' && group.status === 'pending_review' && (
                                <input
                                  aria-label={`选择 ${group.kind === 'product_forming' ? '主出模方向' : group.target_refs[0]}`}
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
                              <button
                                className="min-w-0 flex-1 truncate text-left text-[10px] text-slate-300"
                                disabled={busy || editing}
                                onClick={() => selectForming(group)}
                                title={
                                  group.kind === 'product_forming'
                                    ? reviewTargetLabel(group)
                                    : group.target_refs.join(', ')
                                }
                                type="button"
                              >
                                {reviewTargetLabel(group)}
                                {group.kind === 'surface_attribute' &&
                                  ` · ${context.annotations.find(item => item.annotation_id === group.target_refs[0])?.value ?? ''}`}
                              </button>
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
                                    const feature = context.features.find(item =>
                                      group.target_refs.includes(item.feature_id)
                                    )

                                    if (feature) {
                                      onEditFeature(feature, context)
                                    }
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

                                    if (scope) {
                                      onEditScope(scope, context)
                                    }
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

                                    if (annotation) {
                                      onEditAnnotation(
                                        annotation,
                                        context,
                                        context.annotation_kinds?.find(kind => kind.kind === annotation.kind)
                                          ?.value_schema.enum ?? [annotation.value]
                                      )
                                    }
                                  }}
                                  type="button"
                                >
                                  编辑属性
                                </button>
                              )}
                              {group.kind === 'surface_attribute' &&
                                context.annotations.find(item => item.annotation_id === group.target_refs[0])?.kind ===
                                  'parting_boundary' && (
                                  <span className="text-[9px] text-slate-400">定位关联面（粗定位）</span>
                                )}
                              <button
                                className="text-[10px] text-cyan-200"
                                disabled={busy || editing}
                                onClick={() => selectForming(group)}
                                type="button"
                              >
                                定位
                              </button>
                            </div>
                            {reviewGroupNote(context, group) && (
                              <p className="mt-1.5 text-[10px] leading-4 text-slate-400">
                                {reviewGroupNote(context, group)}
                              </p>
                            )}
                            {(group.kind === 'local_direction' ||
                              group.kind === 'product_forming' ||
                              group.kind === 'undercut_scope') &&
                              onEditDirection && (
                                <div className="mt-2">
                                  {group.kind === 'local_direction' && !directionUnlocked(context, group) && (
                                    <p className="mb-1 text-amber-200">
                                      可先输入并暂存方向；正式确认需完成范围及局部结构审核。
                                    </p>
                                  )}
                                  <button
                                    className="text-cyan-100 disabled:opacity-40"
                                    disabled={busy || editing}
                                    onClick={() => {
                                      const scope = context.forming_scopes.find(
                                        item => item.forming_scope_id === group.target_refs[0]
                                      )

                                      if (scope) {
                                        onEditDirection(scope, context)
                                      }
                                    }}
                                    type="button"
                                  >
                                    编辑方向
                                  </button>
                                </div>
                              )}
                          </div>
                        ))}
                      {openBuckets.has(bucket.id) && bucket.groups.length > (limits[bucket.id] ?? 50) && (
                        <button
                          className="mt-2 text-cyan-200"
                          onClick={() =>
                            setLimits(previous => ({ ...previous, [bucket.id]: (previous[bucket.id] ?? 50) + 50 }))
                          }
                          type="button"
                        >
                          再显示 50 组
                        </button>
                      )}
                    </details>
                  </div>
                ))}
              {category.id === 'structure' && (
                <>
                  <h3 className="mb-1 mt-2 font-medium text-slate-300">已确认组</h3>
                  {confirmedFeaturesContent}
                </>
              )}
              {!buckets.some(bucket => bucket.category === category.id) && category.id !== 'structure' && (
                <p className="text-slate-400">暂无{category.label}项目。</p>
              )}
            </div>
          </details>
        ))
      )}
      {pendingCount > 0 && (
        <button
          className="mt-2 w-full rounded bg-cyan-500/20 p-1.5 text-cyan-100 disabled:opacity-40"
          disabled={busy || editing || !selected.length}
          onClick={() => void submit('confirm', { review_group_ids: selected })}
          type="button"
        >
          {busy ? '提交中…' : `确认选中组（${selected.length}）`}
        </button>
      )}
    </section>
  )
}
