import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { Mesh, Raycaster } from 'three'
import type * as ThreeTypes from 'three'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ReviewContext } from './dfm-review'
import { DfmViewerPane } from './dfm-viewer-pane'

const rendered = vi.hoisted(() => ({ scene: null as ThreeTypes.Scene | null }))

vi.mock('three', async importOriginal => {
  const actual = await importOriginal<typeof ThreeTypes>()

  return {
    ...actual,
    WebGLRenderer: class {
      domElement = document.createElement('canvas')
      setPixelRatio() {}
      setSize() {}
      render(scene: ThreeTypes.Scene) { rendered.scene = scene }
      dispose() {}
    }
  }
})
vi.mock('three/addons/controls/TrackballControls.js', async () => {
  const { Vector3 } = await import('three')

  return {
    TrackballControls: class {
      target = new Vector3()
      handleResize() {}
      update() {}
      dispose() {}
      addEventListener() {}
      removeEventListener() {}
    }
  }
})

const previousBridge = window.hermesDesktop
const previousScroll = Element.prototype.scrollIntoView
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: previousBridge })
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: previousScroll })
  rendered.scene = null
})

function faceMaterials(): number[] {
  let materials: number[] = []
  rendered.scene?.traverse(object => {
    if (object instanceof Mesh && Array.isArray(object.material) && object.material.length === 4) {
      materials = object.geometry.groups.map((group: { materialIndex?: number }) => group.materialIndex ?? 0)
    }
  })

  return materials
}

function expand(label: string) {
  const details = screen.getByText(label).closest('details')!
  details.open = true
  fireEvent(details, new Event('toggle'))

  return details
}

async function mountObjectMenuWorkflow() {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  vi.stubGlobal('requestAnimationFrame', () => 0)
  vi.stubGlobal('cancelAnimationFrame', () => {})
  vi.stubGlobal('PointerEvent', MouseEvent)
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  const project = 'dfm_0123456789ab'

  const refs = [1, 2, 3, 4, 5].map(index => ({ kind: 'face' as const, index,
    input_sha256: 'a'.repeat(64), topology_snapshot_id: 'topology-A' }))

  let state: ReviewContext = { project_id: project, revision: 7, snapshot_id: 'discovery-A',
    annotations: [{ annotation_id: 'APPEARANCE', kind: 'appearance', value: 'appearance',
      status: 'active', geometry_refs: [refs[0]] }], forming_scopes: [],
    features: [
      { feature_id: 'BOSS_1', kind: 'hollow_boss', confidence: 1, properties: { geometry_refs: refs.slice(0, 2) } },
      { feature_id: 'BLEND_1', kind: 'blend', confidence: 1, properties: { geometry_refs: refs.slice(1, 3) } }
    ], review_groups: [
      { review_group_id: 'boss', kind: 'ordinary_structure', target_refs: ['BOSS_1'], status: 'pending_review' },
      { review_group_id: 'blend', kind: 'ordinary_structure', target_refs: ['BLEND_1'], status: 'pending_review' },
      { review_group_id: 'appearance', kind: 'surface_attribute', target_refs: ['APPEARANCE'], status: 'pending_review' }
    ] }

  const manifest = { contract_version: 'hermes.dfm.viewer/v2', project_id: project, status: 'discovery',
    project_revision: state.revision, snapshot_id: state.snapshot_id, review_groups: state.review_groups,
    features: state.features.map(feature => ({ ...feature, geometry_refs: feature.properties.geometry_refs,
      subtype: '', method: 'native', parameters: feature.properties, diagnostics: {} })),
    issues: [], issue_count: 0, scene_path: 'scene.json', topology_path: 'topology.json' }

  const scene = { schema_version: 2, render_mesh_snapshot: { triangle_count: 10 }, primitives: [{ primitive_id: 'body',
    vertices: refs.flatMap((_, i) => [[i * 2, 0, 0], [i * 2 + 1, 0, 0], [i * 2 + 1, 1, 0], [i * 2, 1, 0]]),
    triangles: refs.flatMap((_, i) => [[i * 4, i * 4 + 1, i * 4 + 2], [i * 4, i * 4 + 2, i * 4 + 3]]) }] }

  const topology = { schema_version: 2, faces: refs.map((geometry_ref, i) => ({ geometry_ref,
    triangle_refs: [i * 2, i * 2 + 1].map(triangle_id => ({ primitive_id: 'body', triangle_id })) })) }

  const api = vi.fn(async (_request: { path: string; method?: string; body?: unknown }) => structuredClone(state))
  const readJsonFile = vi.fn(async (path: string) => path.endsWith('scene.json') ? scene : path.endsWith('topology.json') ? topology : manifest)
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api, readJsonFile } })
  render(<DfmViewerPane target={{ projectId: project, manifestPath: '/project/viewer.json', status: 'discovery' }} />)
  await screen.findByText('带孔 Boss 柱 · 1 组待确认')
  const canvas = document.querySelector('canvas')!
  canvas.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, width: 400, height: 400, right: 400, bottom: 400, toJSON() {} })
  const ray = vi.spyOn(Raycaster.prototype, 'intersectObject')

  const clickCanvas = (triangle: number | null, button = 0, drag = false) => {
    ray.mockImplementation(object => object.type === 'Mesh' && triangle !== null ? ([{ faceIndex: triangle }] as never) : [])
    fireEvent.pointerDown(canvas, { button, buttons: button === 2 ? 2 : 1, clientX: 180, clientY: 180 })

    if (drag) {
      fireEvent.pointerMove(window, { button, buttons: 2, clientX: 240, clientY: 180 })
    } else {
      fireEvent.pointerUp(canvas, { button, clientX: 180, clientY: 180 })
    }
  }

  return { api, canvas, clickCanvas, readJsonFile, getState: () => state, setState: (next: ReviewContext) => { state = next } }
}

describe('candidate confirmation and feature editing', () => {
  it('requires an overlap choice, prefers an explicitly selected owner, and never treats raw model hits as background', async () => {
    const { api, clickCanvas } = await mountObjectMenuWorkflow()
    clickCanvas(2, 2)
    let menu = screen.getByRole('menu', { name: '审核对象菜单' })
    expect(within(menu).getByText('该面属于多个审核对象')).toBeTruthy()
    expect(within(menu).queryByRole('menuitem', { name: /删除识别项/ })).toBeNull()
    fireEvent.click(within(menu).getByRole('menuitem', { name: /BLEND_1/ }))
    expect(faceMaterials()).toEqual([0, 2, 2, 0, 0])
    expect(within(menu).getByText(/BLEND_1/)).toBeTruthy()
    clickCanvas(0, 2)
    menu = screen.getByRole('menu', { name: '审核对象菜单' })
    expect(within(menu).getByText('该面属于多个审核对象')).toBeTruthy()
    fireEvent.keyDown(window, { key: 'Escape' })
    clickCanvas(0)
    clickCanvas(2, 2)
    menu = screen.getByRole('menu', { name: '审核对象菜单' })
    expect(within(menu).queryByText('该面属于多个审核对象')).toBeNull()
    expect(within(menu).getByText(/BOSS_1/)).toBeTruthy()
    expect(faceMaterials()).toEqual([2, 2, 0, 0, 0])

    // Face 5 exists in the mesh but belongs to no review object. A raw hit without
    // any topology mapping likewise must not start the background view gesture.
    clickCanvas(8, 2)
    expect(screen.queryByRole('menu', { name: '审核对象菜单' })).toBeNull()
    clickCanvas(999, 2, true)
    expect(screen.queryByRole('group', { name: '视图方向菜单' })).toBeNull()
    fireEvent.pointerCancel(window)
    clickCanvas(null, 2, true)
    expect(screen.getByRole('group', { name: '视图方向菜单' })).toBeTruthy()
    fireEvent.pointerCancel(window)
    expect(api.mock.calls.filter(([request]) => request.method === 'POST')).toHaveLength(0)
  })

  it('protects the active face draft and review checkboxes from object context menus', async () => {
    const { api, canvas, clickCanvas } = await mountObjectMenuWorkflow()
    fireEvent.click(screen.getByText('带孔 Boss 柱 · 1 组待确认'))
    const row = document.querySelector('[data-review-object="BOSS_1"]') as HTMLElement
    fireEvent.click(within(row).getByRole('checkbox', { name: '选择 BOSS_1' }))
    fireEvent.click(within(row).getByRole('button', { name: '编辑选面' }))
    const editor = await screen.findByRole('region', { name: '编辑特征选面' })
    clickCanvas(8)
    expect(within(editor).getByText('已选 3 个面 · 新增 1 · 移除 0')).toBeTruthy()
    const materials = faceMaterials()
    clickCanvas(2, 2)
    expect(screen.queryByRole('menu', { name: '审核对象菜单' })).toBeNull()
    expect(screen.getByRole('region', { name: '编辑特征选面' })).toBe(editor)
    expect(within(editor).getByText('已选 3 个面 · 新增 1 · 移除 0')).toBeTruthy()
    expect(faceMaterials()).toEqual(materials)
    expect(document.querySelector('canvas')).toBe(canvas)
    fireEvent.click(within(editor).getByRole('button', { name: '取消编辑' }))
    expect((screen.getByRole('checkbox', { name: '选择 BOSS_1' }) as HTMLInputElement).checked).toBe(true)
    expect(api.mock.calls.filter(([request]) => request.method === 'POST' || request.method === 'PATCH')).toHaveLength(0)
  })

  it('closes stale object menus and deletes once against the refreshed review identity', async () => {
    const { api, clickCanvas, getState, setState } = await mountObjectMenuWorkflow()
    clickCanvas(0)
    clickCanvas(2, 2)
    expect(screen.getByRole('menu', { name: '审核对象菜单' })).toBeTruthy()
    setState({ ...getState(), revision: 9, snapshot_id: 'discovery-B' })
    fireEvent.click(screen.getByRole('button', { name: '刷新' }))
    await waitFor(() => expect(screen.queryByRole('menu', { name: '审核对象菜单' })).toBeNull())
    clickCanvas(2, 2)
    const menu = screen.getByRole('menu', { name: '审核对象菜单' })
    let complete!: (value: ReviewContext) => void
    api.mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
    fireEvent.click(within(menu).getByRole('menuitem', { name: /删除识别项/ }))
    expect(screen.queryByRole('menu', { name: '审核对象菜单' })).toBeNull()
    clickCanvas(4, 2)
    expect(screen.queryByRole('menu', { name: '审核对象菜单' })).toBeNull()
    const pending = screen.getAllByRole('button', { name: '提交中…' })
    expect(pending.every(button => (button as HTMLButtonElement).disabled)).toBe(true)
    fireEvent.click(pending[0])
    const writes = api.mock.calls.filter(([request]) => request.method === 'POST')
    expect(writes).toHaveLength(1)
    expect(writes[0][0]).toMatchObject({ path: '/api/dfm/projects/dfm_0123456789ab/discovery/review/remove',
      body: { review_group_id: 'boss', expected_revision: 9, expected_snapshot_id: 'discovery-B' } })

    const next = { ...getState(), revision: 10, snapshot_id: 'discovery-C',
      features: getState().features.filter(feature => feature.feature_id !== 'BOSS_1'),
      review_groups: getState().review_groups.filter(group => group.review_group_id !== 'boss') }

    setState(next)
    await act(async () => { complete(structuredClone(next)) })
    expect(document.querySelector('[data-review-object="BOSS_1"]')).toBeNull()
    expect(faceMaterials()).toEqual([0, 0, 0, 0, 0])
    expect(screen.getByRole('status').textContent).toContain('已删除识别项')
  })

  it('selects a whole category, toggles a single identity across faces, and only clears on a short left background click', async () => {
    vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
    vi.stubGlobal('requestAnimationFrame', () => 0)
    vi.stubGlobal('cancelAnimationFrame', () => {})
    vi.stubGlobal('PointerEvent', MouseEvent)
    Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
    const refs = [1, 2, 3, 4].map(index => ({ kind: 'face', index }))

    const features = [
      { feature_id: 'boss-1', kind: 'hollow_boss', geometry_refs: refs.slice(0, 2) },
      { feature_id: 'boss-2', kind: 'hollow_boss', geometry_refs: [refs[2]] },
      { feature_id: 'blend-1', kind: 'blend', geometry_refs: [refs[3]] }
    ].map(feature => ({ ...feature, subtype: '', method: 'native', parameters: {}, diagnostics: {} }))

    const manifest = { contract_version: 'hermes.dfm.viewer/v2', status: 'completed', features,
      issues: [], issue_count: 0, scene_path: 'scene.json', topology_path: 'topology.json' }

    const scene = { schema_version: 2, render_mesh_snapshot: { triangle_count: 8 }, primitives: [{
      primitive_id: 'body',
      vertices: refs.flatMap((_, i) => [[i * 2, 0, 0], [i * 2 + 1, 0, 0], [i * 2 + 1, 1, 0], [i * 2, 1, 0]]),
      triangles: refs.flatMap((_, i) => [[i * 4, i * 4 + 1, i * 4 + 2], [i * 4, i * 4 + 2, i * 4 + 3]])
    }] }

    const topology = { schema_version: 2, faces: refs.map((geometry_ref, i) => ({ geometry_ref,
      triangle_refs: [i * 2, i * 2 + 1].map(triangle_id => ({ primitive_id: 'body', triangle_id })) })) }

    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: {
      readJsonFile: vi.fn(async (path: string) => path.endsWith('scene.json') ? scene : path.endsWith('topology.json') ? topology : manifest)
    } })
    render(<DfmViewerPane target={{ manifestPath: '/example/viewer.json', status: 'completed' }} />)
    await screen.findByText('已确认特征 · 3 个实例')
    fireEvent.click(screen.getByRole('tab', { name: /特征点/ }))
    const canvas = document.querySelector('canvas')!
    canvas.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, width: 400, height: 400, right: 400, bottom: 400, toJSON() {} })
    const ray = vi.spyOn(Raycaster.prototype, 'intersectObject')

    const clickCanvas = (triangle: number | null, button = 0, distance = 0) => {
      ray.mockImplementation(object => object.type === 'Mesh' && triangle !== null ? ([{ faceIndex: triangle }] as never) : [])
      fireEvent.pointerDown(canvas, { button, clientX: 100, clientY: 100 })
      fireEvent.pointerUp(canvas, { button, clientX: 100 + distance, clientY: 100 })
    }

    const bossTitle = screen.getByText('带孔 BOSS 柱 · 2 个实例')
    const blendTitle = screen.getByText('圆角链 · 1 个实例')
    fireEvent.click(bossTitle)
    expect(faceMaterials()).toEqual([2, 2, 2, 0])
    expect(bossTitle.closest('details')?.open).toBe(true)
    clickCanvas(0)
    expect(faceMaterials()).toEqual([2, 2, 0, 0])
    clickCanvas(2)
    expect(faceMaterials()).toEqual([0, 0, 0, 0])
    expect(bossTitle.closest('details')?.open).toBe(false)
    fireEvent.click(bossTitle)
    fireEvent.click(bossTitle)
    expect(faceMaterials()).toEqual([0, 0, 0, 0])
    expect(bossTitle.closest('details')?.open).toBe(false)
    fireEvent.click(bossTitle)
    fireEvent.click(blendTitle)
    expect(faceMaterials()).toEqual([0, 0, 0, 2])
    expect(bossTitle.closest('details')?.open).toBe(false)
    const blendRow = document.querySelector('[data-review-object="blend-1"]') as HTMLElement
    fireEvent.click(within(blendRow).getByRole('button'))
    fireEvent.click(within(blendRow).getByRole('button'))
    expect(faceMaterials()).toEqual([0, 0, 0, 0])
    expect(blendTitle.closest('details')?.open).toBe(false)
    clickCanvas(0)
    clickCanvas(null, 1)
    clickCanvas(null, 2)
    clickCanvas(null, 0, 5)
    expect(faceMaterials()).toEqual([2, 2, 0, 0])
    clickCanvas(null, 0, 4)
    expect(faceMaterials()).toEqual([0, 0, 0, 0])
    expect(bossTitle.closest('details')?.open).toBe(false)
    expect(screen.getByRole('tab', { name: /特征点/ })).toBeTruthy()
  })

  it.each(['removed', 'changed'] as const)('preserves the edit baseline and draft when another window has %s the feature', async change => {
    vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
    vi.stubGlobal('requestAnimationFrame', () => 0)
    vi.stubGlobal('cancelAnimationFrame', () => {})
    vi.stubGlobal('PointerEvent', MouseEvent)
    Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
    const project = 'dfm_0123456789ab'
    const refs = [1, 2, 3].map(index => ({ kind: 'face' as const, index, input_sha256: 'a'.repeat(64), topology_snapshot_id: 'topology-A' }))

    let state: ReviewContext = { project_id: project, revision: 7, snapshot_id: 'discovery-A', annotations: [], forming_scopes: [],
      features: [
        { feature_id: 'BOSS_1', kind: 'hollow_boss', confidence: 1, properties: { geometry_refs: [refs[0]] } },
        { feature_id: 'BLEND_1', kind: 'blend', confidence: 1, properties: { geometry_refs: [refs[1]] } }
      ], review_groups: [
        { review_group_id: 'boss', kind: 'ordinary_structure', target_refs: ['BOSS_1'], status: 'pending_review' },
        { review_group_id: 'blend', kind: 'ordinary_structure', target_refs: ['BLEND_1'], status: 'pending_review' }
      ] }

    const manifest = { contract_version: 'hermes.dfm.viewer/v2', project_id: project, status: 'discovery',
      project_revision: state.revision, snapshot_id: state.snapshot_id, review_groups: state.review_groups,
      features: state.features.map(feature => ({ ...feature, geometry_refs: feature.properties.geometry_refs,
        subtype: '', method: 'native', parameters: feature.properties, diagnostics: {} })),
      issues: [], issue_count: 0, scene_path: 'scene.json', topology_path: 'topology.json' }

    const scene = { schema_version: 2, render_mesh_snapshot: { triangle_count: 6 }, primitives: [{ primitive_id: 'body',
      vertices: refs.flatMap((_, i) => [[i * 2, 0, 0], [i * 2 + 1, 0, 0], [i * 2 + 1, 1, 0], [i * 2, 1, 0]]),
      triangles: refs.flatMap((_, i) => [[i * 4, i * 4 + 1, i * 4 + 2], [i * 4, i * 4 + 2, i * 4 + 3]]) }] }

    const topology = { schema_version: 2, faces: refs.map((geometry_ref, i) => ({ geometry_ref,
      triangle_refs: [i * 2, i * 2 + 1].map(triangle_id => ({ primitive_id: 'body', triangle_id })) })) }

    const api = vi.fn(async (_request: { path: string; method?: string }) => structuredClone(state))
    const readJsonFile = vi.fn(async (path: string) => path.endsWith('scene.json') ? scene : path.endsWith('topology.json') ? topology : manifest)
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api, readJsonFile } })
    render(<DfmViewerPane target={{ projectId: project, manifestPath: '/project/viewer.json', status: 'discovery' }} />)
    fireEvent.click(await screen.findByText('带孔 Boss 柱 · 1 组待确认'))
    const row = document.querySelector('[data-review-object="BOSS_1"]') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: '编辑选面' }))
    const editor = await screen.findByRole('region', { name: '编辑特征选面' })
    const canvas = document.querySelector('canvas')!
    canvas.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, width: 400, height: 400, right: 400, bottom: 400, toJSON() {} })
    vi.spyOn(Raycaster.prototype, 'intersectObject').mockImplementation(object => object.type === 'Mesh' ? ([{ faceIndex: 4 }] as never) : [])
    fireEvent.pointerDown(canvas, { button: 0, clientX: 100, clientY: 100 })
    fireEvent.pointerUp(canvas, { button: 0, clientX: 100, clientY: 100 })
    expect(within(editor).getByText('已选 2 个面 · 新增 1 · 移除 0')).toBeTruthy()
    const readsBeforeRefresh = readJsonFile.mock.calls.length

    // Another window changes the persisted target while this window owns an unsaved draft.
    state = { ...state, revision: state.revision + 1, snapshot_id: 'discovery-B',
      features: change === 'removed' ? state.features.filter(feature => feature.feature_id !== 'BOSS_1') : state.features.map(feature =>
        feature.feature_id === 'BOSS_1' ? { ...feature, properties: { geometry_refs: refs.slice(0, 2) } } : feature),
      review_groups: change === 'removed' ? state.review_groups.filter(group => group.review_group_id !== 'boss') : state.review_groups }
    fireEvent.click(screen.getByRole('button', { name: '刷新' }))
    await screen.findByText('当前区域已修改或移除，草稿仍保留。请取消编辑，或重新载入最新区域。')
    expect(screen.getByRole('region', { name: '编辑特征选面' })).toBe(editor)
    expect(within(editor).getByText('编辑带孔 BOSS 柱 · BOSS_1')).toBeTruthy()
    expect(within(editor).getByText('已选 2 个面 · 新增 1 · 移除 0')).toBeTruthy()
    expect(document.querySelector('canvas')).toBe(canvas)
    expect(readJsonFile.mock.calls).toHaveLength(readsBeforeRefresh)
    const save = within(editor).getByRole('button', { name: '保存选面' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    fireEvent.click(save)
    expect(api.mock.calls.filter(([request]) => request.method === 'PATCH')).toHaveLength(0)
    fireEvent.click(within(editor).getByRole('button', { name: '恢复原选面' }))
    expect(within(editor).getByText('已选 1 个面 · 新增 0 · 移除 0')).toBeTruthy()

    if (change === 'removed') {
      expect(document.querySelector('[data-review-object="BOSS_1"]')).toBeNull()
      fireEvent.click(within(editor).getByRole('button', { name: '取消编辑' }))
      await waitFor(() => expect(screen.queryByRole('region', { name: '编辑特征选面' })).toBeNull())
      expect(screen.queryByRole('button', { name: '重新载入最新区域' })).toBeNull()
    } else {
      fireEvent.click(screen.getByRole('button', { name: '重新载入最新区域' }))
      await waitFor(() => expect(within(editor).getByText('已选 2 个面 · 新增 0 · 移除 0')).toBeTruthy())
      expect(save.disabled).toBe(false)
      expect(screen.queryByRole('button', { name: '重新载入最新区域' })).toBeNull()
    }
  })

  it.each(['discovery', 'completed'] as const)(
    'moves confirmed results once, preserves a failed edit, and returns a saved revision to pending (%s)',
    async initialStatus => {
      vi.stubGlobal(
        'ResizeObserver',
        class {
          observe() {}
          disconnect() {}
        }
      )
      vi.stubGlobal('requestAnimationFrame', () => 0)
      vi.stubGlobal('cancelAnimationFrame', () => {})
      vi.stubGlobal('PointerEvent', MouseEvent)
      Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
      const project = 'dfm_0123456789ab'

      const refs = [1, 2, 3, 4, 5].map(index => ({
        kind: 'face' as const,
        index,
        entity_id: `face-${index}`,
        input_sha256: 'a'.repeat(64),
        topology_snapshot_id: 'topology-A'
      }))

      let state: ReviewContext = {
        project_id: project,
        revision: 7,
        snapshot_id: 'discovery-A',
        edit_timeout_ms: 180_000,
        annotations: [
          {
            annotation_id: 'APPEARANCE',
            kind: 'appearance',
            value: 'appearance',
            status: 'active',
            geometry_refs: [refs[0], refs[2]]
          }
        ],
        forming_scopes: [
          {
            forming_scope_id: 'MAIN',
            kind: 'main_forming',
            mode: 'main',
            mechanism_kind: 'none', undercut_geometry_refs: [], candidate_solutions: [], coordinate_system: 'model', direction_source: 'user_confirmed',
            draw_direction: [0, 0, 1],
            geometry_refs: refs
          },
          { forming_scope_id: 'SIDE', kind: 'undercut', mode: 'local', draw_direction: null, geometry_refs: [refs[3]],
            mechanism_kind: 'undetermined', undercut_geometry_refs: [refs[3]], candidate_solutions: [], coordinate_system: 'model', direction_source: null }
        ],
        features: [
          { feature_id: 'BOSS_1', kind: 'hollow_boss', confidence: 1, properties: { geometry_refs: [refs[0]] } },
          { feature_id: 'BLEND_1', kind: 'blend', confidence: 1, properties: { geometry_refs: [refs[1]] } }
        ],
        review_groups: [
          {
            review_group_id: 'attribute',
            kind: 'surface_attribute',
            target_refs: ['APPEARANCE'],
            status: 'pending_review'
          },
          { review_group_id: 'main', kind: 'product_forming', target_refs: ['MAIN'], status: 'confirmed' },
          { review_group_id: 'side', kind: 'forming_scope', target_refs: ['SIDE'], status: 'pending_review' },
          { review_group_id: 'boss', kind: 'ordinary_structure', target_refs: ['BOSS_1'], status: 'pending_review' },
          { review_group_id: 'blend', kind: 'ordinary_structure', target_refs: ['BLEND_1'], status: 'pending_review' }
        ]
      }

      const manifest = {
        contract_version: 'hermes.dfm.viewer/v2',
        status: initialStatus,
        project_id: project,
        snapshot_id: state.snapshot_id,
        project_revision: 7,
        review_status: 'pending',
        review_groups: state.review_groups,
        scope_id: 'injection',
        verification_level: 'experimental',
        scene_path: 'scene.json',
        topology_path: 'topology.json',
        issues:
          initialStatus === 'completed'
            ? [
                {
                  evaluation_id: 'existing-issue',
                  metric_id: 'wall_thickness',
                  title: 'Existing issue',
                  actual: 3,
                  expected: 2,
                  operator: '<=',
                  geometry_refs: refs
                }
              ]
            : [],
        issue_count: initialStatus === 'completed' ? 1 : 0,
        features: state.features.map(feature => ({
          ...feature,
          subtype: '',
          method: 'native',
          geometry_refs: feature.properties.geometry_refs,
          parameters: {},
          diagnostics: {}
        }))
      }

      const scene = {
        schema_version: 2,
        render_mesh_snapshot: { render_mesh_snapshot_id: 'mesh-A', triangle_count: 10 },
        primitives: [
          {
            primitive_id: 'body',
            vertices: refs.flatMap((_, i) => [
              [i * 2, 0, 0],
              [i * 2 + 1, 0, 0],
              [i * 2 + 1, 1, 0],
              [i * 2, 1, 0]
            ]),
            triangles: refs.flatMap((_, i) => [
              [i * 4, i * 4 + 1, i * 4 + 2],
              [i * 4, i * 4 + 2, i * 4 + 3]
            ])
          }
        ]
      }

      const topology = {
        schema_version: 2,
        faces: refs.map((geometry_ref, i) => ({
          geometry_ref,
          triangle_refs: [i * 2, i * 2 + 1].map(triangle_id => ({ primitive_id: 'body', triangle_id }))
        }))
      }

      let failedOnce = false
      let directionFailedOnce = false
      let directionRacedOnce = false
      let delayedRefresh: Promise<ReviewContext> | null = null

      const api = vi.fn(async (request: { path: string; method?: string; body?: Record<string, unknown>; timeoutMs?: number }) => {
        if (!request.method && request.path.endsWith('/review') && delayedRefresh) {
          const response = delayedRefresh
          delayedRefresh = null

          return response
        }

        if (request.path.endsWith('/remove')) {
          state = { ...state, revision: state.revision + 1,
            features: state.features.filter(feature => feature.feature_id !== 'BLEND_1'),
            review_groups: state.review_groups.filter(group => group.review_group_id !== 'blend') }

          return { ...structuredClone(state), viewer_manifest: '/project/discovery-viewer.json' }
        }

        if (request.path.endsWith('/prepare')) {
          expect(request.timeoutMs).toBe(state.edit_timeout_ms)

          return structuredClone(state)
        }

        if (
          request.path.endsWith('/direction') &&
          request.method === 'POST' &&
          request.body?.forming_scope_id === 'SIDE'
        ) {
          expect(request.timeoutMs).toBe(state.edit_timeout_ms)
          state = {
            ...state,
            revision: state.revision + 1,
            snapshot_id: 'discovery-C',
            forming_scopes: state.forming_scopes.map(scope =>
              scope.forming_scope_id === 'SIDE'
                ? { ...scope, draw_direction: [1, 0, 0], status: 'candidate' as const }
                : scope
            )
          }

          return {
            ...structuredClone(state),
            viewer_manifest: '/project/discovery-viewer.json',
            direction_status: 'forming_direction_staged'
          }
        }

        if (request.path.endsWith('/direction') && request.method === 'POST') {
          expect(request.timeoutMs).toBe(state.edit_timeout_ms)

          if (!directionFailedOnce) {
            directionFailedOnce = true
            throw new Error('Direction save failed')
          }

          if (!directionRacedOnce) {
            directionRacedOnce = true
            state = { ...state, revision: state.revision + 1, snapshot_id: 'discovery-raced' }
            throw new Error('409 geometry_edit_conflict: The reviewed snapshot changed')
          }

          state = {
            ...state,
            revision: state.revision + 1,
            snapshot_id: 'discovery-D',
            forming_scopes: state.forming_scopes.map(scope =>
              scope.mode === 'main' ? { ...scope, draw_direction: [1, 0, 0] } : scope
            )
          }

          return { ...structuredClone(state), viewer_manifest: '/project/discovery-viewer.json' }
        }

        if (request.method === 'POST') {
          state = {
            ...state,
            revision: state.revision + 1,
            review_groups: state.review_groups.map(group =>
              group.review_group_id === 'boss' ? { ...group, status: 'confirmed' as const } : group
            )
          }
        } else if (request.method === 'PATCH') {
          expect(request.timeoutMs).toBe(state.edit_timeout_ms)

          if (!failedOnce) {
            failedOnce = true
            throw new Error('保存失败，请重试')
          }

          state = {
            ...state,
            revision: state.revision + 1,
            snapshot_id: 'discovery-B',
            features: state.features.map(feature =>
              feature.feature_id === 'BOSS_1'
                ? { ...feature, properties: { geometry_refs: refs.slice(0, 2) } }
                : feature
            ),
            review_groups: [
              ...state.review_groups.map(group =>
                group.review_group_id === 'boss' ? { ...group, status: 'stale' as const } : group
              ),
              {
                review_group_id: 'boss-revised',
                kind: 'ordinary_structure',
                target_refs: ['BOSS_1'],
                status: 'pending_review'
              }
            ]
          }

          return {
            revision: state.revision,
            discovery_snapshot_id: state.snapshot_id,
            geometry_refs: refs.slice(0, 2),
            viewer_manifest: '/project/discovery-viewer.json'
          }
        }

        return structuredClone(state)
      })

      const readJsonFile = vi.fn(async (path: string) =>
        path.endsWith('scene.json')
          ? scene
          : path.endsWith('topology.json')
            ? topology
            : {
                ...manifest,
                status: path.endsWith('discovery-viewer.json') ? 'discovery' : initialStatus,
                issues: path.endsWith('discovery-viewer.json') ? [] : manifest.issues,
                issue_count: path.endsWith('discovery-viewer.json') ? 0 : manifest.issue_count,
                project_revision: state.revision,
                snapshot_id: state.snapshot_id,
                review_groups: state.review_groups,
                features: state.features.map(feature => ({
                  ...feature,
                  subtype: '',
                  method: 'native',
                  geometry_refs: feature.properties.geometry_refs,
                  parameters: {},
                  diagnostics: {}
                }))
              }
      )

      Object.defineProperty(window, 'hermesDesktop', {
        configurable: true,
        value: {
          api,
          readJsonFile
        }
      })

      const view = render(
        <DfmViewerPane target={{ projectId: project, manifestPath: '/project/viewer.json', status: initialStatus }} />
      )

      await screen.findByText('已确认特征 · 0 个实例')
      let canvas = document.querySelector('canvas')!
      canvas.getBoundingClientRect = () => ({
        x: 0,
        y: 0,
        left: 0,
        top: 0,
        width: 400,
        height: 400,
        right: 400,
        bottom: 400,
        toJSON() {}
      })
      const ray = vi.spyOn(Raycaster.prototype, 'intersectObject')
      const canvasRect = canvas.getBoundingClientRect

      const pick = (triangle: number) => {
        canvas = document.querySelector('canvas')!
        canvas.getBoundingClientRect = canvasRect
        ray.mockImplementation(object => (object.type === 'Mesh' ? ([{ faceIndex: triangle }] as never) : []))
        fireEvent.pointerDown(canvas, { button: 0, clientX: 100, clientY: 100 })
        fireEvent.pointerUp(canvas, { button: 0, clientX: 100, clientY: 100 })
      }

      pick(0)
      await screen.findByRole('checkbox', { name: '选择 BOSS_1' })
      const major = screen.getByText('结构特征', { exact: false, selector: 'summary' }).closest('details')!
      pick(0)
      await waitFor(() => expect(document.querySelector<HTMLDetailsElement>('[data-review-bucket="structure:hollow_boss"]')?.open).toBe(false))
      expect(faceMaterials()).toEqual([0, 0, 0, 0, 0])
      expect(major.open).toBe(true)
      pick(0)
      await screen.findByRole('checkbox', { name: '选择 BOSS_1' })
      fireEvent.click(screen.getByRole('checkbox', { name: '选择 BOSS_1' }))
      ray.mockReturnValue([])
      fireEvent.pointerDown(canvas, { button: 0, clientX: 100, clientY: 100 })
      fireEvent.pointerUp(canvas, { button: 0, clientX: 102, clientY: 100 })
      expect(faceMaterials()).toEqual([0, 0, 0, 0, 0])
      pick(0)
      expect((await screen.findByRole('checkbox', { name: '选择 BOSS_1' }) as HTMLInputElement).checked).toBe(true)
      fireEvent.click(screen.getByRole('checkbox', { name: '选择 BOSS_1' }))
      const staleContext = structuredClone(state)
      let finishStaleRefresh!: (value: ReviewContext) => void
      delayedRefresh = new Promise(resolve => { finishStaleRefresh = resolve })
      fireEvent.click(screen.getByRole('button', { name: '刷新' }))
      pick(2)
      const blendRow = document.querySelector('[data-review-object="BLEND_1"]') as HTMLElement
      const originalCanvas = canvas
      const readsBeforeRemoval = readJsonFile.mock.calls.length
      fireEvent.click(within(blendRow).getByRole('button', { name: '删除' }))
      await waitFor(() => expect(state.features.some(feature => feature.feature_id === 'BLEND_1')).toBe(false))
      await waitFor(() => expect(screen.queryByText('Existing issue')).toBeNull())
      expect(document.querySelector('canvas')).toBe(originalCanvas)
      expect(readJsonFile.mock.calls).toHaveLength(readsBeforeRemoval)
      expect(faceMaterials()).toEqual([0, 0, 0, 0, 0])
      pick(4)
      await screen.findByRole('checkbox', { name: '选择 APPEARANCE' })
      expect(document.querySelector('[data-review-object="APPEARANCE"]')?.className).toContain('border-amber-400')
      expect(faceMaterials()).toEqual([2, 0, 2, 0, 0])
      await act(async () => { finishStaleRefresh(staleContext) })
      expect(faceMaterials()).toEqual([2, 0, 2, 0, 0])
      expect(document.querySelector('canvas')).toBe(originalCanvas)
      pick(6)
      await screen.findByRole('checkbox', { name: '选择 SIDE' })
      expect(document.querySelector('[data-review-object="SIDE"]')?.className).toContain('border-amber-400')
      fireEvent.click(
        within(document.querySelector('[data-review-object="SIDE"]') as HTMLElement).getByRole('button', {
          name: '编辑成型面'
        })
      )
      await screen.findByText(/编辑成型面 SIDE/)
      fireEvent.click(within(screen.getByRole('region', { name: '编辑成型面' })).getByRole('button', { name: '编辑机构与方向' }))
      const stagedEditor = await screen.findByRole('region', { name: '编辑出模方向' })
      fireEvent.change(within(stagedEditor).getByRole('textbox', { name: '方向向量' }), {
        target: { value: '[1,0,0]' }
      })
      fireEvent.click(within(stagedEditor).getByRole('button', { name: '保存方案草稿' }))
      await waitFor(() => expect(screen.queryByRole('region', { name: '编辑出模方向' })).toBeNull())
      expect(state.review_groups.find(group => group.review_group_id === 'side')?.status).toBe('pending_review')
      await waitFor(() => expect(document.querySelector('canvas')).not.toBeNull())
      pick(8)
      await screen.findByText('整件主出模设置')
      expect(document.querySelector('[data-review-object="MAIN"]')?.className).toContain('border-amber-400')
      fireEvent.click(
        within(document.querySelector('[data-review-object="MAIN"]') as HTMLElement).getByRole('button', {
          name: '编辑主方向'
        })
      )
      const directionEditor = await screen.findByRole('region', { name: '编辑出模方向' })
      fireEvent.change(within(directionEditor).getByRole('textbox', { name: '方向向量' }), {
        target: { value: '[1,0,0]' }
      })
      expect(document.querySelector('[data-direction-vector]')?.getAttribute('data-direction-vector')).toBe('[1,0,0]')
      state = { ...state, revision: state.revision + 1, snapshot_id: 'unrelated-new-review' }
      const mainPins = { expected_revision: state.revision, expected_snapshot_id: state.snapshot_id }
      fireEvent.click(within(directionEditor).getByRole('button', { name: '保存主方向' }))
      await screen.findByText('Direction save failed')
      expect((within(directionEditor).getByRole('textbox', { name: '方向向量' }) as HTMLInputElement).value).toBe(
        '[1,0,0]'
      )
      state = {
        ...state,
        revision: state.revision + 1,
        snapshot_id: 'changed-target',
        forming_scopes: state.forming_scopes.map(scope =>
          scope.mode === 'main' ? { ...scope, draw_direction: [0, 1, 0] } : scope
        )
      }
      fireEvent.click(within(directionEditor).getByRole('button', { name: '保存主方向' }))
      await screen.findByRole('button', { name: '重新载入最新区域' })
      expect((within(directionEditor).getByRole('textbox', { name: '方向向量' }) as HTMLInputElement).value).toBe(
        '[1,0,0]'
      )
      expect((within(directionEditor).getByRole('button', { name: '保存主方向' }) as HTMLButtonElement).disabled).toBe(
        true
      )
      expect(
        api.mock.calls.filter(
          ([request]) => request.path.endsWith('/direction') && request.body?.forming_scope_id === 'MAIN'
        )
      ).toHaveLength(1)
      fireEvent.click(screen.getByRole('button', { name: '重新载入最新区域' }))
      await waitFor(() =>
        expect((within(directionEditor).getByRole('textbox', { name: '方向向量' }) as HTMLInputElement).value).toBe(
          '[0,1,0]'
        )
      )
      fireEvent.change(within(directionEditor).getByRole('textbox', { name: '方向向量' }), {
        target: { value: '[1,0,0]' }
      })
      fireEvent.click(within(directionEditor).getByRole('button', { name: '保存主方向' }))
      await waitFor(() => expect(screen.queryByRole('region', { name: '编辑出模方向' })).toBeNull())

      const savedDirection = api.mock.calls.filter(
        ([request]) => request.path.endsWith('/direction') && request.body?.forming_scope_id === 'MAIN'
      )

      expect(savedDirection).toHaveLength(3)
      expect(savedDirection[0][0].body).toEqual({ forming_scope_id: 'MAIN', draw_direction: [1, 0, 0], mechanism_kind: 'none', geometry_refs: refs, ...mainPins })
      expect(savedDirection[2][0].body).toEqual({
        forming_scope_id: 'MAIN',
        draw_direction: [1, 0, 0],
        mechanism_kind: 'none', geometry_refs: refs,
        expected_revision: Number(savedDirection[1][0].body?.expected_revision) + 1,
        expected_snapshot_id: 'discovery-raced'
      })
      await waitFor(() => expect(document.querySelector('canvas')).not.toBeNull())
      pick(0)
      fireEvent.click(screen.getByRole('tab', { name: /特征点/ }))
      await screen.findByText('带孔 Boss 柱 · 1 组待确认')
      const pending = expand('带孔 Boss 柱 · 1 组待确认')
      fireEvent.click(within(pending).getByRole('checkbox', { name: '选择 BOSS_1' }))
      fireEvent.click(screen.getByRole('button', { name: '确认选中组（1）' }))
      await screen.findByText('已确认特征 · 1 个实例')
      expect(screen.queryByRole('checkbox', { name: '选择 BOSS_1' })).toBeNull()
      const featurePins = { expected_revision: state.revision, expected_snapshot_id: state.snapshot_id }
      const confirmedSummary = screen.getByText('带孔 BOSS 柱 · 1 个实例')

      // Confirmation may preserve the selected object's open category.
      if (!confirmedSummary.closest('details')!.open) {fireEvent.click(confirmedSummary)}

      fireEvent.click(await screen.findByRole('button', { name: '编辑' }))
      const editor = await screen.findByRole('region', { name: '编辑特征选面' })
      expect(within(editor).getByText(/已选 1 个面/)).toBeTruthy()
      ray.mockImplementation(object => (object.type === 'Mesh' ? ([{ faceIndex: 2 }] as never) : []))
      fireEvent.pointerDown(canvas, { button: 0, clientX: 100, clientY: 100 })
      fireEvent.pointerUp(canvas, { button: 0, clientX: 100, clientY: 100 })
      expect(within(editor).getByText(/已选 2 个面 · 新增 1/)).toBeTruthy()
      ray.mockReturnValue([])
      fireEvent.pointerDown(canvas, { button: 0, clientX: 100, clientY: 100 })
      fireEvent.pointerUp(canvas, { button: 0, clientX: 100, clientY: 100 })
      expect(within(editor).getByText(/已选 2 个面 · 新增 1/)).toBeTruthy()
      fireEvent.click(within(editor).getByRole('button', { name: '保存选面' }))
      await screen.findByText('保存失败，请重试')
      expect(within(editor).getByText(/已选 2 个面/)).toBeTruthy()
      const readsBeforeProgress = readJsonFile.mock.calls.length
      view.rerender(
        <DfmViewerPane
          target={{
            projectId: project,
            manifestPath: '/project/viewer.json',
            status: initialStatus,
            revision: state.revision
          }}
        />
      )
      expect(within(editor).getByText(/已选 2 个面/)).toBeTruthy()
      expect(readJsonFile.mock.calls).toHaveLength(readsBeforeProgress)
      fireEvent.click(within(editor).getByRole('button', { name: '保存选面' }))
      await waitFor(() => expect(screen.queryByRole('region', { name: '编辑特征选面' })).toBeNull())
      await screen.findByText('已确认特征 · 0 个实例')
      await screen.findByText('带孔 Boss 柱 · 1 组待确认')
      const returned = expand('带孔 Boss 柱 · 1 组待确认')
      expect(readJsonFile.mock.calls.some(([path]) => path === '/project/discovery-viewer.json')).toBe(true)
      expect(within(returned).getByRole('checkbox', { name: '选择 BOSS_1' })).toBeTruthy()
      const edits = api.mock.calls.filter(([request]) => request.method === 'PATCH')
      expect(edits).toHaveLength(2)
      expect(edits[1][0].body).toEqual({
        ...featurePins,
        geometry_refs: refs.slice(0, 2)
      })
    }
  )
})
