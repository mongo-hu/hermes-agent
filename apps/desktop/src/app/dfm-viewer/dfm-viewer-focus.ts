import { Box3, type OrthographicCamera, Ray, Vector3 } from 'three'

import { type CameraOrientation, orthographicCameraOrientation, orthographicVisibleHeight } from './dfm-viewer-camera'
import type { MergedFaceMeshData } from './dfm-viewer-geometry'

interface FocusFace {
  start: number
  count: number
  bounds: Box3
  area: number
  normal: Vector3
  sample: Vector3
}

export interface FocusIndex {
  mesh: MergedFaceMeshData
  bounds: Box3
  faces: Map<number, FocusFace>
}

/** Reuse the displayed mesh and per-face bounds; no STEP reload or geometry query. */
export function createFocusIndex(mesh: MergedFaceMeshData): FocusIndex {
  const bounds = new Box3()
  const faces = new Map<number, FocusFace>()

  const a = new Vector3(),
    b = new Vector3(),
    c = new Vector3(),
    normal = new Vector3(),
    edge = new Vector3()

  for (const group of mesh.groups) {
    const face: FocusFace = {
      start: group.start,
      count: group.count,
      bounds: new Box3(),
      area: 0,
      normal: new Vector3(),
      sample: new Vector3()
    }

    let largest = 0

    for (let i = group.start; i < group.start + group.count; i += 3) {
      a.fromArray(mesh.positions, mesh.indices[i] * 3)
      b.fromArray(mesh.positions, mesh.indices[i + 1] * 3)
      c.fromArray(mesh.positions, mesh.indices[i + 2] * 3)
      face.bounds.expandByPoint(a).expandByPoint(b).expandByPoint(c)
      normal.subVectors(b, a).cross(edge.subVectors(c, a))
      const area = normal.length() / 2
      face.area += area

      if (area > largest) {
        largest = area
        face.normal.copy(normal).normalize()
        face.sample.copy(a).add(b).add(c).divideScalar(3)
      }
    }

    bounds.union(face.bounds)

    if (face.area > 0) {
      faces.set(group.faceIndex, face)
    }
  }

  return { mesh, bounds, faces }
}

/** Prefer an unobstructed view of the actual selected faces, then fit that selection. */
export function selectionCameraPose(
  index: FocusIndex,
  selected: ReadonlySet<number>,
  camera: OrthographicCamera
): (CameraOrientation & { target: Vector3; visibleHeight: number; modelRadius: number }) | null {
  const faces = [...selected].flatMap(id => (index.faces.has(id) ? [index.faces.get(id)!] : []))

  if (!faces.length || index.bounds.isEmpty()) {
    return null
  }

  const bounds = faces.reduce((box, face) => box.union(face.bounds), new Box3())
  const target = bounds.getCenter(new Vector3())
  const modelCenter = index.bounds.getCenter(new Vector3())
  const modelRadius = Math.max(index.bounds.getSize(new Vector3()).length() / 2, 1e-6)
  const currentDirection = orthographicCameraOrientation(camera).direction
  const weightedNormal = faces.reduce((sum, face) => sum.addScaledVector(face.normal, face.area), new Vector3())
  const samples = [...faces].sort((left, right) => right.area - left.area).slice(0, 4)
  const directions: Vector3[] = []

  for (const direction of [
    weightedNormal,
    ...samples.map(face => face.normal),
    target.clone().sub(modelCenter),
    currentDirection,
    new Vector3(0, 0, 1),
    new Vector3(0, 0, -1),
    new Vector3(1, 0, 0),
    new Vector3(-1, 0, 0),
    new Vector3(0, 1, 0),
    new Vector3(0, -1, 0)
  ]) {
    if (direction.lengthSq() < 1e-12) {
      continue
    }

    const candidate = direction.clone().normalize()

    if (!directions.some(existing => existing.dot(candidate) > 0.995)) {
      directions.push(candidate)
    }
  }

  const tolerance = modelRadius * 1e-5
  const rayDistance = modelRadius * 4 + tolerance

  const ray = new Ray(),
    a = new Vector3(),
    b = new Vector3(),
    c = new Vector3(),
    hit = new Vector3()

  // Bound work even for a face containing a very dense triangulation.
  let remainingTriangleTests = 250000

  const visible = (direction: Vector3, point: Vector3): boolean | null => {
    // Every sample has its own ray origin: orthographic sight lines are parallel.
    const origin = point.clone().addScaledVector(direction, rayDistance)
    ray.set(origin, direction.clone().negate())

    for (const face of index.faces.values()) {
      if (!ray.intersectBox(face.bounds, hit) || origin.distanceTo(hit) > rayDistance + tolerance) {
        continue
      }

      for (let i = face.start; i < face.start + face.count; i += 3) {
        if (--remainingTriangleTests < 0) {
          return null
        }

        a.fromArray(index.mesh.positions, index.mesh.indices[i] * 3)
        b.fromArray(index.mesh.positions, index.mesh.indices[i + 1] * 3)
        c.fromArray(index.mesh.positions, index.mesh.indices[i + 2] * 3)

        if (ray.intersectTriangle(a, b, c, false, hit) && origin.distanceTo(hit) < rayDistance - tolerance) {
          return false
        }
      }
    }

    return true
  }

  let bestDirection: Vector3 | null = null
  let bestScore = -Infinity

  for (const direction of directions) {
    let exposed = 0,
      area = 0

    let exhausted = false

    for (const face of samples) {
      const canSee = visible(direction, face.sample)

      if (canSee === null) {
        exhausted = true

        break
      }

      area += face.area

      if (canSee) {
        exposed += face.area * Math.abs(face.normal.dot(direction))
      }
    }

    if (exhausted && bestDirection) {
      break
    }

    const score =
      (area ? exposed / area : 0) * 10 +
      weightedNormal.clone().normalize().dot(direction) * 0.3 +
      currentDirection.dot(direction) * 0.05

    if (score > bestScore) {
      bestScore = score
      bestDirection = direction
    }
  }

  if (!bestDirection) {return null}

  const up = Math.abs(bestDirection.z) > 0.9999
    ? new Vector3(0, bestDirection.z > 0 ? 1 : -1, 0) : new Vector3(0, 0, 1)

  const orientation = { direction: bestDirection, up }

  return { ...orientation, target, modelRadius, visibleHeight: orthographicVisibleHeight(
    bounds, orientation, (camera.right - camera.left) / (camera.top - camera.bottom)
  ) }
}
