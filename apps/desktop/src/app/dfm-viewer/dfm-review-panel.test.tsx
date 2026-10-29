import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ReviewContext } from './dfm-review'
import { DfmReviewPanel } from './dfm-review-panel'

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
      { forming_scope_id: 'SIDE', kind: 'undercut', mode: 'local', draw_direction: null, geometry_refs: [ref] }
    ],
    review_groups: [
      { review_group_id: 'boss-1', kind: 'ordinary_structure', target_refs: ['BOSS_1'], status: 'pending_review' },
      { review_group_id: 'boss-2', kind: 'ordinary_structure', target_refs: ['BOSS_2'], status: 'pending_review' },
      { review_group_id: 'range', kind: 'undercut_scope', target_refs: ['SIDE'], status: 'confirmed' },
      { review_group_id: 'structure', kind: 'local_structure', target_refs: ['SIDE'], status: 'confirmed' },
      { review_group_id: 'direction', kind: 'local_direction', target_refs: ['SIDE'], status: 'pending_review' }
    ]
  }
}

function openBucket(label: RegExp) {
  const summary = screen.getByText(label)
  const details = summary.closest('details')!
  details.open = true
  fireEvent(details, new Event('toggle', { bubbles: true }))

  return details
}

function mount(initial: ReviewContext, next = initial) {
  const api = vi.fn(async (request: { path: string; method?: string; body?: unknown }) =>
    request.method === 'POST' ? next : initial
  )

  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api } })
  const onChanged = vi.fn()
  const onHighlight = vi.fn()
  const onEditScope = vi.fn()
  const onEditAnnotation = vi.fn()
  const onCreateAnnotation = vi.fn()

  const view = render(
    <DfmReviewPanel
      onChanged={onChanged}
      onCreateAnnotation={onCreateAnnotation}
      onEditAnnotation={onEditAnnotation}
      onEditScope={onEditScope}
      onHighlight={onHighlight}
      projectId={initial.project_id}
      revision={initial.revision}
    />
  )

  return { ...view, api, onChanged, onHighlight, onEditScope, onEditAnnotation, onCreateAnnotation }
}

describe('DFM grouped review interaction', () => {
  it('pins the reviewed snapshot while submitting a group confirmation', async () => {
    const initial = { ...context(), snapshot_id: 'discovery.snapshot.reviewed' }
    const { api } = mount(initial, { ...initial, revision: 8 })
    await screen.findByText(/带孔 Boss 柱/)
    openBucket(/带孔 Boss 柱/)
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 BOSS_1' }))
    fireEvent.click(screen.getByRole('button', { name: '确认选中组（1）' }))
    await waitFor(() => expect(api).toHaveBeenCalledTimes(2))
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
    fireEvent.click(screen.getByRole('button', { name: '标记外观' }))
    expect(onCreateAnnotation).toHaveBeenCalledWith(initial.annotation_kinds[0], initial.revision)
    const bucket = openBucket(/面属性 · 外观/)
    fireEvent.click(within(bucket).getByRole('button', { name: '编辑属性' }))
    expect(onEditAnnotation).toHaveBeenCalledWith(initial.annotations[0], initial.revision, [
      'appearance',
      'non_appearance'
    ])
    fireEvent.click(within(bucket).getByRole('button', { name: '定位' }))
    expect(onHighlight).toHaveBeenCalledWith(initial.annotations[0].geometry_refs)
    fireEvent.click(within(bucket).getByRole('checkbox', { name: '选择 appearance-1' }))
    fireEvent.click(screen.getByRole('button', { name: '确认选中组（1）' }))
    await waitFor(() =>
      expect(api).toHaveBeenLastCalledWith({
        path: '/api/dfm/projects/project-a/discovery/review/confirm',
        method: 'POST',
        body: { expected_revision: 7, review_group_ids: ['attribute-1'] }
      })
    )
  })
  it('opens the range editor with the authoritative scope and current revision', async () => {
    const initial = context()
    const { onEditScope, api } = mount(initial)
    await screen.findByText('已确认的范围与属性')
    const bucket = openBucket(/已确认的范围与属性/)
    fireEvent.click(await within(bucket).findByRole('button', { name: '编辑范围' }))
    expect(onEditScope).toHaveBeenCalledWith(initial.forming_scopes[0], initial.revision)
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
    expect(onHighlight).toHaveBeenCalledWith(initial.features[0].properties.geometry_refs)
    fireEvent.click(checkbox)
    fireEvent.click(screen.getByRole('button', { name: '确认选中组（1）' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(next))
    expect(api.mock.calls[1][0]).toEqual({
      path: '/api/dfm/projects/project-a/discovery/review/confirm',
      method: 'POST',
      body: { review_group_ids: ['boss-1'], expected_revision: 7 }
    })
    expect(screen.queryByRole('checkbox', { name: '选择 BOSS_1' })).toBeNull()
    expect((screen.getByRole('checkbox', { name: '选择 BOSS_2' }) as HTMLInputElement).disabled).toBe(false)
  })

  it('requires an explicit nonzero local direction and submits the confirmed model vector', async () => {
    const initial = context()
    const { api } = mount(initial, { ...initial, revision: 8 })
    await screen.findByText(/局部退出方向/)
    openBucket(/局部退出方向/)
    const confirm = await screen.findByRole('button', { name: '确认此方向' })
    fireEvent.click(confirm)
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '请输入三个有限数值，且方向不能全为零。')
    expect(api).toHaveBeenCalledTimes(1)

    for (const [axis, value] of [
      ['X', '1'],
      ['Y', '0'],
      ['Z', '0']
    ]) {
      fireEvent.change(screen.getByRole('textbox', { name: `SIDE 方向 ${axis}` }), { target: { value } })
    }

    fireEvent.click(confirm)
    await waitFor(() => expect(api).toHaveBeenCalledTimes(2))
    expect(api.mock.calls[1][0]).toEqual({
      path: '/api/dfm/projects/project-a/discovery/review/direction',
      method: 'POST',
      body: { forming_scope_id: 'SIDE', draw_direction: [1, 0, 0], expected_revision: 7 }
    })
  })

  it('keeps a local direction locked until its own structure is confirmed', async () => {
    const initial = context()
    initial.review_groups = initial.review_groups.map(group =>
      group.review_group_id === 'structure' ? { ...group, status: 'pending_review' } : group
    )
    mount(initial)
    await screen.findByText(/局部退出方向/)
    openBucket(/局部退出方向/)
    const button = await screen.findByRole('button', { name: '确认此方向' })
    expect((button as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('先确认该倒扣范围和局部结构。')).toBeTruthy()
  })

  it('refreshes after a version conflict and retries against the new revision', async () => {
    const initial = context()
    const { api } = mount(initial)
    await screen.findByText(/带孔 Boss 柱/)
    openBucket(/带孔 Boss 柱/)
    fireEvent.click(await screen.findByRole('checkbox', { name: '选择 BOSS_1' }))
    api.mockRejectedValueOnce(new Error('409 project changed'))
    api.mockResolvedValueOnce({ ...initial, revision: 9 })
    fireEvent.click(screen.getByRole('button', { name: '确认选中组（1）' }))
    await waitFor(() => expect(api).toHaveBeenCalledTimes(3))
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '409 project changed')
    fireEvent.click(screen.getByRole('button', { name: '确认选中组（1）' }))
    await waitFor(() => expect(api).toHaveBeenCalledTimes(4))
    expect(api.mock.calls[3][0].body).toEqual({ review_group_ids: ['boss-1'], expected_revision: 9 })
  })
})
