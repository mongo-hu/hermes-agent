import { PerspectiveCamera, Vector3 } from 'three'
import { describe, expect, it } from 'vitest'

import { createFocusIndex, selectionCameraPose } from './dfm-viewer-focus'
import type { MergedFaceMeshData } from './dfm-viewer-geometry'

function model(reversedBottom = false): MergedFaceMeshData {
  return {
    positions: new Float32Array([-2, -2, -1, 2, -2, -1, 2, 2, -1, -2, 2, -1, -2, -2, 1, 2, -2, 1, 2, 2, 1, -2, 2, 1]),
    indices: new Uint32Array([...(reversedBottom ? [0, 1, 2, 0, 2, 3] : [0, 2, 1, 0, 3, 2]), 4, 5, 6, 4, 6, 7]),
    groups: [
      { faceIndex: 1, start: 0, count: 6 },
      { faceIndex: 2, start: 6, count: 6 }
    ],
    triangleFaceIndices: new Uint32Array([1, 1, 2, 2])
  }
}

describe('DFM selection camera', () => {
  it.each([false, true])('turns toward an occluded bottom face, even with reversed triangle winding (%s)', reversed => {
    const pose = selectionCameraPose(createFocusIndex(model(reversed)), new Set([1]), new Vector3(0, 0, 15), 38, 1)!
    expect(pose.position.z).toBeLessThan(-1)
    expect(pose.target.z).toBe(-1)
  })

  it('fits every selected vertex inside the viewport without changing model coordinates', () => {
    const mesh = model()
    const before = mesh.positions.slice()
    const pose = selectionCameraPose(createFocusIndex(mesh), new Set([1]), new Vector3(0, 0, 15), 38, 0.6)!
    const camera = new PerspectiveCamera(38, 0.6, 0.001, 1000)
    camera.up.set(0, 0, 1)
    camera.position.copy(pose.position)
    camera.lookAt(pose.target)
    camera.updateMatrixWorld()

    for (let i = 0; i < 12; i += 3) {
      const point = new Vector3().fromArray(mesh.positions, i).project(camera)
      expect(Math.abs(point.x)).toBeLessThan(1)
      expect(Math.abs(point.y)).toBeLessThan(1)
      expect(Math.abs(point.z)).toBeLessThan(1)
    }

    expect(mesh.positions).toEqual(before)
  })

  it('leaves the view unchanged for absent geometry', () => {
    expect(selectionCameraPose(createFocusIndex(model()), new Set([100]), new Vector3(), 38, 1)).toBeNull()
  })

  it('zooms to a small feature on a wide thin model instead of fitting the whole bounding sphere', () => {
    const mesh = model()
    mesh.positions.set([-100, -100, -1, 100, -100, -1, 100, 100, -1, -100, 100, -1])
    const pose = selectionCameraPose(createFocusIndex(mesh), new Set([2]), new Vector3(0, 0, 500), 38, 1)!
    expect(pose.position.z).toBeGreaterThan(1)
    expect(pose.position.distanceTo(pose.target)).toBeLessThan(30)
  })
})
