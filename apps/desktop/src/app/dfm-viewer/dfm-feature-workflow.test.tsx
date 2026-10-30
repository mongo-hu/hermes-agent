import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { Raycaster } from 'three'
import type * as ThreeTypes from 'three'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ReviewContext } from './dfm-review'
import { DfmViewerPane } from './dfm-viewer-pane'

vi.mock('three', async importOriginal => {
  const actual = await importOriginal<typeof ThreeTypes>()

  return {
    ...actual,
    WebGLRenderer: class {
      domElement = document.createElement('canvas')
      setPixelRatio() {}
      setSize() {}
      render() {}
      dispose() {}
    }
  }
})
vi.mock('three/addons/controls/OrbitControls.js', async () => {
  const { Vector3 } = await import('three')

  return {
    OrbitControls: class {
      target = new Vector3()
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
})

function expand(label: string) {
  const details = screen.getByText(label).closest('details')!
  details.open = true
  fireEvent(details, new Event('toggle'))

  return details
}

describe('candidate confirmation and feature editing', () => {
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
            draw_direction: [0, 0, 1],
            geometry_refs: refs
          },
          { forming_scope_id: 'SIDE', kind: 'undercut', mode: 'local', draw_direction: null, geometry_refs: [refs[3]] }
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
          { review_group_id: 'side', kind: 'undercut_scope', target_refs: ['SIDE'], status: 'pending_review' },
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

      const api = vi.fn(async (request: { path: string; method?: string; body?: Record<string, unknown>; timeoutMs?: number }) => {
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
      major.open = false
      fireEvent(major, new Event('toggle'))
      pick(0)
      await waitFor(() => expect(major.open).toBe(true))
      pick(4)
      await screen.findByRole('checkbox', { name: '选择 APPEARANCE' })
      expect(document.querySelector('[data-review-object="APPEARANCE"]')?.className).toContain('border-amber-400')
      pick(6)
      await screen.findByRole('checkbox', { name: '选择 SIDE' })
      expect(document.querySelector('[data-review-object="SIDE"]')?.className).toContain('border-amber-400')
      fireEvent.click(
        within(document.querySelector('[data-review-object="SIDE"]') as HTMLElement).getByRole('button', {
          name: '编辑范围'
        })
      )
      await screen.findByText(/编辑范围 SIDE/)
      fireEvent.click(screen.getByRole('button', { name: '输入退出方向' }))
      const stagedEditor = await screen.findByRole('region', { name: '编辑出模方向' })
      fireEvent.change(within(stagedEditor).getByRole('textbox', { name: '方向向量' }), {
        target: { value: '[1,0,0]' }
      })
      fireEvent.click(within(stagedEditor).getByRole('button', { name: '暂存方向' }))
      await waitFor(() => expect(screen.queryByRole('region', { name: '编辑出模方向' })).toBeNull())
      expect(state.review_groups.find(group => group.review_group_id === 'side')?.status).toBe('pending_review')
      await waitFor(() => expect(document.querySelector('canvas')).not.toBeNull())
      pick(8)
      await screen.findByText('整件主出模设置')
      expect(document.querySelector('[data-review-object="MAIN"]')?.className).toContain('border-amber-400')
      fireEvent.click(
        within(document.querySelector('[data-review-object="MAIN"]') as HTMLElement).getByRole('button', {
          name: '编辑方向'
        })
      )
      const directionEditor = await screen.findByRole('region', { name: '编辑出模方向' })
      fireEvent.change(within(directionEditor).getByRole('textbox', { name: '方向向量' }), {
        target: { value: '[1,0,0]' }
      })
      expect(document.querySelector('[data-direction-vector]')?.getAttribute('data-direction-vector')).toBe('[1,0,0]')
      state = { ...state, revision: state.revision + 1, snapshot_id: 'unrelated-new-review' }
      const mainPins = { expected_revision: state.revision, expected_snapshot_id: state.snapshot_id }
      fireEvent.click(within(directionEditor).getByRole('button', { name: '保存方向' }))
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
      fireEvent.click(within(directionEditor).getByRole('button', { name: '保存方向' }))
      await screen.findByRole('button', { name: '重新载入最新区域' })
      expect((within(directionEditor).getByRole('textbox', { name: '方向向量' }) as HTMLInputElement).value).toBe(
        '[1,0,0]'
      )
      expect((within(directionEditor).getByRole('button', { name: '保存方向' }) as HTMLButtonElement).disabled).toBe(
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
      fireEvent.click(within(directionEditor).getByRole('button', { name: '保存方向' }))
      await waitFor(() => expect(screen.queryByRole('region', { name: '编辑出模方向' })).toBeNull())

      const savedDirection = api.mock.calls.filter(
        ([request]) => request.path.endsWith('/direction') && request.body?.forming_scope_id === 'MAIN'
      )

      expect(savedDirection).toHaveLength(3)
      expect(savedDirection[0][0].body).toEqual({ forming_scope_id: 'MAIN', draw_direction: [1, 0, 0], ...mainPins })
      expect(savedDirection[2][0].body).toEqual({
        forming_scope_id: 'MAIN',
        draw_direction: [1, 0, 0],
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
      expand('带孔 BOSS 柱 · 1 个实例')
      fireEvent.click(screen.getByRole('button', { name: '编辑' }))
      const editor = await screen.findByRole('region', { name: '编辑特征选面' })
      expect(within(editor).getByText(/已选 1 个面/)).toBeTruthy()
      ray.mockImplementation(object => (object.type === 'Mesh' ? ([{ faceIndex: 2 }] as never) : []))
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
