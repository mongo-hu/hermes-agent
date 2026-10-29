import { useCallback, useEffect, useRef, useState } from 'react'

import {
  directionUnlocked,
  parseReviewDirection,
  reviewBuckets,
  reviewGeometry,
  type ReviewContext,
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
}

export function DfmReviewPanel({
  projectId,
  revision,
  onChanged,
  onRefreshed,
  onHighlight,
  onEditScope,
  onEditAnnotation,
  onCreateAnnotation
}: DfmReviewPanelProps) {
  const [context, setContext] = useState<ReviewContext | null>(null)
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
    if (currentProject.current !== projectId || next.project_id !== projectId) return
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
      if (currentProject.current === projectId) setError(cause instanceof Error ? cause.message : String(cause))
    })
  }, [refresh, revision])

  const submit = async (path: string, body: Record<string, unknown>) => {
    if (!context || busy) return
    setBusy(true)
    setError('')
    try {
      const next = await window.hermesDesktop.api<ReviewContext>({
        path: `${base}/${path}`,
        method: 'POST',
        body: { ...body, expected_revision: context.revision, expected_snapshot_id: context.snapshot_id }
      })
      if (currentProject.current !== projectId || next.project_id !== projectId) return
      setContext(next)
      setSelected([])
      onChanged(next)
    } catch (cause) {
      if (currentProject.current !== projectId) return
      setError(cause instanceof Error ? cause.message : String(cause))
      await refresh().catch(() => undefined)
    } finally {
      if (currentProject.current === projectId) setBusy(false)
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
        <h2 className="font-semibold text-cyan-100">分组确认</h2>
        <button
          type="button"
          disabled={busy}
          onClick={() => void refresh().catch(cause => setError(String(cause)))}
          className="text-cyan-200"
        >
          刷新
        </button>
      </div>
      <p className="mb-2 text-[10px] text-slate-400">每组独立确认；局部方向未提供，不阻塞已就绪的其他组。</p>
      {context && onCreateAnnotation && (
        <div className="mb-2 flex flex-wrap gap-2">
          {(context.annotation_kinds ?? [])
            .filter(kind => kind.geometry_kinds.includes('face'))
            .map(kind => (
              <button
                type="button"
                key={kind.kind}
                disabled={busy}
                className="text-[10px] text-cyan-200"
                onClick={() => onCreateAnnotation(kind, context.revision)}
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
        <p role="alert" className="mb-2 break-words text-red-200">
          {error}
        </p>
      )}
      {!context ? (
        <p className="text-slate-400">正在读取确认状态…</p>
      ) : (
        reviewBuckets(context).map(bucket => (
          <details
            key={bucket.id}
            open={openBuckets.has(bucket.id)}
            onToggle={event => {
              const open = event.currentTarget.open
              setOpenBuckets(previous => {
                if (previous.has(bucket.id) === open) return previous
                const next = new Set(previous)
                if (open) next.add(bucket.id)
                else next.delete(bucket.id)
                return next
              })
            }}
            className="mb-1 rounded border border-white/10 p-1.5"
          >
            <summary className="cursor-pointer text-slate-200">
              {bucket.label} · {bucket.groups.filter(group => group.status === 'confirmed').length}/
              {bucket.groups.length} 已确认
            </summary>
            {openBuckets.has(bucket.id) &&
              bucket.groups.slice(0, limits[bucket.id] ?? 50).map(group => (
                <div key={group.review_group_id} className="mt-2 rounded bg-black/10 p-1.5">
                  <div className="flex items-center gap-1.5">
                    {group.kind !== 'local_direction' && (
                      <input
                        type="checkbox"
                        aria-label={`选择 ${group.target_refs[0]}`}
                        disabled={busy || group.status !== 'pending_review'}
                        checked={selected.includes(group.review_group_id)}
                        onChange={event =>
                          setSelected(previous =>
                            event.target.checked
                              ? [...previous, group.review_group_id]
                              : previous.filter(id => id !== group.review_group_id)
                          )
                        }
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
                    <span className="text-[9px] text-slate-400">
                      {group.status === 'confirmed' ? '已确认' : '待确认'}
                    </span>
                    {group.kind === 'undercut_scope' && onEditScope && (
                      <button
                        type="button"
                        disabled={busy}
                        className="text-[10px] text-cyan-200"
                        onClick={() => {
                          const scope = context.forming_scopes.find(
                            item => item.forming_scope_id === group.target_refs[0]
                          )
                          if (scope) onEditScope(scope, context.revision)
                        }}
                      >
                        编辑范围
                      </button>
                    )}
                    {group.kind === 'surface_attribute' && onEditAnnotation && (
                      <button
                        type="button"
                        disabled={
                          busy ||
                          context.annotations
                            .find(item => item.annotation_id === group.target_refs[0])
                            ?.geometry_refs.some(ref => ref.kind !== 'face')
                        }
                        className="text-[10px] text-cyan-200"
                        onClick={() => {
                          const annotation = context.annotations.find(
                            item => item.annotation_id === group.target_refs[0]
                          )
                          if (annotation)
                            onEditAnnotation(
                              annotation,
                              context.revision,
                              context.annotation_kinds?.find(kind => kind.kind === annotation.kind)?.value_schema
                                .enum ?? [annotation.value]
                            )
                        }}
                      >
                        编辑属性
                      </button>
                    )}
                    {group.kind === 'surface_attribute' &&
                      context.annotations.find(item => item.annotation_id === group.target_refs[0])?.kind ===
                        'parting_boundary' && <span className="text-[9px] text-slate-400">定位关联面（粗定位）</span>}
                    <button
                      type="button"
                      className="text-[10px] text-cyan-200"
                      onClick={() => onHighlight(reviewGeometry(context, group))}
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
                            key={axis}
                            aria-label={`${group.target_refs[0]} 方向 ${axis}`}
                            className="min-w-0 flex-1 rounded bg-black/25 px-1 py-1 text-slate-100"
                            placeholder={axis}
                            inputMode="decimal"
                            value={directions[group.review_group_id]?.[index] ?? ''}
                            onChange={event =>
                              setDirections(previous => {
                                const value = [...(previous[group.review_group_id] ?? ['', '', ''])]
                                value[index] = event.target.value
                                return { ...previous, [group.review_group_id]: value }
                              })
                            }
                          />
                        ))}
                      </div>
                      <button
                        type="button"
                        disabled={busy || !directionUnlocked(context, group)}
                        className="mt-1 text-cyan-100 disabled:opacity-40"
                        onClick={() => submitDirection(group)}
                      >
                        确认此方向
                      </button>
                    </div>
                  )}
                </div>
              ))}
            {openBuckets.has(bucket.id) && bucket.groups.length > (limits[bucket.id] ?? 50) && (
              <button
                type="button"
                className="mt-2 text-cyan-200"
                onClick={() => setLimits(previous => ({ ...previous, [bucket.id]: (previous[bucket.id] ?? 50) + 50 }))}
              >
                再显示 50 组
              </button>
            )}
          </details>
        ))
      )}
      <button
        type="button"
        disabled={busy || !selected.length}
        onClick={() => void submit('confirm', { review_group_ids: selected })}
        className="mt-2 w-full rounded bg-cyan-500/20 p-1.5 text-cyan-100 disabled:opacity-40"
      >
        {busy ? '提交中…' : `确认选中组（${selected.length}）`}
      </button>
    </section>
  )
}
