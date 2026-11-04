import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { type ComponentProps, createRef, useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { type ReviewContext, reviewEditTimeout } from './dfm-review'
import { DfmReviewPanel, type DfmReviewPanelActions, removableObjectReviewGroup } from './dfm-review-panel'
import type { DfmHighlightSelection } from './dfm-viewer-selection'

const previousBridge = window.hermesDesktop

afterEach(() => {
  cleanup()
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: previousBridge })
})

function context(): ReviewContext {
  const ref = {
    kind: 'face' as const,
    index: 101,
    entity_id: 'face-101',
    input_sha256: 'a'.repeat(64),
    topology_snapshot_id: 'topology'
  }

  return {
    project_id: 'project-a',
    revision: 7,
    annotations: [],
    features: ['BOSS_1', 'BOSS_2'].map(feature_id => ({
      feature_id,
      kind: 'hollow_boss',
      confidence: 1,
      properties: { geometry_refs: [ref] }
    })),
    forming_scopes: [
      { forming_scope_id: 'SIDE', kind: 'undercut', mode: 'local', draw_direction: [1, 0, 0], geometry_refs: [ref],
        mechanism_kind: 'slider', undercut_geometry_refs: [ref], candidate_solutions: [], coordinate_system: 'model', direction_source: 'user_confirmed', status: 'candidate' }
    ],
    review_groups: [
      { review_group_id: 'boss-1', kind: 'ordinary_structure', target_refs: ['BOSS_1'], status: 'pending_review' },
      { review_group_id: 'boss-2', kind: 'ordinary_structure', target_refs: ['BOSS_2'], status: 'pending_review' },
      { review_group_id: 'plan', kind: 'forming_scope', target_refs: ['SIDE'], status: 'pending_review' }
    ]
  }
}

function openBucket(label: RegExp) {
  const summary = screen.getByText(label, { selector: 'summary' })
  const details = summary.closest('details')!
  let parent = details.parentElement?.closest('details')

  while (parent) {
    parent.open = true
    fireEvent(parent, new Event('toggle'))
    parent = parent.parentElement?.closest('details')
  }

  details.open = true
  fireEvent(details, new Event('toggle', { bubbles: true }))

  return details
}

function mount(initial: ReviewContext, next = initial, props: Partial<ComponentProps<typeof DfmReviewPanel>> = {}) {
  const api = vi.fn(async (request: { path: string; method?: string; body?: unknown }) =>
    request.method === 'POST' ? next : initial
  )

  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api } })
  const onChanged = vi.fn()
  const onHighlight = vi.fn()
  const onEditDirection = vi.fn()
  const onEditAnnotation = vi.fn()
  const onCreateAnnotation = vi.fn()

  const view = render(
    <DfmReviewPanel
      onChanged={onChanged}
      onCreateAnnotation={onCreateAnnotation}
      onEditAnnotation={onEditAnnotation}
      onEditDirection={onEditDirection}
      onHighlight={onHighlight}
      projectId={initial.project_id}
      revision={initial.revision}
      {...props}
    />
  )

  return { ...view, api, onChanged, onHighlight, onEditDirection, onEditAnnotation, onCreateAnnotation }
}

describe('DFM grouped review interaction', () => {
  it.each(['structure', 'annotation', 'forming'] as const)(
    'routes external %s deletion through the versioned panel action', async category => {
      const initial = { ...context(), snapshot_id: 'snapshot-current' }
      const ref = initial.features[0].properties.geometry_refs![0]
      initial.annotations.push({ annotation_id: 'APPEARANCE', kind: 'appearance', value: 'appearance',
        status: 'active', geometry_refs: [ref] })
      initial.review_groups.push({ review_group_id: 'appearance', kind: 'surface_attribute',
        target_refs: ['APPEARANCE'], status: 'pending_review' })
      const targets = { structure: 'BOSS_1', annotation: 'APPEARANCE', forming: 'SIDE' }
      const ids = { structure: 'boss-1', annotation: 'appearance', forming: 'plan' }

      const next = { ...initial, revision: 8,
        review_groups: initial.review_groups.filter(group => group.review_group_id !== ids[category]) }

      const actionsRef = createRef<DfmReviewPanelActions>()
      const { api, onChanged } = mount(initial, next, { actionsRef })
      await screen.findByText(/带孔 Boss 柱/)
      await act(() => actionsRef.current!.removeObject({ category, objectId: targets[category] }))
      expect(api.mock.calls.filter(call => call[0].method === 'POST')).toHaveLength(1)
      expect(api.mock.calls[1][0]).toMatchObject({
        path: '/api/dfm/projects/project-a/discovery/review/remove', method: 'POST',
        timeoutMs: reviewEditTimeout(initial),
        body: { review_group_id: ids[category], expected_revision: 7, expected_snapshot_id: 'snapshot-current' }
      })
      expect(onChanged).toHaveBeenCalledWith(next)
      expect(await screen.findByRole('status')).toHaveProperty('textContent', '已删除识别项；剩余 3 项待处理。')
    }
  )

  it.each(['confirm', 'remove'] as const)('shares an immediate submission lock when %s starts first', async first => {
    const initial = { ...context(), snapshot_id: 'snapshot-current' }
    const actionsRef = createRef<DfmReviewPanelActions>()
    const onBusyChange = vi.fn()
    const { api } = mount(initial, initial, { actionsRef, onBusyChange })
    const confirm = await screen.findByRole('button', { name: '接受全部可确认项（3）' })
    let complete!: (value: ReviewContext) => void
    api.mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))

    act(() => {
      if (first === 'confirm') {fireEvent.click(confirm)}
      void actionsRef.current!.removeObject({ category: 'structure', objectId: 'BOSS_1' })
      void actionsRef.current!.removeObject({ category: 'structure', objectId: 'BOSS_2' })
      fireEvent.click(confirm)
    })
    expect(api.mock.calls.filter(call => call[0].method === 'POST')).toHaveLength(1)
    expect(api.mock.calls[1][0].path).toBe(`/api/dfm/projects/project-a/discovery/review/${first}`)
    expect((confirm as HTMLButtonElement).disabled).toBe(true)
    expect(onBusyChange).toHaveBeenLastCalledWith(true)
    await act(async () => { complete(initial) })
    expect(onBusyChange).toHaveBeenLastCalledWith(false)
    expect((confirm as HTMLButtonElement).disabled).toBe(false)
  })

  it.each(['editing', 'actionBusy', 'confirmed', 'no snapshot', 'missing object', 'wrong category'])(
    'rejects external deletion for %s', async reason => {
      const initial: ReviewContext = { ...context(), snapshot_id: reason === 'no snapshot' ? undefined : 'snapshot-current' }

      if (reason === 'confirmed') {
        initial.review_groups[0].status = 'confirmed'
      }

      const actionsRef = createRef<DfmReviewPanelActions>()
      const { api } = mount(initial, initial, { actionsRef, editing: reason === 'editing', actionBusy: reason === 'actionBusy' })
      await screen.findByRole('button', { name: /接受全部可确认项/ })
      await act(() => actionsRef.current!.removeObject({
        category: reason === 'wrong category' ? 'annotation' : 'structure',
        objectId: reason === 'missing object' ? 'MISSING' : 'BOSS_1'
      }))
      expect(api.mock.calls.filter(call => call[0].method === 'POST')).toHaveLength(0)
    }
  )

  it('keeps external removal conflicts visible and refreshes before a deliberate retry', async () => {
    const initial = { ...context(), snapshot_id: 'snapshot-current' }
    const actionsRef = createRef<DfmReviewPanelActions>()
    const onRefreshed = vi.fn()
    const { api, onChanged } = mount(initial, initial, { actionsRef, onRefreshed })
    await screen.findByText(/带孔 Boss 柱/)
    const updated = { ...initial, revision: 9, snapshot_id: 'snapshot-new' }
    api.mockRejectedValueOnce(new Error('409 manifest_conflict: project changed'))
    api.mockResolvedValueOnce(updated)
    await act(() => actionsRef.current!.removeObject({ category: 'structure', objectId: 'BOSS_1' }))
    expect(onChanged).not.toHaveBeenCalled()
    expect(onRefreshed).toHaveBeenLastCalledWith(updated)
    expect(screen.getByRole('alert').textContent).toContain('审核状态已更新，已刷新列表')
    expect(api.mock.calls.filter(call => call[0].method === 'POST')).toHaveLength(1)
    api.mockResolvedValueOnce(updated)
    await act(() => actionsRef.current!.removeObject({ category: 'structure', objectId: 'BOSS_1' }))
    expect(api.mock.calls[3][0].body).toEqual({
      review_group_id: 'boss-1', expected_revision: 9, expected_snapshot_id: 'snapshot-new'
    })
  })

  it('uses the latest review context and does not delete a confirmed object through an old menu', async () => {
    const initial = { ...context(), snapshot_id: 'snapshot-current' }
    const actionsRef = createRef<DfmReviewPanelActions>()
    const { api, rerender, onChanged, onHighlight } = mount(initial, initial, { actionsRef })
    await screen.findByText(/带孔 Boss 柱/)

    const updated: ReviewContext = { ...initial, revision: 9, snapshot_id: 'snapshot-new',
      review_groups: initial.review_groups.map(group => group.review_group_id === 'boss-1'
        ? { ...group, status: 'confirmed' } : group) }

    rerender(<DfmReviewPanel actionsRef={actionsRef} latestContext={updated} onChanged={onChanged}
      onHighlight={onHighlight} projectId={initial.project_id} revision={initial.revision} />)
    expect(removableObjectReviewGroup(updated, { category: 'structure', objectId: 'BOSS_1' })).toBeUndefined()
    await act(() => actionsRef.current!.removeObject({ category: 'structure', objectId: 'BOSS_1' }))
    expect(api.mock.calls.filter(call => call[0].method === 'POST')).toHaveLength(0)
    await act(() => actionsRef.current!.removeObject({ category: 'structure', objectId: 'BOSS_2' }))
    expect(api.mock.calls[1][0].body).toEqual({
      review_group_id: 'boss-2', expected_revision: 9, expected_snapshot_id: 'snapshot-new'
    })
  })

  it('exposes undercut editing and individual confirmation through the collapsed category', async () => {
    const initial = { ...context(), snapshot_id: 'review-current' }
    initial.review_groups = initial.review_groups.map(group =>
      group.review_group_id === 'plan' ? { ...group, status: 'pending_review' } : group
    )

    const next: ReviewContext = { ...initial, revision: 8, review_groups: initial.review_groups.map(group =>
      group.review_group_id === 'plan' ? { ...group, status: 'confirmed' } : group
    ) }

    const { api, onChanged, onEditDirection } = mount(initial, next)
    const shortcut = await screen.findByRole('button', { name: '局部成型方案（1 组待确认）' })
    const forming = screen.getByText(/^出模设置/, { selector: 'summary' }).closest('details')!
    expect(forming.open).toBe(false)
    expect(within(forming).getByText('1 组待确认')).toBeTruthy()
    fireEvent.click(shortcut)
    const confirm = await screen.findByRole('button', { name: '确认方案' })
    const row = confirm.closest('[data-review-object]') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: '修改' }))
    expect(onEditDirection).toHaveBeenCalledWith(initial.forming_scopes[0], initial)
    // Reopen an explicitly collapsed category from the same shortcut.
    forming.open = false
    fireEvent(forming, new Event('toggle'))
    fireEvent.click(shortcut)
    await waitFor(() => expect(forming.open).toBe(true))
    // Confirming this range must leave another selected group unsubmitted.
    openBucket(/带孔 Boss 柱/)
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 BOSS_1' }))
    fireEvent.click(confirm)
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(next))
    expect(api.mock.calls[1][0].body).toEqual({ review_group_ids: ['plan'], expected_revision: 7, expected_snapshot_id: 'review-current' })
    expect(screen.getByRole('button', { name: '确认选中组（1）' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '局部成型方案（1 组已确认）' }))
    const confirmedBucket = screen.getByText(/局部成型方案/, { selector: 'summary' }).closest('details')!
    expect(within(confirmedBucket).getByRole('button', { name: '修改方案' })).toBeTruthy()
    expect(within(confirmedBucket).queryByRole('button', { name: '确认方案' })).toBeNull()
  })

  it('does not show an undercut shortcut when discovery has no undercut ranges', async () => {
    const initial = context()
    initial.review_groups = initial.review_groups.filter(group => group.kind !== 'forming_scope')
    mount(initial)
    await screen.findByText(/带孔 Boss 柱/)
    expect(screen.queryByRole('button', { name: /^局部成型方案/ })).toBeNull()
  })

  it('pins the reviewed snapshot while submitting a group confirmation', async () => {
    const initial = { ...context(), snapshot_id: 'discovery.snapshot.reviewed', edit_timeout_ms: 180_000 }
    const { api } = mount(initial, { ...initial, revision: 8 })
    await screen.findByText(/带孔 Boss 柱/)
    openBucket(/带孔 Boss 柱/)
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 BOSS_1' }))
    fireEvent.click(screen.getByRole('button', { name: '确认选中组（1）' }))
    await waitFor(() => expect(api).toHaveBeenCalledTimes(2))
    expect(api.mock.calls[1][0]).toMatchObject({ timeoutMs: initial.edit_timeout_ms })
    expect(api.mock.calls[1][0].body).toEqual({
      review_group_ids: ['boss-1'],
      expected_revision: 7,
      expected_snapshot_id: 'discovery.snapshot.reviewed'
    })
  })

  it('keeps surface properties independent and opens the same versioned face editor', async () => {
    const initial = context()
    initial.annotations = [
      {
        annotation_id: 'appearance-1',
        kind: 'appearance',
        value: 'appearance',
        status: 'active',
        geometry_refs: initial.features[0].properties.geometry_refs!
      }
    ]
    initial.annotation_kinds = [
      {
        kind: 'appearance',
        geometry_kinds: ['face'],
        recognition_status: 'not_implemented',
        value_schema: { enum: ['appearance', 'non_appearance'] }
      }
    ]
    initial.review_groups.push({
      review_group_id: 'attribute-1',
      kind: 'surface_attribute',
      target_refs: ['appearance-1'],
      status: 'pending_review'
    })
    const { onEditAnnotation, onCreateAnnotation, onHighlight, api } = mount(initial)
    await screen.findByText(/面属性 · 外观/)
    openBucket(/属性标注/)
    fireEvent.click(screen.getByRole('button', { name: '标记外观' }))
    expect(onCreateAnnotation).toHaveBeenCalledWith(initial.annotation_kinds[0], initial)
    const bucket = openBucket(/面属性 · 外观/)
    fireEvent.click(within(bucket).getByRole('button', { name: '编辑属性' }))
    expect(onEditAnnotation).toHaveBeenCalledWith(initial.annotations[0], initial, [
      'appearance',
      'non_appearance'
    ])
    fireEvent.click(within(bucket).getByRole('button', { name: '定位' }))
    expect(onHighlight).toHaveBeenCalledWith(initial.annotations[0].geometry_refs,
      { bucketId: 'annotation:appearance', category: 'annotation', objectId: 'appearance-1' }, true)
    fireEvent.click(within(bucket).getByRole('checkbox', { name: '选择 appearance-1' }))
    fireEvent.click(screen.getByRole('button', { name: '确认选中组（1）' }))
    await waitFor(() =>
      expect(api).toHaveBeenLastCalledWith({
        path: '/api/dfm/projects/project-a/discovery/review/confirm',
        method: 'POST',
        timeoutMs: reviewEditTimeout(initial),
        body: { expected_revision: 7, review_group_ids: ['attribute-1'] }
      })
    )
  })
  it('opens the range editor with the authoritative scope and current revision', async () => {
    const initial = context()
    const { onEditDirection, api } = mount(initial)
    await screen.findByText(/局部成型方案/, { selector: 'summary' })
    const bucket = openBucket(/局部成型方案/)
    fireEvent.click(within(bucket).getByRole('button', { name: /^SIDE / }))
    fireEvent.click(within(bucket).getByRole('button', { name: '修改' }))
    expect(onEditDirection).toHaveBeenCalledWith(initial.forming_scopes[0], initial)
    expect(api).toHaveBeenCalledTimes(1)
  })
  it('does not carry a pending request or late result into another project', async () => {
    const initial = context()
    const { api, rerender, onChanged, onHighlight } = mount(initial)
    await screen.findByText(/带孔 Boss 柱/)
    openBucket(/带孔 Boss 柱/)
    fireEvent.click(await screen.findByRole('checkbox', { name: '选择 BOSS_1' }))
    let resolve!: (value: ReviewContext) => void

    const pending = new Promise<ReviewContext>(done => {
      resolve = done
    })

    api.mockImplementationOnce(() => pending)
    fireEvent.click(screen.getByRole('button', { name: '确认选中组（1）' }))
    const next = { ...context(), project_id: 'project-b', revision: 1 }
    api.mockResolvedValue(next)
    rerender(<DfmReviewPanel onChanged={onChanged} onHighlight={onHighlight} projectId="project-b" revision={1} />)
    await waitFor(() => expect(api).toHaveBeenCalledTimes(3))
    openBucket(/带孔 Boss 柱/)
    const checkbox = await screen.findByRole('checkbox', { name: '选择 BOSS_1' })
    expect((checkbox as HTMLInputElement).disabled).toBe(false)
    fireEvent.click(checkbox)
    expect((screen.getByRole('button', { name: '确认选中组（1）' }) as HTMLButtonElement).disabled).toBe(false)
    await act(async () => {
      resolve({ ...initial, revision: 8 })
      await pending
    })
    expect(onChanged).not.toHaveBeenCalled()
    expect((screen.getByRole('checkbox', { name: '选择 BOSS_1' }) as HTMLInputElement).checked).toBe(true)
  })
  it('loads read-only, expands a category, highlights and confirms only selected instances', async () => {
    const initial = context()

    const next = {
      ...initial,
      revision: 8,
      review_groups: initial.review_groups.map(group =>
        group.review_group_id === 'boss-1' ? { ...group, status: 'confirmed' as const } : group
      )
    }

    const { api, onChanged, onHighlight } = mount(initial, next)
    await screen.findByText(/带孔 Boss 柱/)
    expect(api).toHaveBeenCalledTimes(1)
    expect(api.mock.calls[0][0].method).toBeUndefined()
    expect(screen.queryByRole('checkbox', { name: '选择 BOSS_1' })).toBeNull()
    const bucket = openBucket(/带孔 Boss 柱/)
    const checkbox = await screen.findByRole('checkbox', { name: '选择 BOSS_1' })
    fireEvent.click(within(bucket).getAllByRole('button', { name: '定位' })[0])
    expect(onHighlight).toHaveBeenCalledWith(initial.features[0].properties.geometry_refs,
      { bucketId: 'structure:hollow_boss', category: 'structure', objectId: 'BOSS_1' }, true)
    fireEvent.click(checkbox)
    fireEvent.click(screen.getByRole('button', { name: '确认选中组（1）' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(next))
    expect(api.mock.calls[1][0]).toEqual({
      path: '/api/dfm/projects/project-a/discovery/review/confirm',
      method: 'POST',
      timeoutMs: reviewEditTimeout(initial),
      body: { review_group_ids: ['boss-1'], expected_revision: 7 }
    })
    expect(screen.queryByRole('checkbox', { name: '选择 BOSS_1' })).toBeNull()
    expect((screen.getByRole('checkbox', { name: '选择 BOSS_2' }) as HTMLInputElement).disabled).toBe(false)
  })

  it('opens the plan editor independently of pending structural confirmations', async () => {
    const initial = context()
    const { api, onEditDirection } = mount(initial)
    await screen.findByText(/局部成型方案/, { selector: 'summary' })
    const bucket = openBucket(/局部成型方案/)
    fireEvent.click(within(bucket).getByRole('button', { name: /^SIDE / }))
    fireEvent.click(within(bucket).getByRole('button', { name: '修改' }))
    expect(onEditDirection).toHaveBeenCalledWith(initial.forming_scopes[0], initial)
    expect(api).toHaveBeenCalledTimes(1)
  })

  it('hides all pending controls when there are no pending groups and keeps confirmed groups visible', async () => {
    const initial = context()
    initial.review_groups = initial.review_groups.map(group => ({ ...group, status: 'confirmed' }))
    mount(initial)
    await screen.findByText(/局部成型方案/, { selector: 'summary' })
    expect(screen.queryByText('待确认组')).toBeNull()
    expect(screen.queryByText(/待确认 · 0/)).toBeNull()
    expect(screen.queryByRole('button', { name: /确认选中组/ })).toBeNull()
    expect(screen.getAllByText('已确认组').length).toBeGreaterThan(0)
    const bucket = openBucket(/局部成型方案/)
    fireEvent.click(within(bucket).getByRole('button', { name: /^SIDE / }))
    expect(within(bucket).getByRole('button', { name: '修改方案' })).toBeTruthy()
  })

  it('allows editing an incomplete plan while preventing its confirmation', async () => {
    const initial = context()
    initial.forming_scopes[0] = { ...initial.forming_scopes[0], draw_direction: null, direction_source: null, mechanism_kind: 'undetermined' }
    const { onEditDirection } = mount(initial)
    await screen.findByText(/局部成型方案/, { selector: 'summary' })
    const bucket = openBucket(/局部成型方案/)
    fireEvent.click(within(bucket).getByRole('button', { name: /^SIDE / }))
    const button = await screen.findByRole('button', { name: '补充缺失信息' })
    expect((button as HTMLButtonElement).disabled).toBe(false)
    expect(screen.getByText(/待补充：成型机构、退出方向/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: '确认方案' })).toBeNull()
    fireEvent.click(button)
    expect(onEditDirection).toHaveBeenCalledWith(initial.forming_scopes[0], initial)
  })

  it('refreshes after a version conflict and retries against the new revision', async () => {
    const initial = context()
    const { api } = mount(initial)
    await screen.findByText(/带孔 Boss 柱/)
    openBucket(/带孔 Boss 柱/)
    fireEvent.click(await screen.findByRole('checkbox', { name: '选择 BOSS_1' }))
    api.mockRejectedValueOnce(new Error('409 manifest_conflict: project changed'))
    api.mockResolvedValueOnce({ ...initial, revision: 9 })
    fireEvent.click(screen.getByRole('button', { name: '确认选中组（1）' }))
    await waitFor(() => expect(api).toHaveBeenCalledTimes(3))
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '审核状态已更新，已刷新列表。请核对后重新确认选中的组。')
    fireEvent.click(screen.getByRole('button', { name: '确认选中组（1）' }))
    await waitFor(() => expect(api).toHaveBeenCalledTimes(4))
    expect(api.mock.calls[3][0].body).toEqual({ review_group_ids: ['boss-1'], expected_revision: 9 })
  })

  it('highlights every candidate in a bucket and folds the previous bucket without clearing review checks', async () => {
    const initial = context()
    const ref = initial.features[0].properties.geometry_refs![0]
    initial.features = Array.from({ length: 102 }, (_, index) => ({
      feature_id: `BLEND_${index}`, kind: 'blend', confidence: 1,
      properties: { geometry_refs: [{ ...ref, index: index + 1, entity_id: `face-${index + 1}` }] }
    }))
    initial.features.push({ feature_id: 'HOLE', kind: 'drilled_hole', confidence: 1, properties: { geometry_refs: [ref] } })
    initial.review_groups = initial.features.map(feature => ({ review_group_id: `review-${feature.feature_id}`,
      kind: 'ordinary_structure', target_refs: [feature.feature_id], status: 'pending_review' }))
    const api = vi.fn(async () => initial)
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api } })
    const highlight = vi.fn()

    function Harness() {
      const [selection, setSelection] = useState<DfmHighlightSelection | null>(null)

      return <DfmReviewPanel highlightSelection={selection} onChanged={vi.fn()} onHighlight={(refs, target, force) => {
          highlight(refs, target, force)
          setSelection(previous => !force && previous?.bucketId === target?.bucketId && previous?.objectId === target?.objectId
            ? null : target ?? null)
        }}
        projectId={initial.project_id} revision={initial.revision} />
    }

    render(<Harness />)
    const blend = await screen.findByText(/^圆角 ·/, { selector: 'summary' })
    const hole = screen.getByText(/^孔 ·/, { selector: 'summary' })
    fireEvent.click(blend)
    expect(highlight.mock.calls[0][0]).toHaveLength(102)
    expect(screen.getAllByRole('checkbox')).toHaveLength(50)
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 BLEND_0' }))
    fireEvent.click(hole)
    expect(blend.closest('details')!.open).toBe(false)
    expect(hole.closest('details')!.open).toBe(true)
    fireEvent.click(hole)
    expect(hole.closest('details')!.open).toBe(false)
    fireEvent.click(blend)
    expect((screen.getByRole('checkbox', { name: '选择 BLEND_0' }) as HTMLInputElement).checked).toBe(true)
    expect(api).toHaveBeenCalledTimes(1)
  })

  it('accepts only the current complete batch and leaves incomplete plans and later groups pending', async () => {
    const initial = { ...context(), snapshot_id: 'snapshot-current' }
    initial.forming_scopes.push({ ...initial.forming_scopes[0], forming_scope_id: 'SIDE_INCOMPLETE',
      mechanism_kind: 'undetermined', draw_direction: null })
    initial.review_groups.push({ review_group_id: 'incomplete', kind: 'forming_scope',
      target_refs: ['SIDE_INCOMPLETE'], status: 'pending_review' })

    const next: ReviewContext = { ...initial, revision: 8, review_groups: [
      ...initial.review_groups.map(group => group.review_group_id !== 'incomplete'
        ? { ...group, status: 'confirmed' as const } : group),
      { review_group_id: 'new-structure', kind: 'ordinary_structure', target_refs: ['NEW_BOSS'], status: 'pending_review' }
    ] }

    const { api } = mount(initial, next)
    fireEvent.click(await screen.findByRole('button', { name: '接受全部可确认项（3）' }))
    await waitFor(() => expect(api).toHaveBeenCalledTimes(2))
    expect(api.mock.calls[1][0].body).toEqual({
      review_group_ids: ['boss-1', 'boss-2', 'plan'], expected_revision: 7, expected_snapshot_id: 'snapshot-current'
    })
    expect(await screen.findByRole('status')).toHaveProperty('textContent', '已提交本次确认；剩余 2 项待处理。')
    expect(screen.getByRole('button', { name: '接受全部可确认项（1）' })).toBeTruthy()
    expect(api.mock.calls.filter(call => call[0].method === 'POST')).toHaveLength(1)
  })

  it('removes only a pending candidate with pinned review identity and preserves other review checks', async () => {
    const initial = { ...context(), snapshot_id: 'snapshot-current' }
    initial.review_groups.find(group => group.review_group_id === 'plan')!.status = 'confirmed'

    const next = { ...initial, revision: 8,
      features: initial.features.filter(feature => feature.feature_id !== 'BOSS_1'),
      review_groups: initial.review_groups.filter(group => group.review_group_id !== 'boss-1') }

    const { api, onChanged } = mount(initial, next)
    await screen.findByText(/带孔 Boss 柱/)
    openBucket(/带孔 Boss 柱/)
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 BOSS_2' }))
    const row = screen.getByRole('button', { name: 'BOSS_1' }).closest('[data-review-object]') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: '删除' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(next))
    expect(api.mock.calls[1][0]).toMatchObject({
      path: '/api/dfm/projects/project-a/discovery/review/remove', method: 'POST',
      body: { review_group_id: 'boss-1', expected_revision: 7, expected_snapshot_id: 'snapshot-current' }
    })
    expect(screen.queryByRole('button', { name: 'BOSS_1' })).toBeNull()
    expect((screen.getByRole('checkbox', { name: '选择 BOSS_2' }) as HTMLInputElement).checked).toBe(true)
    const range = openBucket(/局部成型方案/)
    expect(within(range).queryByRole('button', { name: '删除' })).toBeNull()
  })

  it('blocks batch confirmation and deletion while editing without losing pending groups', async () => {
    const initial = context()
    const { rerender, onChanged, onHighlight, api } = mount(initial)
    await screen.findByText(/带孔 Boss 柱/)
    openBucket(/带孔 Boss 柱/)
    rerender(<DfmReviewPanel editing onChanged={onChanged}
      onHighlight={onHighlight} projectId={initial.project_id} revision={initial.revision} />)
    expect((screen.getByRole('button', { name: '接受全部可确认项（3）' }) as HTMLButtonElement).disabled).toBe(true)

    for (const button of screen.getAllByRole('button', { name: '删除' })) {
      expect((button as HTMLButtonElement).disabled).toBe(true)
    }

    expect(api).toHaveBeenCalledTimes(1)
  })

  it('does not restore an axis when the same scope is toggled off from an earlier bucket identity', async () => {
    const initial = context()
    const { rerender, onChanged, onHighlight } = mount(initial)
    const onSelectDirection = vi.fn()
    await screen.findByText(/局部成型方案/, { selector: 'summary' })
    rerender(<DfmReviewPanel
      highlightSelection={{ bucketId: 'forming:forming_scope:confirmed', category: 'forming', objectId: 'SIDE' }}
      onChanged={onChanged} onHighlight={onHighlight} onSelectDirection={onSelectDirection}
      projectId={initial.project_id} revision={initial.revision} />)
    const range = openBucket(/局部成型方案/)
    fireEvent.click(within(range).getByRole('button', { name: /^SIDE / }))
    expect(onSelectDirection).not.toHaveBeenCalled()
    fireEvent.click(within(range).getByRole('button', { name: '定位' }))
    expect(onSelectDirection).not.toHaveBeenCalled()
    expect(onHighlight).toHaveBeenLastCalledWith(expect.any(Array),
      { category: 'forming', bucketId: 'forming:forming_scope', objectId: 'SIDE' }, true)
  })

  it('accepts all complete local plans in one request even beyond the visible page, skipping unknowns', async () => {
    const initial = { ...context(), snapshot_id: 'snapshot-large' }
    initial.forming_scopes = Array.from({ length: 121 }, (_, index) => ({ ...initial.forming_scopes[0],
      forming_scope_id: `SIDE_${index}`, draw_direction: index === 120 ? null : [1, 0, 0] }))
    initial.review_groups = initial.forming_scopes.map((scope, index) => ({ review_group_id: `plan-${index}`,
      kind: 'forming_scope', target_refs: [scope.forming_scope_id], status: 'pending_review' }))
    const next: ReviewContext = { ...initial, revision: 8, review_groups: initial.review_groups.map((group, index) =>
      index < 120 ? { ...group, status: 'confirmed' } : group) }
    const { api } = mount(initial, next)
    fireEvent.click(await screen.findByRole('button', { name: '局部成型方案（121 组待确认）' }))
    expect(screen.getAllByRole('checkbox')).toHaveLength(50)
    expect(screen.queryByRole('button', { name: /接受全部可确认项/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '接受全部可确认方案（120）' }))
    await waitFor(() => expect(api).toHaveBeenCalledTimes(2))
    expect(api.mock.calls[1][0].body).toEqual({ review_group_ids: initial.review_groups.slice(0, 120).map(group => group.review_group_id),
      expected_revision: 7, expected_snapshot_id: 'snapshot-large' })
    expect(screen.getByRole('status').textContent).toBe('已确认 120 个方案；剩余 1 个方案待处理。')
    expect((screen.getByRole('button', { name: '接受全部可确认方案（0）' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('expands only the highlighted local plan and collapses it when the same object is toggled off', async () => {
    const initial = context()
    initial.forming_scopes.push({ ...initial.forming_scopes[0], forming_scope_id: 'SIDE_2', draw_direction: null })
    initial.review_groups.push({ review_group_id: 'plan-2', kind: 'forming_scope', target_refs: ['SIDE_2'], status: 'pending_review' })
    const api = vi.fn(async () => initial)
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api } })
    function Harness() {
      const [selection, setSelection] = useState<DfmHighlightSelection | null>(null)
      return <DfmReviewPanel highlightSelection={selection} onChanged={vi.fn()} onEditDirection={vi.fn()}
        onHighlight={(_refs, target, force) => setSelection(previous => !force && previous?.objectId === target?.objectId ? null : target ?? null)}
        projectId={initial.project_id} revision={initial.revision} />
    }
    render(<Harness />)
    fireEvent.click(await screen.findByRole('button', { name: '局部成型方案（2 组待确认）' }))
    expect(screen.getByRole('button', { name: '确认方案' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /^SIDE_2 / }))
    expect(screen.queryByRole('button', { name: '确认方案' })).toBeNull()
    expect(screen.getByRole('button', { name: '补充缺失信息' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /^SIDE_2 / }))
    expect(screen.queryByRole('button', { name: '补充缺失信息' })).toBeNull()
    expect(api).toHaveBeenCalledTimes(1)
  })

  it('keeps the shared editor in its card and prevents switching or accepting another scheme', async () => {
    const initial = context()
    initial.forming_scopes.push({ ...initial.forming_scopes[0], forming_scope_id: 'SIDE_2' })
    initial.review_groups.push({ review_group_id: 'plan-2', kind: 'forming_scope', target_refs: ['SIDE_2'], status: 'pending_review' })
    const { api } = mount(initial, initial, { editing: true, editingScopeId: 'SIDE',
      scopeEditor: <div role="region" aria-label="统一方案草稿">draft contents</div> })
    const editor = await screen.findByRole('region', { name: '统一方案草稿' })
    expect(editor.closest('[data-review-object]')?.getAttribute('data-review-object')).toBe('SIDE')
    expect(screen.queryByRole('button', { name: '修改' })).toBeNull()
    expect((screen.getByRole('button', { name: /^SIDE_2 / }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: '接受全部可确认方案（2）' }) as HTMLButtonElement).disabled).toBe(true)
    expect(api).toHaveBeenCalledTimes(1)
  })

  it('keeps a removed scheme draft visible with its cancel action after a remote refresh', async () => {
    const initial = context()
    const cancel = vi.fn()
    const scopeEditor = <div role="region" aria-label="保留的方案草稿"><input aria-label="草稿方向" defaultValue="[1,2,0]" />
      <button onClick={cancel} type="button">取消修改</button></div>
    const { api, rerender, onChanged, onHighlight } = mount(initial, initial,
      { editing: true, editingScopeId: 'SIDE', scopeEditor })
    const editor = await screen.findByRole('region', { name: '保留的方案草稿' })
    expect(editor.closest('[data-review-object]')?.getAttribute('data-review-object')).toBe('SIDE')
    const removed: ReviewContext = { ...initial, revision: 9, forming_scopes: [],
      review_groups: initial.review_groups.filter(group => group.kind !== 'forming_scope') }
    rerender(<DfmReviewPanel editing editingScopeId="SIDE" latestContext={removed} onChanged={onChanged}
      onHighlight={onHighlight} projectId={initial.project_id} revision={initial.revision} scopeEditor={scopeEditor} />)
    expect(screen.getAllByRole('region', { name: '保留的方案草稿' })).toHaveLength(1)
    expect(screen.getByText(/原方案已变化，当前草稿保留/)).toBeTruthy()
    expect((screen.getByRole('textbox', { name: '草稿方向' }) as HTMLInputElement).value).toBe('[1,2,0]')
    fireEvent.click(screen.getByRole('button', { name: '取消修改' }))
    expect(cancel).toHaveBeenCalledOnce()
    expect(api.mock.calls.filter(call => call[0].method === 'POST')).toHaveLength(0)
  })
})
