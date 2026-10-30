import {
  ArrowHelper,
  Box3,
  Group,
  type Line,
  type Mesh,
  type PerspectiveCamera,
  Quaternion,
  Raycaster,
  type Scene,
  Vector2,
  Vector3
} from 'three'
import type { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { TransformControls } from 'three/addons/controls/TransformControls.js'

import type { FocusIndex } from './dfm-viewer-focus'
import type { GeometryReference } from './dfm-viewer-geometry'

export interface DirectionAxisState {
  scopeId: string
  direction: number[] | null
  geometryRefs: GeometryReference[]
  origin?: [number, number, number]
  editing: boolean
  mode: 'rotate' | 'translate'
}

export function directionPose(vector: number[], origin: Vector3) {
  const direction = new Vector3(...vector).normalize()

  return { position: origin.clone(), quaternion: new Quaternion().setFromUnitVectors(new Vector3(0, 0, 1), direction) }
}

export function directionFromPose(object: Group): [number, number, number] {
  return new Vector3(0, 0, 1)
    .applyQuaternion(object.quaternion)
    .normalize()
    .toArray()
    .map(value => Number(value.toFixed(8))) as [number, number, number]
}

/** Display-only anchor. Native geometry consumes the direction, never this position. */
export function directionOrigin(index: FocusIndex, refs: GeometryReference[]) {
  const box = new Box3()

  for (const ref of refs) {
    const face = ref.kind === 'face' ? index.faces.get(ref.index) : undefined

    if (face) {box.union(face.bounds)}
  }

  return (box.isEmpty() ? index.bounds : box).getCenter(new Vector3())
}

export function createDirectionAxis(
  scene: Scene,
  camera: PerspectiveCamera,
  element: HTMLElement,
  orbit: OrbitControls,
  focusIndex: FocusIndex,
  onChange: (direction: [number, number, number], origin: [number, number, number]) => void
) {
  const object = new Group()
  const length = Math.max(focusIndex.bounds.getSize(new Vector3()).length() * 0.45, 0.1)
  const arrow = new ArrowHelper(new Vector3(0, 0, 1), new Vector3(), length, 0x47e4f0, length * 0.2, length * 0.085)
  object.add(arrow)
  scene.add(object)
  arrow.traverse(child => {
    const material = (child as Mesh | Line).material

    if (material && !Array.isArray(material)) {
      material.depthTest = false
      material.depthWrite = false
    }

    child.renderOrder = 20
  })
  const transform = new TransformControls(camera, element)
  transform.enabled = false
  transform.setSize(0.85)
  transform.setSpace('world')
  scene.add(transform.getHelper())
  let editing = false
  let visible = false

  const dragging = (event: { value?: unknown }) => {
    orbit.enabled = !event.value
  }

  const changed = () => {
    if (editing && transform.dragging) {onChange(directionFromPose(object), object.position.toArray())}
  }

  transform.addEventListener('dragging-changed', dragging)
  transform.addEventListener('objectChange', changed)
  const ray = new Raycaster()
  ray.params.Line = { threshold: length * 0.04 }

  return {
    update(state: DirectionAxisState | null) {
      editing = Boolean(state?.editing)
      visible = Boolean(state?.direction)
      transform.enabled = editing && visible
      object.visible = visible

      if (!visible || !state?.direction) {
        transform.detach()
        orbit.enabled = true

        return
      }

      if (!transform.dragging) {
        const pose = directionPose(
          state.direction,
          state.origin ? new Vector3(...state.origin) : directionOrigin(focusIndex, state.geometryRefs)
        )

        object.position.copy(pose.position)
        object.quaternion.copy(pose.quaternion)
      }

      arrow.setColor(editing ? 0xffc34a : 0x47e4f0)

      if (editing) {
        transform.attach(object)
        transform.setMode(state.mode)
      } else {
        transform.detach()
        orbit.enabled = true
      }
    },
    hit(event: PointerEvent) {
      if (!visible) {return false}
      const rect = element.getBoundingClientRect()
      ray.setFromCamera(
        new Vector2(
          ((event.clientX - rect.left) / rect.width) * 2 - 1,
          -((event.clientY - rect.top) / rect.height) * 2 + 1
        ),
        camera
      )

      return ray.intersectObject(arrow, true).length > 0
    },
    dispose() {
      transform.removeEventListener('dragging-changed', dragging)
      transform.removeEventListener('objectChange', changed)
      transform.detach()
      scene.remove(transform.getHelper(), object)
      transform.dispose()

      // ArrowHelper uses shared geometries; dispose only its private materials.
      for (const material of [arrow.line.material, arrow.cone.material].flat()) {material.dispose()}
      orbit.enabled = true
    }
  }
}
