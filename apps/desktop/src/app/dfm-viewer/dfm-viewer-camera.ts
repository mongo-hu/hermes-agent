import type { Box3, OrthographicCamera } from 'three'
import { Quaternion, Vector3 } from 'three'

export type StandardView = 'up' | 'down' | 'left' | 'right' | 'front' | 'back'

export interface CameraOrientation {
  /** Unit vector from the observation target toward the camera. */
  direction: Vector3
  up: Vector3
}

const STANDARD_VIEWS: StandardView[] = ['up', 'down', 'left', 'right', 'front', 'back']

export function standardCameraPose(view: StandardView): CameraOrientation {
  switch (view) {
    case 'up': return { direction: new Vector3(0, 0, 1), up: new Vector3(0, 1, 0) }

    case 'down': return { direction: new Vector3(0, 0, -1), up: new Vector3(0, -1, 0) }

    case 'left': return { direction: new Vector3(-1, 0, 0), up: new Vector3(0, 0, 1) }

    case 'right': return { direction: new Vector3(1, 0, 0), up: new Vector3(0, 0, 1) }

    case 'front': return { direction: new Vector3(0, -1, 0), up: new Vector3(0, 0, 1) }

    case 'back': return { direction: new Vector3(0, 1, 0), up: new Vector3(0, 0, 1) }
  }
}

export function orthographicCameraOrientation(camera: OrthographicCamera): CameraOrientation {
  const rotation = camera.getWorldQuaternion(new Quaternion())

  return {
    direction: new Vector3(0, 0, 1).applyQuaternion(rotation),
    up: new Vector3(0, 1, 0).applyQuaternion(rotation)
  }
}

/** Snap the viewing direction first, then keep the closest quarter-turn roll. */
export function nearestStandardPose(camera: OrthographicCamera): CameraOrientation {
  const current = orthographicCameraOrientation(camera)
  let nearest = standardCameraPose(STANDARD_VIEWS[0])

  for (const view of STANDARD_VIEWS.slice(1)) {
    const candidate = standardCameraPose(view)

    if (candidate.direction.dot(current.direction) > nearest.direction.dot(current.direction) + 1e-10) {
      nearest = candidate
    }
  }

  const right = nearest.up.clone().cross(nearest.direction)
  const rolls = [nearest.up, right, nearest.up.clone().negate(), right.clone().negate()]
  let up = rolls[0]

  for (const candidate of rolls.slice(1)) {
    if (candidate.dot(current.up) > up.dot(current.up) + 1e-10) {up = candidate}
  }

  return { direction: nearest.direction, up: up.clone() }
}

function boxCorners(bounds: Box3): Vector3[] {
  return [bounds.min.x, bounds.max.x].flatMap(x =>
    [bounds.min.y, bounds.max.y].flatMap(y =>
      [bounds.min.z, bounds.max.z].map(z => new Vector3(x, y, z))))
}

/** Fit the projected box, not its sphere; a thin long part can fill the view. */
export function orthographicVisibleHeight(
  bounds: Box3,
  orientation: CameraOrientation,
  aspect: number,
  padding = 1.12
): number {
  if (bounds.isEmpty()) {return 0}
  const direction = orientation.direction.clone().normalize()
  const right = orientation.up.clone().cross(direction).normalize()
  const up = direction.clone().cross(right).normalize()
  const center = bounds.getCenter(new Vector3())
  let halfWidth = 0
  let halfHeight = 0

  for (const corner of boxCorners(bounds)) {
    corner.sub(center)
    halfWidth = Math.max(halfWidth, Math.abs(corner.dot(right)))
    halfHeight = Math.max(halfHeight, Math.abs(corner.dot(up)))
  }

  return Math.max(halfHeight * 2, halfWidth * 2 / Math.max(aspect, 1e-6), 1e-6) * Math.max(padding, 1)
}

export function setOrthographicVisibleHeight(camera: OrthographicCamera, visibleHeight: number): void {
  camera.zoom = Math.max(camera.top - camera.bottom, 1e-9) / Math.max(visibleHeight, 1e-9)
  camera.updateProjectionMatrix()
}

/** A resize changes horizontal coverage while retaining vertical coverage and zoom. */
export function resizeOrthographicCamera(camera: OrthographicCamera, aspect: number): void {
  const halfHeight = Math.max((camera.top - camera.bottom) / 2, 1e-9)
  const center = (camera.left + camera.right) / 2
  const halfWidth = halfHeight * Math.max(aspect, 1e-6)
  camera.left = center - halfWidth
  camera.right = center + halfWidth
  camera.updateProjectionMatrix()
}

/** Depth changes never change orthographic framing; keep all model corners in front. */
export function updateOrthographicClipping(camera: OrthographicCamera, bounds: Box3): void {
  if (bounds.isEmpty()) {return}
  const forward = camera.getWorldDirection(new Vector3())
  const depths = boxCorners(bounds).map(corner => corner.sub(camera.position).dot(forward))
  let nearest = Math.min(...depths)
  let farthest = Math.max(...depths)
  const margin = Math.max(bounds.getSize(new Vector3()).length() * 0.01, 1e-5)

  if (nearest < margin) {
    const retreat = margin - nearest
    camera.position.addScaledVector(forward, -retreat)
    nearest += retreat
    farthest += retreat
    camera.updateMatrixWorld()
  }

  camera.near = Math.max(nearest - margin / 2, margin / 100)
  camera.far = Math.max(farthest + margin, camera.near + margin)
  camera.updateProjectionMatrix()
}

/** Recenter and rescale only. The caller's current viewing direction and roll stay intact. */
export function fitOrthographicCamera(
  camera: OrthographicCamera,
  target: Vector3,
  bounds: Box3,
  padding = 1.12
): void {
  if (bounds.isEmpty()) {return}
  const orientation = orthographicCameraOrientation(camera)
  const center = bounds.getCenter(new Vector3())
  camera.position.add(center.clone().sub(target))
  target.copy(center)
  setOrthographicVisibleHeight(camera, orthographicVisibleHeight(
    bounds, orientation, (camera.right - camera.left) / (camera.top - camera.bottom), padding
  ))
  updateOrthographicClipping(camera, bounds)
}
