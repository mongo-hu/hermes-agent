import { MOUSE, type OrthographicCamera, type Vector3 } from 'three'
import { TrackballControls } from 'three/addons/controls/TrackballControls.js'

/** Keep screen-space drag distances independent of orthographic camera depth. */
export function resizeDfmCameraControls(
  controls: TrackballControls,
  camera: OrthographicCamera,
  element: HTMLElement
) {
  const width = Math.max(element.clientWidth, 1)
  const height = Math.max(element.clientHeight, 1)
  const distance = Math.max(camera.position.distanceTo(controls.target), 1e-9)

  // Trackball normalizes pan by width/height, then multiplies it by eye depth
  // and divides both orthographic axes by clientWidth. Cancel that extra scale.
  controls.panSpeed = width / distance
  // Mouse-circle coordinates span two units per viewport width.
  controls.rotateSpeed = Math.PI * width / height
  controls.handleResize()
}

export function createDfmCameraControls(camera: OrthographicCamera, element: HTMLElement, target: Vector3) {
  const position = camera.position.clone()
  const rotation = camera.quaternion.clone()

  // lookAt's world-up hint need not be perpendicular to the viewing direction.
  // Trackball pans along up itself, so use the actual screen-up without changing
  // the displayed pose. It will rotate up and the eye together across all poles.
  camera.up.set(0, 1, 0).applyQuaternion(rotation).normalize()
  const controls = new TrackballControls(camera, element)
  controls.target.copy(target)
  // The constructor updates against its default target at the world origin.
  camera.position.copy(position)
  camera.quaternion.copy(rotation)
  camera.updateMatrixWorld()
  controls.mouseButtons = { LEFT: MOUSE.ROTATE, MIDDLE: MOUSE.PAN, RIGHT: null }
  controls.staticMoving = true
  controls.keys = ['', '', '']
  controls.minZoom = 0.001
  controls.maxZoom = 1e6

  const resize = () => resizeDfmCameraControls(controls, camera, element)
  const activePointers = new Set<number>()

  const beginPointer = (event: PointerEvent) => {
    if (controls.enabled) {activePointers.add(event.pointerId)}
  }

  const flushMovement = () => {
    if (controls.enabled) {controls.update()}
  }

  const stopMovement = () => {
    element.removeEventListener('pointermove', flushMovement)
    element.removeEventListener('pointerup', finishPointer)
    element.removeEventListener('pointercancel', finishPointer)
    element.removeEventListener('lostpointercapture', finishPointer)
  }

  const finishPointer = (event: PointerEvent) => {
    activePointers.delete(event.pointerId)

    if (activePointers.size === 0) {stopMovement()}
  }

  const endMovement = () => {
    // Wheel zoom emits its own start/end even while a mouse button stays down.
    if (activePointers.size === 0) {stopMovement()}
  }

  const startMovement = () => {
    // The native pointermove handler is attached before Trackball emits start.
    // Consume its new delta immediately, before another event can overwrite it
    // while rendering is busy. The normal render update then has nothing to replay.
    resize()
    element.addEventListener('pointermove', flushMovement)
    element.addEventListener('pointerup', finishPointer)
    element.addEventListener('pointercancel', finishPointer)
    element.addEventListener('lostpointercapture', finishPointer)
  }

  resize()
  // Clipping may retreat the camera after rotation. Recalibrate before the next
  // actual input so that its new depth cannot change the apparent pan speed.
  element.addEventListener('pointerdown', beginPointer)
  controls.addEventListener('start', startMovement)
  controls.addEventListener('end', endMovement)
  const dispose = controls.dispose.bind(controls)

  controls.dispose = () => {
    controls.removeEventListener('start', startMovement)
    controls.removeEventListener('end', endMovement)
    element.removeEventListener('pointerdown', beginPointer)
    activePointers.clear()
    stopMovement()
    dispose()
  }

  return controls
}
