import { OrthographicCamera, Vector3 } from 'three'
import { describe, expect, it } from 'vitest'

import { setOrthographicVisibleHeight, updateOrthographicClipping } from './dfm-viewer-camera'
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

function cameraAbove(aspect = 1) {
  const camera = new OrthographicCamera(-aspect, aspect, 1, -1, 0.001, 1000)
  camera.position.set(0, 0, 15)
  camera.up.set(0, 1, 0)
  camera.lookAt(new Vector3())

  return camera
}

describe('DFM orthographic selection camera', () => {
  it.each([false, true])('turns toward an occluded bottom face, even with reversed triangle winding (%s)', reversed => {
    const pose = selectionCameraPose(createFocusIndex(model(reversed)), new Set([1]), cameraAbove())!
    expect(pose.direction.z).toBe(-1)
    expect(pose.up.toArray()).toEqual([0, -1, 0])
    expect(pose.target.z).toBe(-1)
  })

  it('fits every selected vertex inside the viewport without changing model coordinates', () => {
    const mesh = model()
    const before = mesh.positions.slice()
    const index = createFocusIndex(mesh)
    const camera = cameraAbove(0.6)
    const pose = selectionCameraPose(index, new Set([1]), camera)!
    camera.up.copy(pose.up)
    camera.position.copy(pose.target).addScaledVector(pose.direction, 1)
    camera.lookAt(pose.target)
    setOrthographicVisibleHeight(camera, pose.visibleHeight)
    updateOrthographicClipping(camera, index.bounds)
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
    expect(selectionCameraPose(createFocusIndex(model()), new Set([100]), cameraAbove())).toBeNull()
  })

  it('zooms to a small feature on a wide thin model instead of fitting the whole bounding sphere', () => {
    const mesh = model()
    mesh.positions.set([-100, -100, -1, 100, -100, -1, 100, 100, -1, -100, 100, -1])
    const pose = selectionCameraPose(createFocusIndex(mesh), new Set([2]), cameraAbove())!
    expect(pose.direction.z).toBe(1)
    expect(pose.visibleHeight).toBeGreaterThan(4)
    expect(pose.visibleHeight).toBeLessThan(6)
  })

  it('tests separated selected faces with parallel sight lines, including narrow occluders above both', () => {
    const quads = [[-100, 0], [100, 0], [-100, 10], [100, 10]]

    const mesh: MergedFaceMeshData = {
      positions: new Float32Array(quads.flatMap(([x, z]) => [x - 1, -1, z, x + 1, -1, z, x + 1, 1, z, x - 1, 1, z])),
      indices: new Uint32Array(quads.flatMap((_, index) => {
        const start = index * 4

        return [start, start + 1, start + 2, start, start + 2, start + 3]
      })),
      groups: quads.map((_, index) => ({ faceIndex: index + 1, start: index * 6, count: 6 })),
      triangleFaceIndices: new Uint32Array(quads.flatMap((_, index) => [index + 1, index + 1]))
    }

    const pose = selectionCameraPose(createFocusIndex(mesh), new Set([1, 2]), cameraAbove())!
    expect(pose.direction.z).toBe(-1)
    expect(pose.target.toArray()).toEqual([0, 0, 0])
  })
})
