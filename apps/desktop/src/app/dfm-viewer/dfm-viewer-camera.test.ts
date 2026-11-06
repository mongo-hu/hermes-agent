import { Box3, OrthographicCamera, Raycaster, Vector2, Vector3 } from 'three'
import { describe, expect, it } from 'vitest'

import {
  fitOrthographicCamera,
  nearestStandardPose,
  resizeOrthographicCamera,
  standardCameraPose,
  type StandardView,
  updateOrthographicClipping
} from './dfm-viewer-camera'

function corners(bounds: Box3) {
  return [bounds.min.x, bounds.max.x].flatMap(x =>
    [bounds.min.y, bounds.max.y].flatMap(y =>
      [bounds.min.z, bounds.max.z].map(z => new Vector3(x, y, z))))
}

describe('orthographic viewer camera', () => {
  it.each([
    ['up', [0, 0, 1], [0, 1, 0]],
    ['down', [0, 0, -1], [0, -1, 0]],
    ['left', [-1, 0, 0], [0, 0, 1]],
    ['right', [1, 0, 0], [0, 0, 1]],
    ['front', [0, -1, 0], [0, 0, 1]],
    ['back', [0, 1, 0], [0, 0, 1]]
  ] as const)('defines %s against the model axes without a polar singularity', (view, direction, up) => {
    const pose = standardCameraPose(view)
    expect(pose.direction.toArray()).toEqual(direction)
    expect(pose.up.toArray()).toEqual(up)
    expect(pose.direction.dot(pose.up)).toBe(0)
    const right = pose.up.clone().cross(pose.direction)
    expect(right.clone().cross(pose.up).dot(pose.direction)).toBeCloseTo(1)
  })

  it.each(['up', 'down', 'left', 'right', 'front', 'back'] as StandardView[])(
    'straightens near %s without unnecessarily reversing the current screen up', view => {
      const standard = standardCameraPose(view)
      const right = standard.up.clone().cross(standard.direction)

      for (const roll of [standard.up, right, standard.up.clone().negate(), right.clone().negate()]) {
        const camera = new OrthographicCamera(-2, 2, 1, -1, 0.1, 100)
        const target = new Vector3(7, -11, 3)
        const direction = standard.direction.clone().addScaledVector(right, 0.015).normalize()
        camera.position.copy(target).addScaledVector(direction, 30)
        camera.up.copy(roll)
        camera.zoom = 3
        camera.lookAt(target)
        const beforePosition = camera.position.clone()
        const beforeRotation = camera.quaternion.clone()
        const result = nearestStandardPose(camera)
        expect(result.direction.distanceTo(standard.direction)).toBeLessThan(1e-10)
        expect(result.up.dot(roll)).toBeCloseTo(1)
        expect(camera.position).toEqual(beforePosition)
        expect(camera.quaternion.toArray()).toEqual(beforeRotation.toArray())
        expect(camera.zoom).toBe(3)
      }
    }
  )

  it.each([0.3, 2.5])('fits an asymmetric box at aspect %s without changing its orientation', aspect => {
    const bounds = new Box3(new Vector3(-30, -10, -6), new Vector3(80, 5, 3))
    const camera = new OrthographicCamera(-aspect, aspect, 1, -1, 0.1, 10)
    const target = new Vector3(300, 100, -200)
    camera.position.copy(target).add(new Vector3(1, -1, 1).normalize().multiplyScalar(2))
    camera.up.set(0, 0, 1)
    camera.lookAt(target)
    const rotation = camera.quaternion.clone()
    fitOrthographicCamera(camera, target, bounds)
    expect(target).toEqual(bounds.getCenter(new Vector3()))
    expect(camera.quaternion.angleTo(rotation)).toBeLessThan(1e-7)
    camera.updateMatrixWorld()

    for (const corner of corners(bounds)) {
      const screen = corner.project(camera)
      expect(Math.abs(screen.x)).toBeLessThan(1)
      expect(Math.abs(screen.y)).toBeLessThan(1)
      expect(Math.abs(screen.z)).toBeLessThan(1)
    }
  })

  it('keeps vertical coverage and zoom during a window resize', () => {
    const camera = new OrthographicCamera(-10, 10, 5, -5, 0.1, 100)
    camera.zoom = 2.5
    const height = (camera.top - camera.bottom) / camera.zoom
    resizeOrthographicCamera(camera, 0.4)
    expect((camera.top - camera.bottom) / camera.zoom).toBe(height)
    expect((camera.right - camera.left) / (camera.top - camera.bottom)).toBeCloseTo(0.4)
    expect(camera.zoom).toBe(2.5)
  })

  it('retreats clipping depth without moving or resizing the projected geometry', () => {
    const bounds = new Box3(new Vector3(-2, -3, -8), new Vector3(9, 5, 20))
    const camera = new OrthographicCamera(-10, 10, 10, -10, 0.1, 2)
    camera.position.set(1, 2, 0)
    camera.lookAt(new Vector3(1, 2, -1))
    camera.updateMatrixWorld()
    const points = corners(bounds)
    const before = points.map(point => point.clone().project(camera))
    const rotation = camera.quaternion.clone()
    updateOrthographicClipping(camera, bounds)
    expect(camera.quaternion.toArray()).toEqual(rotation.toArray())
    expect(camera.zoom).toBe(1)

    points.forEach((point, index) => {
      const after = point.clone().project(camera)
      expect(after.x).toBeCloseTo(before[index].x)
      expect(after.y).toBeCloseTo(before[index].y)
      expect(Math.abs(after.z)).toBeLessThan(1)
    })
  })

  it('uses parallel picking rays and equal projected sizes at different depths', () => {
    const camera = new OrthographicCamera(-10, 10, 10, -10, 0.1, 100)
    camera.position.set(0, 0, 30)
    camera.lookAt(new Vector3())
    camera.updateMatrixWorld()
    const ray = new Raycaster()
    ray.setFromCamera(new Vector2(-0.5, 0), camera)
    const first = ray.ray.clone()
    ray.setFromCamera(new Vector2(0.5, 0), camera)
    expect(first.direction.dot(ray.ray.direction)).toBeCloseTo(1)
    expect(first.origin.distanceTo(ray.ray.origin)).toBeGreaterThan(0)
    const width = (z: number) => new Vector3(2, 0, z).project(camera).x - new Vector3(0, 0, z).project(camera).x
    expect(width(10)).toBeCloseTo(width(-10))
  })
})
