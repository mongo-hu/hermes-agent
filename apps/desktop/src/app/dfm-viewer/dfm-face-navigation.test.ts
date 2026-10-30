import { describe, expect, it } from 'vitest'

import { createFaceNavigationIndex } from './dfm-face-navigation'
import type { ReviewContext, ReviewFeature } from './dfm-review'
import type { GeometryReference } from './dfm-viewer-geometry'

const face = (index: number): GeometryReference => ({
  kind: 'face',
  index,
  input_sha256: 'model',
  topology_snapshot_id: 'topology'
})

const feature = (id: string, indices: number[], parts = {}): ReviewFeature => ({
  feature_id: id,
  kind: 'hollow_boss',
  confidence: 1,
  properties: { geometry_refs: indices.map(face), parts }
})

function fixture() {
  const faces = [1, 2, 3, 4, 5].map(index => ({ geometry_ref: face(index), triangle_refs: [] }))

  const context: Pick<ReviewContext, 'features' | 'annotations' | 'forming_scopes'> = {
    features: [feature('BOSS', [1])],
    annotations: [
      {
        annotation_id: 'APPEARANCE',
        kind: 'appearance',
        value: 'appearance',
        status: 'active',
        geometry_refs: [face(1), face(2)]
      }
    ],
    forming_scopes: [
      {
        forming_scope_id: 'MAIN',
        kind: 'main_forming',
        mode: 'main',
        draw_direction: [0, 0, 1],
        geometry_refs: faces.map(item => item.geometry_ref)
      },
      {
        forming_scope_id: 'SIDE',
        kind: 'undercut',
        mode: 'local',
        draw_direction: null,
        geometry_refs: [face(1), face(2), face(3)]
      }
    ]
  }

  return { faces, context }
}

describe('3D face navigation', () => {
  it('prefers structures, then annotations, then local and global forming settings', () => {
    const { context, faces } = fixture()
    const index = createFaceNavigationIndex(context, faces, [])
    expect(index.get(1)).toEqual({ category: 'structure', objectId: 'BOSS' })
    expect(index.get(2)).toEqual({ category: 'annotation', objectId: 'APPEARANCE' })
    expect(index.get(3)).toEqual({ category: 'forming', objectId: 'SIDE' })
    expect(index.get(4)).toEqual({ category: 'forming', objectId: 'MAIN' })
    expect(index.get(999)).toBeUndefined()
  })

  it('chooses the narrower owning structure and excludes reference-only faces', () => {
    const { context, faces } = fixture()
    context.features = [
      feature('WALL', [1, 2, 3, 4]),
      feature('BOSS', [1, 2], {
        outer_wall: [face(1)],
        adjacent_main_wall: [face(2)]
      })
    ]
    const index = createFaceNavigationIndex(context, faces, [])
    expect(index.get(1)?.objectId).toBe('BOSS')
    expect(index.get(2)?.objectId).toBe('WALL')
  })

  it('rejects stale topology and stale annotations; maps parting edges to adjacent faces only', () => {
    const { context, faces } = fixture()
    context.features[0].properties.geometry_refs = [{ ...face(1), topology_snapshot_id: 'old' }]
    context.annotations[0].status = 'stale'
    context.forming_scopes[1].status = 'stale'
    const edge: GeometryReference = { ...face(9), kind: 'edge' }
    context.annotations.push({
      annotation_id: 'PARTING',
      kind: 'parting_boundary',
      value: 'parting',
      status: 'active',
      geometry_refs: [edge]
    })
    const index = createFaceNavigationIndex(context, faces, [{ geometry_ref: edge, adjacent_face_indices: [2] }])
    expect(index.get(1)).toEqual({ category: 'forming', objectId: 'MAIN' })
    expect(index.get(2)).toEqual({ category: 'annotation', objectId: 'PARTING' })
    expect(index.get(9)).toBeUndefined()
  })
})
