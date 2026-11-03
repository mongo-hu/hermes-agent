import { OrthographicCamera, Vector3 } from 'three'
import type { TrackballControls } from 'three/addons/controls/TrackballControls.js'
import { afterEach, describe, expect, it } from 'vitest'

import { createDfmCameraControls, resizeDfmCameraControls } from './dfm-camera-controls'
import { resizeOrthographicCamera, standardCameraPose, type StandardView } from './dfm-viewer-camera'

const cleanups: Array<() => void> = []

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) {cleanup()}
})

function viewport(initialWidth = 800, initialHeight = 600) {
  const element = document.createElement('canvas')
  let width = initialWidth
  let height = initialHeight
  Object.defineProperties(element, {
    clientWidth: { get: () => width },
    clientHeight: { get: () => height }
  })
  element.getBoundingClientRect = () => new DOMRect(20, 30, width, height)
  // jsdom has no native pointer capture. Only these platform APIs are stubbed;
  // every control action below runs the installed Three implementation.
  element.setPointerCapture = () => undefined
  element.releasePointerCapture = () => undefined
  document.body.appendChild(element)
  cleanups.push(() => element.remove())

  return { element, resize: (nextWidth: number, nextHeight: number) => { width = nextWidth; height = nextHeight } }
}

function pointer(element: HTMLElement, type: string, button: number, x: number, y: number) {
  const event = new MouseEvent(type, { bubbles: true, button, clientX: x, clientY: y,
    buttons: type === 'pointerup' ? 0 : [1, 4, 2][button] })

  Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: 'mouse' } })
  element.dispatchEvent(event)
}

function drag(controls: TrackballControls, element: HTMLElement, button: number, dx: number, dy: number) {
  const rect = element.getBoundingClientRect()
  const x = rect.x + rect.width / 2
  const y = rect.y + rect.height / 2
  pointer(element, 'pointerdown', button, x, y)

  for (let step = 1; step <= 12; step++) {
    pointer(element, 'pointermove', button, x + dx * step / 12, y + dy * step / 12)
    controls.update()
  }

  pointer(element, 'pointerup', button, x + dx, y + dy)
  controls.update()
}

function setup(view: StandardView = 'up', roll = 0, width = 800, height = 600) {
  const host = viewport(width, height)
  const pose = standardCameraPose(view)
  const target = new Vector3(17, -9, 4)
  const camera = new OrthographicCamera(-10 * width / height, 10 * width / height, 10, -10, 0.1, 1000)
  camera.position.copy(target).addScaledVector(pose.direction, 38)
  camera.up.copy(pose.up).applyAxisAngle(pose.direction, roll)
  camera.zoom = 1.7
  camera.updateProjectionMatrix()
  camera.lookAt(target)
  const controls = createDfmCameraControls(camera, host.element, target)
  cleanups.push(() => controls.dispose())

  return { ...host, camera, controls, target }
}

function screenPoint(camera: OrthographicCamera, point: Vector3, element: HTMLElement) {
  camera.updateMatrixWorld()
  const ndc = point.clone().project(camera)

  return [(ndc.x + 1) * element.clientWidth / 2, (1 - ndc.y) * element.clientHeight / 2]
}

describe('DFM free camera controls', () => {
  it('preserves a non-origin oblique pose and turns the world-up hint into screen-up', () => {
    const { element } = viewport()
    const target = new Vector3(91, -57, 12)
    const camera = new OrthographicCamera(-2, 2, 1, -1, 0.1, 1000)
    camera.position.copy(target).add(new Vector3(25, -31, 21))
    camera.up.set(0, 0, 1)
    camera.zoom = 2.4
    camera.lookAt(target)
    const rotation = camera.quaternion.clone()
    const position = camera.position.clone()
    const controls = createDfmCameraControls(camera, element, target)
    cleanups.push(() => controls.dispose())
    expect(camera.position.toArray()).toEqual(position.toArray())
    expect(camera.quaternion.toArray()).toEqual(rotation.toArray())
    expect(camera.up.distanceTo(new Vector3(0, 1, 0).applyQuaternion(rotation))).toBeLessThan(1e-10)
    controls.update()
    expect(camera.position.distanceTo(position)).toBeLessThan(1e-10)
    expect(camera.quaternion.angleTo(rotation)).toBeLessThan(1e-7)
    expect(controls.target.toArray()).toEqual(target.toArray())
    expect(camera.zoom).toBe(2.4)
  })

  it.each([['up', 0], ['down', 0], ['up', Math.PI / 2], ['front', Math.PI / 2]] as const)(
    'rotates continuously through both old poles from %s with roll %s', (view, roll) => {
      const { camera, controls, element, target } = setup(view, roll)
      const direction = camera.position.clone().sub(target).normalize()
      const rotation = camera.quaternion.clone()
      const up = camera.up.clone()
      const rect = element.getBoundingClientRect()
      const x = rect.x + rect.width / 2
      const y = rect.y + rect.height / 2
      pointer(element, 'pointerdown', 0, x, y)

      for (let step = 1; step <= 16; step++) {
        pointer(element, 'pointermove', 0, x, y + rect.height * step / 16)
        controls.update()
        const offset = camera.position.clone().sub(target).normalize()
        expect(offset.dot(direction)).toBeCloseTo(Math.cos(2 * Math.PI * step / 16), 8)
        expect(camera.up.dot(offset)).toBeCloseTo(0, 10)
        expect(camera.position.distanceTo(target)).toBeCloseTo(38, 8)
        expect(controls.target.toArray()).toEqual(target.toArray())
        expect(camera.zoom).toBe(1.7)
      }

      pointer(element, 'pointerup', 0, x, y + rect.height)
      expect(camera.quaternion.angleTo(rotation)).toBeLessThan(1e-7)
      expect(camera.up.distanceTo(up)).toBeLessThan(1e-10)

      for (let frame = 0; frame < 20; frame++) {controls.update()}
      expect(camera.quaternion.angleTo(rotation)).toBeLessThan(1e-7)
    }
  )

  it.each([[800, 600], [600, 1000]])('pans one screen pixel per pointer pixel at %s × %s', (width, height) => {
    const { camera, controls, element, target, resize } = setup('front', Math.PI / 2, width, height)
    const direction = new Vector3(1.25, -1.55, 1.05).normalize()
    camera.position.copy(target).addScaledVector(direction, 38)
    camera.up.set(0, 0, 1)
    camera.lookAt(target)
    controls.dispose()
    const obliqueControls = createDfmCameraControls(camera, element, target)
    cleanups.push(() => obliqueControls.dispose())
    const point = target.clone().add(new Vector3(2, 3, -1))
    const rotation = camera.quaternion.clone()
    const zoom = camera.zoom

    for (const distance of [38, 108]) {
      // A clip-depth correction may retreat the eye without changing its image.
      camera.position.copy(obliqueControls.target).addScaledVector(direction, distance)
      const before = screenPoint(camera, point, element)
      drag(obliqueControls, element, 1, 41, -23)
      const after = screenPoint(camera, point, element)
      expect(after[0] - before[0]).toBeCloseTo(41, 8)
      expect(after[1] - before[1]).toBeCloseTo(-23, 8)
      expect(camera.quaternion.angleTo(rotation)).toBeLessThan(1e-7)
      expect(camera.zoom).toBe(zoom)
    }

    resize(height, width)
    resizeOrthographicCamera(camera, height / width)
    resizeDfmCameraControls(obliqueControls, camera, element)
    const before = screenPoint(camera, point, element)
    drag(obliqueControls, element, 1, -29, 37)
    const after = screenPoint(camera, point, element)
    expect(after[0] - before[0]).toBeCloseTo(-29, 8)
    expect(after[1] - before[1]).toBeCloseTo(37, 8)
    const position = camera.position.clone()
    const center = obliqueControls.target.clone()

    for (let frame = 0; frame < 20; frame++) {obliqueControls.update()}
    expect(camera.position.distanceTo(position)).toBeLessThan(1e-10)
    expect(obliqueControls.target.distanceTo(center)).toBeLessThan(1e-10)
  })

  it.each(['KeyA', 'KeyS', 'KeyD'])('keeps right drag inert and left drag rotating while %s is held', code => {
    const { camera, controls, element, target } = setup()
    const position = camera.position.clone()
    const rotation = camera.quaternion.clone()
    window.dispatchEvent(new KeyboardEvent('keydown', { code }))
    drag(controls, element, 2, 80, 60)
    expect(camera.position.distanceTo(position)).toBeLessThan(1e-10)
    expect(camera.quaternion.angleTo(rotation)).toBeLessThan(1e-7)
    expect(controls.target.toArray()).toEqual(target.toArray())
    expect(camera.zoom).toBe(1.7)
    drag(controls, element, 0, 45, 0)
    expect(camera.quaternion.angleTo(rotation)).toBeGreaterThan(0.1)
    expect(controls.target.toArray()).toEqual(target.toArray())
    expect(camera.zoom).toBe(1.7)
    window.dispatchEvent(new KeyboardEvent('keyup', { code }))
  })

  it('consumes every pointer move even when no animation frame runs between events', () => {
    const { camera, controls, element, target } = setup('right')
    const direction = camera.position.clone().sub(target).normalize()
    const initialUp = camera.up.clone()
    const rect = element.getBoundingClientRect()
    const x = rect.x + rect.width / 2
    const y = rect.y + rect.height / 3
    pointer(element, 'pointerdown', 0, x, y)

    for (let step = 1; step <= 12; step++) {
      pointer(element, 'pointermove', 0, x, y + rect.height * step / 72)
    }

    pointer(element, 'pointerup', 0, x, y + rect.height / 6)
    // Simulate the first render frame after the entire fast drag. The unwrapped
    // control applies only the last 5 degrees here instead of all 60 degrees.
    controls.update()
    const result = camera.position.clone().sub(target).normalize()
    expect(result.dot(direction)).toBeCloseTo(Math.cos(Math.PI / 3), 8)
    expect(camera.up.dot(initialUp)).toBeCloseTo(Math.cos(Math.PI / 3), 8)
    expect(controls.target.toArray()).toEqual(target.toArray())
    expect(camera.zoom).toBe(1.7)
    // The later render update must not replay any already consumed increment.
    const rotation = camera.quaternion.clone()
    controls.update()
    expect(camera.quaternion.angleTo(rotation)).toBeLessThan(1e-7)
  })

  it('continues consuming a held drag after wheel zoom emits its own end event', () => {
    const { camera, controls, element, target } = setup('right')
    const direction = camera.position.clone().sub(target).normalize()
    const rect = element.getBoundingClientRect()
    const x = rect.x + rect.width / 2
    const y = rect.y + rect.height / 3
    pointer(element, 'pointerdown', 0, x, y)
    pointer(element, 'pointermove', 0, x, y + rect.height / 72)
    element.dispatchEvent(new WheelEvent('wheel', { deltaY: -120, deltaMode: 0, cancelable: true }))

    for (let step = 2; step <= 12; step++) {
      pointer(element, 'pointermove', 0, x, y + rect.height * step / 72)
    }

    pointer(element, 'pointerup', 0, x, y + rect.height / 6)
    controls.update()
    expect(camera.position.clone().sub(target).normalize().dot(direction)).toBeCloseTo(Math.cos(Math.PI / 3), 8)
    expect(controls.target.toArray()).toEqual(target.toArray())
    expect(camera.zoom).toBeGreaterThan(1.7)
  })

  it('does not update a disabled gizmo camera or leave move listeners after end/dispose', () => {
    const { camera, controls, element } = setup()
    const rect = element.getBoundingClientRect()
    const x = rect.x + rect.width / 2
    const y = rect.y + rect.height / 2
    pointer(element, 'pointerdown', 0, x, y)
    controls.enabled = false
    const position = camera.position.clone()
    const rotation = camera.quaternion.clone()
    pointer(element, 'pointermove', 0, x + 50, y + 50)
    expect(camera.position.toArray()).toEqual(position.toArray())
    expect(camera.quaternion.toArray()).toEqual(rotation.toArray())
    controls.enabled = true
    pointer(element, 'pointerup', 0, x + 50, y + 50)

    // A stale update listener would lookAt(target) and overwrite this pose.
    camera.rotateZ(0.3)
    const afterEnd = camera.quaternion.clone()
    pointer(element, 'pointermove', 0, x + 70, y + 70)
    expect(camera.quaternion.toArray()).toEqual(afterEnd.toArray())

    pointer(element, 'pointerdown', 0, x, y)
    controls.dispose()
    camera.rotateX(0.2)
    const afterDispose = camera.quaternion.clone()
    pointer(element, 'pointermove', 0, x + 50, y + 50)
    pointer(element, 'pointerup', 0, x + 50, y + 50)
    expect(camera.quaternion.toArray()).toEqual(afterDispose.toArray())
  })

  it.each(['pointercancel', 'lostpointercapture'])('removes its move listener after %s', eventType => {
    const { camera, element } = setup()
    const rect = element.getBoundingClientRect()
    const x = rect.x + rect.width / 2
    const y = rect.y + rect.height / 2
    pointer(element, 'pointerdown', 0, x, y)
    pointer(element, eventType, 0, x, y)
    camera.rotateZ(0.3)
    const rotation = camera.quaternion.clone()
    pointer(element, 'pointermove', 0, x + 30, y + 30)
    expect(camera.quaternion.toArray()).toEqual(rotation.toArray())
  })

  it('keeps wheel zoom and stops immediately after its input', () => {
    const { camera, controls, element, target } = setup()
    const rotation = camera.quaternion.clone()
    element.dispatchEvent(new WheelEvent('wheel', { deltaY: -120, deltaMode: 0, cancelable: true }))
    controls.update()
    expect(camera.zoom).toBeGreaterThan(1.7)
    const zoom = camera.zoom

    for (let frame = 0; frame < 20; frame++) {controls.update()}
    expect(camera.zoom).toBe(zoom)
    expect(controls.target.toArray()).toEqual(target.toArray())
    expect(camera.quaternion.angleTo(rotation)).toBeLessThan(1e-7)
  })
})
