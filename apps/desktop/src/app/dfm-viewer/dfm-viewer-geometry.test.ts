import { describe, expect, it } from 'vitest'

import { mergeRenderScene, resolveGeometryRefFaceIndices } from './dfm-viewer-geometry'

describe('resolveGeometryRefFaceIndices', () => {
  it('keeps exact face references from the shared topology contract', () => {
    const result = resolveGeometryRefFaceIndices([
      { index: 7, kind: 'face' },
      { index: 12, kind: 'edge' },
      { index: 3, kind: 'vertex' }
    ])

    expect(Array.from(result)).toEqual([7])
  })
})

describe('mergeRenderScene', () => {
  const topologyFaces = [
    {
      geometry_ref: { index: 2, kind: 'face' as const },
      triangle_refs: [{ primitive_id: 'face-2', triangle_id: 0 }]
    },
    {
      geometry_ref: { index: 7, kind: 'face' as const },
      triangle_refs: [
        { primitive_id: 'face-7', triangle_id: 0 },
        { primitive_id: 'face-7', triangle_id: 1 }
      ]
    }
  ]

  it('merges shared scene primitives without changing topology face identities', () => {
    const merged = mergeRenderScene(
      [
        {
          primitive_id: 'face-2',
          triangles: [[0, 1, 2]],
          vertices: [
            [0, 0, 0],
            [1, 0, 0],
            [0, 1, 0]
          ]
        },
        {
          primitive_id: 'face-7',
          triangles: [
            [0, 1, 2],
            [0, 2, 3]
          ],
          vertices: [
            [0, 0, 1],
            [1, 0, 1],
            [1, 1, 1],
            [0, 1, 1]
          ]
        }
      ],
      topologyFaces
    )

    expect(Array.from(merged.indices)).toEqual([0, 1, 2, 3, 4, 5, 3, 5, 6])
    expect(merged.groups).toEqual([
      { count: 3, faceIndex: 2, start: 0 },
      { count: 6, faceIndex: 7, start: 3 }
    ])
    expect(Array.from(merged.triangleFaceIndices)).toEqual([2, 7, 7])
  })

  it('rejects a primitive without an exact topology mapping', () => {
    expect(() =>
      mergeRenderScene(
        [
          {
            primitive_id: 'face-99',
            triangles: [[0, 1, 2]],
            vertices: [
              [0, 0, 0],
              [1, 0, 0],
              [0, 1, 0]
            ]
          }
        ],
        []
      )
    ).toThrow('没有对应的拓扑面')
  })

  it('rejects an index outside the primitive vertex buffer', () => {
    expect(() =>
      mergeRenderScene(
        [
          {
            primitive_id: 'face-2',
            triangles: [[0, 1, 3]],
            vertices: [
              [0, 0, 0],
              [1, 0, 0],
              [0, 1, 0]
            ]
          }
        ],
        topologyFaces
      )
    ).toThrow('越界的三角形索引')
  })

  it('uses triangle references when one scene primitive contains multiple faces', () => {
    const merged = mergeRenderScene(
      [
        {
          primitive_id: 'shared',
          triangles: [
            [0, 1, 2],
            [0, 2, 3]
          ],
          vertices: [
            [0, 0, 0],
            [1, 0, 0],
            [1, 1, 0],
            [0, 1, 0]
          ]
        }
      ],
      [
        {
          geometry_ref: { index: 4, kind: 'face' },
          triangle_refs: [{ primitive_id: 'shared', triangle_id: 1 }]
        },
        {
          geometry_ref: { index: 9, kind: 'face' },
          triangle_refs: [{ primitive_id: 'shared', triangle_id: 0 }]
        }
      ]
    )

    expect(Array.from(merged.indices)).toEqual([0, 1, 2, 0, 2, 3])
    expect(merged.groups).toEqual([
      { count: 3, faceIndex: 9, start: 0 },
      { count: 3, faceIndex: 4, start: 3 }
    ])
    expect(Array.from(merged.triangleFaceIndices)).toEqual([9, 4])
  })
})
