import { type ReactNode, type Ref, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react'

import {
  confirmableReviewGroupIds,
  directionUnlocked,
  featureNeedsPartReview,
  formingDirectionLabel,
  formingPlanMissing,
  removableReviewGroup,
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
import { type DfmHighlightSelection, sameHighlightSelection } from './dfm-viewer-selection'

export interface DfmReviewPanelActions {
  removeObject: (target: Omit<ReviewNavigation, 'sequence'>) => Promise<void>
}

export interface DfmScopePreparation {
  scopeId: string
  startedAt: number
  error?: string
}

/** Resolve only the removable review group, not other groups referencing the same object. */
export function removableObjectReviewGroup(
  context: ReviewContext | null,
  target: Omit<ReviewNavigation, 'sequence'>
): ReviewGroup | undefined {
  if (!context?.snapshot_id) {return undefined}

  return context.review_groups.find(group => reviewCategory(group) === target.category &&
    group.target_refs.includes(target.objectId) && removableReviewGroup(context, group))
}

interface DfmReviewPanelProps {
  projectId: string
  revision: number | null
  onChanged: (context: ReviewContext) => void
  onRefreshed?: (context: ReviewContext) => void
  onHighlight: (refs: GeometryReference[], selection?: DfmHighlightSelection, force?: boolean) => void
  highlightSelection?: DfmHighlightSelection | null
  onEditAnnotation?: (annotation: ReviewAnnotation, context: ReviewContext, values: string[]) => void
  onCreateAnnotation?: (kind: AnnotationKind, context: ReviewContext) => void
  onEditFeature?: (feature: ReviewFeature, context: ReviewContext) => void
  editing?: boolean
  editingScopeId?: string | null
  scopePreparation?: DfmScopePreparation | null
  scopeEditor?: ReactNode
  latestContext?: ReviewContext | null
  confirmedFeaturesContent?: ReactNode
  navigation?: ReviewNavigation | null
  onSelectDirection?: (scope: ReviewScope) => void
  onEditDirection?: (scope: ReviewScope, context: ReviewContext) => void
  actionsRef?: Ref<DfmReviewPanelActions>
  onBusyChange?: (busy: boolean) => void
  actionBusy?: boolean
}

export function DfmReviewPanel({
  projectId,
  revision,
  onChanged,
  onRefreshed,
  onHighlight,
  highlightSelection,
  onEditAnnotation,
  onCreateAnnotation,
  onEditFeature,
  editing = false,
  editingScopeId,
  scopePreparation,
  scopeEditor,
  latestContext,
  confirmedFeaturesContent,
  navigation,
  onSelectDirection,
  onEditDirection,
  actionsRef,
  onBusyChange,
  actionBusy = false
}: DfmReviewPanelProps) {
  const [loadedContext, setContext] = useState<ReviewContext | null>(null)
  const localContext = loadedContext?.project_id === projectId ? loadedContext : null

  const context =
    latestContext?.project_id === projectId && (!localContext || latestContext.revision >= localContext.revision)
      ? latestContext
      : localContext

  const [selected, setSelected] = useState<string[]>([])
  const [activeScopeId, setActiveScopeId] = useState<string | null>(null)
  const [preparationSeconds, setPreparationSeconds] = useState(0)
  useEffect(() => {
    if (!scopePreparation || scopePreparation.error) {return}
    const update = () => setPreparationSeconds(Math.max(0, Math.floor((Date.now() - scopePreparation.startedAt) / 1000)))
    update()
    const timer = window.setInterval(update, 1000)
    return () => window.clearInterval(timer)
  }, [scopePreparation])
  const [localBusy, setBusy] = useState(false)
  const busy = localBusy || actionBusy
  const submission = useRef<{ projectId: string } | null>(null)
  const onBusyChangeRef = useRef(onBusyChange)
  onBusyChangeRef.current = onBusyChange

  const setSubmissionBusy = useCallback((value: boolean) => {
    setBusy(value)
    onBusyChangeRef.current?.(value)
  }, [])

  const [openBuckets, setOpenBuckets] = useState<Set<string>>(new Set())
  const [limits, setLimits] = useState<Record<string, number>>({})
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [openCategories, setOpenCategories] = useState<Set<ReviewCategory>>(new Set(['structure']))
  const [bucketNavigation, setBucketNavigation] = useState<{ id: string; sequence: number } | null>(null)
  const panelRef = useRef<HTMLElement | null>(null)
  const currentProject = useRef(projectId)
  const previousHighlightBucket = useRef<string | null>(null)
  currentProject.current = projectId
  const onRefreshedRef = useRef(onRefreshed)
  onRefreshedRef.current = onRefreshed
  const base = `/api/dfm/projects/${encodeURIComponent(projectId)}/discovery/review`
  const buckets = useMemo(() => (context ? reviewBuckets(context, true) : []), [context])
  const undercutBuckets = buckets.filter(bucket => bucket.groups[0]?.kind === 'forming_scope')
  const pendingUndercuts = undercutBuckets.filter(bucket => bucket.groups[0].status === 'pending_review')
  const undercutTarget = pendingUndercuts[0] ?? undercutBuckets[0]

  const undercutCount = (pendingUndercuts.length ? pendingUndercuts : undercutBuckets)
    .reduce((count, bucket) => count + bucket.groups.length, 0)

  const editingScopeHasCard = buckets.some(bucket => bucket.groups.some(group => group.kind === 'forming_scope' &&
    group.target_refs.includes(editingScopeId ?? ''))) && context?.forming_scopes.some(scope => scope.forming_scope_id === editingScopeId)

  useEffect(() => {
    currentProject.current = projectId
    setSubmissionBusy(false)
    setSelected([])
    setActiveScopeId(null)
    setOpenBuckets(new Set())
    setLimits({})
    setOpenCategories(new Set(['structure']))
    setBucketNavigation(null)
    setNotice('')
    previousHighlightBucket.current = null

    return () => {
      currentProject.current = ''
      onBusyChangeRef.current?.(false)
    }
  }, [projectId, setSubmissionBusy])

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
      setOpenBuckets(new Set([bucket.id]))
      const position = bucket.groups.findIndex(group => group.target_refs.includes(navigation.objectId))
      setLimits(previous => ({ ...previous, [bucket.id]: Math.max(previous[bucket.id] ?? 50, position + 1) }))
    }
  }, [navigation, context, buckets])

  useEffect(() => {
    const previous = previousHighlightBucket.current

    const selectedBucket = highlightSelection?.objectId ? buckets.find(bucket => bucket.category === highlightSelection.category &&
      bucket.groups.some(group => group.target_refs.includes(highlightSelection.objectId!))) : undefined

    const current = selectedBucket?.id ?? highlightSelection?.bucketId ?? null
    previousHighlightBucket.current = current
    setOpenBuckets(open => {
      const next = new Set(open)

      if (previous && previous !== current) {next.delete(previous)}

      if (current && buckets.some(bucket => bucket.id === current)) {next.add(current)}

      return next.size === open.size && [...next].every(id => open.has(id)) ? open : next
    })

    if (highlightSelection?.category) {
      setOpenCategories(open => open.has(highlightSelection.category!) ? open : new Set([...open, highlightSelection.category!]))
    }
  }, [highlightSelection, buckets])

  useEffect(() => {
    if (!editingScopeId) {return}

    const bucket = buckets.find(item => item.category === 'forming' &&
      item.groups.some(group => group.target_refs.includes(editingScopeId)))

    if (!bucket) {return}
    setOpenCategories(previous => new Set([...previous, 'forming']))
    setOpenBuckets(previous => new Set([...previous, bucket.id]))
    const position = bucket.groups.findIndex(group => group.target_refs.includes(editingScopeId))
    setLimits(previous => ({ ...previous, [bucket.id]: Math.max(previous[bucket.id] ?? 50, position + 1) }))
  }, [editingScopeId, buckets])

  useEffect(() => {
    if (!navigation || !openCategories.has(navigation.category)) {
      return
    }

    const element = Array.from(panelRef.current?.querySelectorAll<HTMLElement>('[data-review-object]') ?? []).find(
      item => item.dataset.reviewObject === navigation.objectId
    )

    element?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [navigation, openCategories, openBuckets, limits])

  useEffect(() => {
    if (!bucketNavigation) {
      return
    }

    const element = Array.from(panelRef.current?.querySelectorAll<HTMLElement>('[data-review-bucket]') ?? [])
      .find(item => item.dataset.reviewBucket === bucketNavigation.id)

    element?.querySelector<HTMLElement>('summary')?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
  }, [bucketNavigation])

  const showUndercuts = () => {
    if (!undercutTarget) {
      return
    }

    setOpenCategories(previous => new Set([...previous, 'forming']))
    setOpenBuckets(previous => new Set([...previous, undercutTarget.id]))
    setBucketNavigation(previous => ({ id: undercutTarget.id, sequence: (previous?.sequence ?? 0) + 1 }))
    selectForming(undercutTarget.groups[0], undercutTarget.id, true)
  }

  const submit = async (path: 'confirm' | 'remove', body: Record<string, unknown>, localBatch = false) => {
    if (!context || busy || editing || submission.current?.projectId === projectId) {
      return
    }

    if (path === 'remove' && (!context.snapshot_id || !context.review_groups.some(group =>
      group.review_group_id === body.review_group_id && removableReviewGroup(context, group)))) {
      return
    }

    const pending = { projectId }
    submission.current = pending
    setSubmissionBusy(true)
    setError('')
    setNotice('')

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
      setSelected(previous => previous.filter(id => next.review_groups.some(
        group => group.review_group_id === id && group.status === 'pending_review'
      )))
      onChanged(next)

      const remaining = next.review_groups.filter(group => group.status === 'pending_review' &&
        (!localBatch || group.kind === 'forming_scope')).length

      if (localBatch) {
        const ids = new Set(body.review_group_ids as string[])
        const accepted = next.review_groups.filter(group => ids.has(group.review_group_id) && group.status === 'confirmed').length
        setNotice(`已确认 ${accepted} 个方案；剩余 ${remaining} 个方案待处理。`)
      } else {
        setNotice(`${path === 'remove' ? '已删除识别项' : '已提交本次确认'}；剩余 ${remaining} 项待处理。`)
      }
    } catch (cause) {
      if (currentProject.current !== projectId) {
        return
      }

      setError(isReviewConflict(cause) ? '审核状态已更新，已刷新列表。请核对后重新确认选中的组。' : cause instanceof Error ? cause.message : String(cause))
      await refresh().catch(() => undefined)
    } finally {
      if (submission.current === pending) {
        submission.current = null

        if (currentProject.current === projectId) {setSubmissionBusy(false)}
      }
    }
  }

  useImperativeHandle(actionsRef, () => ({
    async removeObject(target) {
      const group = removableObjectReviewGroup(context, target)

      if (group) {await submit('remove', { review_group_id: group.review_group_id })}
    }
  }))

  const pendingCount = context?.review_groups.filter(group => group.status === 'pending_review').length ?? 0
  const confirmableIds = context ? confirmableReviewGroupIds(context) : []

  const localConfirmableIds = context?.review_groups.filter(group => group.kind === 'forming_scope' &&
    confirmableIds.includes(group.review_group_id)).map(group => group.review_group_id) ?? []

  const otherPending = context?.review_groups.some(group => group.status === 'pending_review' && group.kind !== 'forming_scope')

  const selectForming = (group: ReviewGroup, bucketId: string, force = false) => {
    if (!context) {
      return
    }

    const selection = {
      category: reviewCategory(group), bucketId, objectId: group.target_refs[0]
    }

    if (group.kind === 'forming_scope') {
      const current = highlightSelection === undefined ? activeScopeId : highlightSelection?.objectId
      setActiveScopeId(!force && current === group.target_refs[0] ? null : group.target_refs[0])
    }

    onHighlight(reviewGeometry(context, group), selection, force)

    if (group.kind === 'product_forming' &&
      (force || !sameHighlightSelection(highlightSelection ?? null, selection))) {
      const scope = context.forming_scopes.find(item => item.forming_scope_id === group.target_refs[0])

      if (scope) {
        onSelectDirection?.(scope)
      }
    }
  }

  const localScopeCard = (group: ReviewGroup, bucketId: string) => {
    if (!context) {return null}
    const scope = context.forming_scopes.find(item => item.forming_scope_id === group.target_refs[0])

    if (!scope) {return <p className="text-amber-200" key={group.review_group_id}>成型方案已变化，请刷新当前审核状态。</p>}
    const id = scope.forming_scope_id
    const preparation = scopePreparation?.scopeId === id ? scopePreparation : null
    const preparing = Boolean(preparation && !preparation.error)

    const active = editingScopeId === id || (highlightSelection === undefined ? activeScopeId === id :
      highlightSelection?.category === 'forming' && highlightSelection.objectId === id)

    const missing = formingPlanMissing(scope)
    const ready = directionUnlocked(context, group)
    const mechanism = { none: '主方向', slider: '滑块', lifter: '斜顶', undetermined: '机构待确定' }[scope.mechanism_kind]
    const label = group.status === 'confirmed' ? '已确认' : ready ? '可确认' : '待补充'

    return <div className={`mt-2 rounded border p-2 ${active ? 'border-cyan-300/50 bg-cyan-500/10' : 'border-white/10 bg-black/10'}`}
      data-forming-status={label} data-review-object={id} key={group.review_group_id}>
      <div className="flex items-start gap-2">
        {group.status === 'pending_review' && <input aria-label={`选择 ${id}`} checked={selected.includes(group.review_group_id)}
          className="mt-1" disabled={busy || editing || !ready} onChange={event => setSelected(previous => event.target.checked
            ? [...previous, group.review_group_id] : previous.filter(item => item !== group.review_group_id))} type="checkbox" />}
        <button aria-expanded={active} aria-label={`${id} ${mechanism} · ${formingDirectionLabel(scope)}`}
          className="min-w-0 flex-1 text-left disabled:opacity-60" disabled={busy || editing}
          onClick={() => selectForming(group, bucketId)} type="button">
          <span className="block truncate font-medium text-slate-100">{id}</span>
          <span className="text-[10px] text-slate-400">{mechanism} · {formingDirectionLabel(scope)}</span>
        </button>
        <span className={`rounded px-1.5 py-0.5 text-[9px] ${group.status === 'confirmed' ? 'bg-cyan-400/10 text-cyan-200' : 'bg-amber-400/10 text-amber-200'}`}>{label}</span>
      </div>
      {active && (editingScopeId === id && scopeEditor ? scopeEditor : <div className="mt-2 border-t border-white/10 pt-2">
        <dl className="grid grid-cols-2 gap-x-2 gap-y-1.5 text-[10px]">
          <dt className="text-amber-200">倒扣面</dt><dd>{scope.undercut_geometry_refs.length} 个</dd>
          <dt className="text-cyan-200">成型面</dt><dd>{scope.geometry_refs.length} 个</dd>
          <dt className="text-slate-400">成型机构</dt><dd>{mechanism}</dd>
          <dt className="text-slate-400">退出方向</dt><dd>{formingDirectionLabel(scope)}</dd>
        </dl>
        {!ready && <p className="mt-2 text-[10px] text-amber-200">待补充：{missing.join('、') || '当前方案状态'}。可补充后确认，或稍后处理。</p>}
        <div className="mt-3 flex flex-wrap gap-2">
          {group.status === 'pending_review' && ready && <button className="rounded bg-cyan-500/25 px-2 py-1 text-cyan-100 disabled:opacity-40"
            disabled={busy || editing} onClick={() => void submit('confirm', { review_group_ids: [group.review_group_id] })} type="button">确认方案</button>}
          {onEditDirection && <button className="rounded border border-white/15 px-2 py-1 disabled:opacity-40" disabled={busy || editing}
            onClick={() => { selectForming(group, bucketId, true); onEditDirection(scope, context) }} type="button">
            {preparing ? `正在准备编辑…（${preparationSeconds} 秒）` : group.status === 'confirmed' ? '修改方案' : ready ? '修改' : '补充缺失信息'}
          </button>}
          {!ready && <button className="px-2 py-1 text-slate-300" disabled={busy || editing}
            onClick={() => selectForming(group, bucketId)} type="button">稍后处理</button>}
          <button className="text-[10px] text-cyan-200" disabled={busy || editing}
            onClick={() => selectForming(group, bucketId, true)} type="button">定位</button>
          {removableReviewGroup(context, group) && <button className="ml-auto text-[10px] text-rose-200 disabled:opacity-40"
            disabled={busy || editing || !context.snapshot_id} onClick={() => void submit('remove', { review_group_id: group.review_group_id })}
            title="删除这条识别结果，不改变模型形状" type="button">删除</button>}
        </div>
        {preparing && <p className="mt-2 text-[10px] text-cyan-100" role="status">
          正在准备当前方案。若几何会话已释放，需要先恢复，请稍候。
        </p>}
        {preparation?.error && <p className="mt-2 rounded border border-red-400/25 bg-red-500/10 p-2 text-[10px] text-red-100" role="alert">
          准备编辑失败：{preparation.error}
        </p>}
      </div>)}
    </div>
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
      {undercutTarget && (
        <button className="mb-2 rounded border border-cyan-300/30 px-2 py-1 text-cyan-100 disabled:opacity-40"
          disabled={busy || editing} onClick={showUndercuts} type="button">
          局部成型方案（{undercutCount} 组{pendingUndercuts.length ? '待确认' : '已确认'}）
        </button>
      )}
      {pendingCount > 0 && (
        <p className="mb-2 text-[10px] text-slate-400">待确认 · {pendingCount} 组；每组独立确认。</p>
      )}
      {editing && <p className="mb-2 text-amber-200">正在编辑，请先确认、暂存或取消修改，再处理其他组。</p>}
      {notice && <p className="mb-2 text-[10px] text-cyan-100" role="status">{notice}</p>}
      {error && (
        <p className="mb-2 break-words text-red-200" role="alert">
          {error}
        </p>
      )}
      {context && editingScopeId && scopeEditor && !editingScopeHasCard && <div className="mb-2 rounded border border-amber-300/30 p-2">
        <p className="mb-2 text-amber-200">原方案已变化，当前草稿保留。可取消修改，或重新载入最新区域。</p>
        {scopeEditor}
      </div>}
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
            <summary className="cursor-pointer font-semibold text-slate-100" onClick={event => { if (editing) {event.preventDefault()} }}>
              {category.label}
              {context.review_groups.some(group => reviewCategory(group) === category.id && group.status === 'pending_review') && (
                <span className="ml-2 text-[10px] font-normal text-amber-200">
                  {context.review_groups.filter(group => reviewCategory(group) === category.id && group.status === 'pending_review').length} 组待确认
                </span>
              )}
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
              {category.id === 'forming' && <>
                <p className="mb-2 text-[10px] text-slate-400">
                  核对倒扣面、成型范围、机构和退出方向，一次确认整个方案。
                </p>
                {pendingUndercuts.length > 0 && <button className="mb-2 w-full rounded bg-cyan-500/20 p-2 text-cyan-100 disabled:opacity-40"
                  disabled={busy || editing || !localConfirmableIds.length}
                  onClick={() => void submit('confirm', { review_group_ids: localConfirmableIds }, true)} type="button">
                  {localBusy ? '提交中…' : `接受全部可确认方案（${localConfirmableIds.length}）`}
                </button>}
              </>}
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
                      data-review-bucket={bucket.id}
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
                      <summary className="cursor-pointer text-slate-200"
                        onClick={event => {
                          if (editing) {event.preventDefault();

 return}

                          if (bucket.category !== 'structure') {return}
                          event.preventDefault()

                          if (busy || editing) {return}
                          const refs = bucket.groups.flatMap(group => reviewGeometry(context, group))
                          onHighlight(refs, { bucketId: bucket.id, category: bucket.category })
                        }}>
                        {bucket.label} · {bucket.groups.length}{' '}
                        {bucket.groups[0].status === 'confirmed' ? '组已确认' : '组待确认'}
                      </summary>
                      {openBuckets.has(bucket.id) &&
                        bucket.groups.slice(0, limits[bucket.id] ?? 50).map(group => group.kind === 'forming_scope'
                          ? localScopeCard(group, bucket.id) : (
                          <div
                            className={`mt-2 rounded border p-1.5 ${navigation?.category === category.id && group.target_refs.includes(navigation.objectId) ? 'border-amber-400/70 bg-amber-500/10' : 'border-transparent bg-black/10'}`}
                            data-review-object={group.target_refs[0]}
                            key={group.review_group_id}
                          >
                            <div className="flex flex-wrap items-center gap-1.5">
                              {group.status === 'pending_review' && (
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
                                onClick={() => selectForming(group, bucket.id)}
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
                              {removableReviewGroup(context, group) && (
                                <button className="text-[10px] text-rose-200 disabled:opacity-40"
                                  disabled={busy || editing || !context.snapshot_id}
                                  onClick={() => void submit('remove', { review_group_id: group.review_group_id })}
                                  title="删除这条识别结果，不改变模型形状"
                                  type="button">
                                  删除
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
                                onClick={() => selectForming(group, bucket.id, true)}
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
                            {group.kind === 'product_forming' &&
                              onEditDirection && (
                                <div className="mt-2">
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
                                    编辑主方向
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
        <div className="mt-2 grid gap-2">
          {otherPending && <button className="w-full rounded bg-cyan-500/20 p-1.5 text-cyan-100 disabled:opacity-40"
            disabled={busy || editing || !confirmableIds.length}
            onClick={() => void submit('confirm', { review_group_ids: confirmableIds })}
            type="button">
            {localBusy ? '提交中…' : `接受全部可确认项（${confirmableIds.length}）`}
          </button>}
        <button
          className="mt-2 w-full rounded bg-cyan-500/20 p-1.5 text-cyan-100 disabled:opacity-40"
          disabled={busy || editing || !selected.length}
          onClick={() => void submit('confirm', { review_group_ids: selected })}
          type="button"
        >
          {localBusy ? '提交中…' : `确认选中组（${selected.length}）`}
        </button>
          <p className="text-[10px] text-slate-400">仅接受当前这一批；新生成的项目及需补全机构、方向或成型面的方案继续待处理。</p>
        </div>
      )}
    </section>
  )
}
