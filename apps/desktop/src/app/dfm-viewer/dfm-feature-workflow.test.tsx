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

      const refs = [1, 2].map(index => ({
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
        annotations: [],
        forming_scopes: [],
        features: [
          { feature_id: 'BOSS_1', kind: 'hollow_boss', confidence: 1, properties: { geometry_refs: [refs[0]] } },
          { feature_id: 'BLEND_1', kind: 'blend', confidence: 1, properties: { geometry_refs: [refs[1]] } }
        ],
        review_groups: [
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
        issues: [],
        issue_count: 0,
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
        render_mesh_snapshot: { render_mesh_snapshot_id: 'mesh-A', triangle_count: 4 },
        primitives: [
          {
            primitive_id: 'body',
            vertices: [
              [0, 0, 0],
              [1, 0, 0],
              [1, 1, 0],
              [0, 1, 0],
              [2, 0, 0],
              [3, 0, 0],
              [3, 1, 0],
              [2, 1, 0]
            ],
            triangles: [
              [0, 1, 2],
              [0, 2, 3],
              [4, 5, 6],
              [4, 6, 7]
            ]
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

      const api = vi.fn(async (request: { path: string; method?: string; body?: Record<string, unknown> }) => {
        if (request.method === 'POST') {
          state = {
            ...state,
            revision: 8,
            review_groups: state.review_groups.map(group =>
              group.review_group_id === 'boss' ? { ...group, status: 'confirmed' as const } : group
            )
          }
        } else if (request.method === 'PATCH') {
          if (!failedOnce) {
            failedOnce = true
            throw new Error('保存失败，请重试')
          }

          state = {
            ...state,
            revision: 9,
            snapshot_id: 'discovery-B',
            features: state.features.map(feature =>
              feature.feature_id === 'BOSS_1' ? { ...feature, properties: { geometry_refs: refs } } : feature
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
            revision: 9,
            discovery_snapshot_id: state.snapshot_id,
            geometry_refs: refs,
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
      fireEvent.click(screen.getByRole('tab', { name: /特征点/ }))
      await screen.findByText('带孔 Boss 柱 · 1 组待确认')
      const pending = expand('带孔 Boss 柱 · 1 组待确认')
      fireEvent.click(within(pending).getByRole('checkbox', { name: '选择 BOSS_1' }))
      fireEvent.click(screen.getByRole('button', { name: '确认选中组（1）' }))
      await screen.findByText('已确认特征 · 1 个实例')
      expect(screen.queryByRole('checkbox', { name: '选择 BOSS_1' })).toBeNull()
      expand('带孔 BOSS 柱 · 1 个实例')
      fireEvent.click(screen.getByRole('button', { name: '编辑' }))
      const editor = await screen.findByRole('region', { name: '编辑特征选面' })
      expect(within(editor).getByText(/已选 1 个面/)).toBeTruthy()
      const canvas = document.querySelector('canvas')!
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
      vi.spyOn(Raycaster.prototype, 'intersectObject').mockReturnValue([{ faceIndex: 2 }] as never)
      fireEvent.pointerDown(canvas, { button: 0, clientX: 100, clientY: 100 })
      fireEvent.pointerUp(canvas, { button: 0, clientX: 100, clientY: 100 })
      expect(within(editor).getByText(/已选 2 个面 · 新增 1/)).toBeTruthy()
      fireEvent.click(within(editor).getByRole('button', { name: '保存选面' }))
      await screen.findByText('保存失败，请重试')
      expect(within(editor).getByText(/已选 2 个面/)).toBeTruthy()
      const readsBeforeProgress = readJsonFile.mock.calls.length
      view.rerender(
        <DfmViewerPane
          target={{ projectId: project, manifestPath: '/project/viewer.json', status: initialStatus, revision: 8 }}
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
        expected_revision: 8,
        expected_snapshot_id: 'discovery-A',
        geometry_refs: refs
      })
    }
  )
})
