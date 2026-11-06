import {
  type DfmViewCommand, placeViewMenu, VIEW_CANCEL_RADIUS, viewCommandAtPoint, viewGestureStarted
} from './dfm-view-gestures'

export interface CanvasMenuPoint { x: number; y: number }
export interface CanvasViewMenu {
  position: CanvasMenuPoint
  radius: number
  activeCommand: DfmViewCommand | null
  needsCentering?: boolean
}

interface CanvasMenuOptions {
  hitTest(event: PointerEvent): { model: boolean; faceIndex: number | null }
  onMenu(menu: CanvasViewMenu | null): void
  onObject(faceIndex: number, point: CanvasMenuPoint): void
  onCommand(command: DfmViewCommand): void
  onStart(): void
  blocked(): boolean
}

export interface CanvasMenuController {
  cancel(): void
  dispose(): void
}

/** Own only right-button gestures; left selection, wheel and middle pan stay independent. */
export function attachCanvasMenus(canvas: HTMLCanvasElement, options: CanvasMenuOptions): CanvasMenuController {
  let gesture: { id: number; origin: CanvasMenuPoint; model: boolean; faceIndex: number | null;
    dragging: boolean; menu: CanvasViewMenu | null } | null = null

  let disposed = false

  const point = (event: PointerEvent) => {
    const rect = canvas.getBoundingClientRect()

    return { x: event.clientX - rect.left, y: event.clientY - rect.top }
  }

  const inside = (current: CanvasMenuPoint) => {
    const rect = canvas.getBoundingClientRect()

    return current.x >= 0 && current.y >= 0 && current.x < rect.width && current.y < rect.height
  }

  const cancel = () => {
    const id = gesture?.id
    gesture = null

    if (id !== undefined && canvas.hasPointerCapture?.(id)) {
      canvas.releasePointerCapture(id)
    }

    options.onMenu(null)
  }

  const down = (event: PointerEvent) => {
    if (event.button !== 2) {return}
    event.preventDefault()
    event.stopImmediatePropagation()
    cancel()
    options.onStart()

    if (options.blocked()) {return}
    gesture = { id: event.pointerId, origin: point(event), ...options.hitTest(event), dragging: false, menu: null }

    // Keep the gesture owned by its initiating surface when a menu covers the canvas.
    try { canvas.setPointerCapture?.(event.pointerId) } catch { /* Pointer may already have ended. */ }
  }

  const move = (event: PointerEvent) => {
    if (!gesture || event.pointerId !== gesture.id) {return}

    if (!(event.buttons & 2)) {cancel();

 return}

    const current = point(event)

    if (!inside(current) || options.blocked()) {cancel();

 return}

    if (!viewGestureStarted(gesture.origin, current) && !gesture.dragging) {return}
    gesture.dragging = true

    if (gesture.model) {return}

    if (!gesture.menu) {
      const rect = canvas.getBoundingClientRect()
      const placed = placeViewMenu(gesture.origin, { left: 0, top: 0, right: rect.width, bottom: rect.height })

      if (!placed) {cancel();

 return}

      const shifted = Math.hypot(placed.position.x - gesture.origin.x, placed.position.y - gesture.origin.y) > 0.01
      gesture.menu = { ...placed, activeCommand: null, needsCentering: shifted }
    }

    // A shifted menu must be entered through its visible cancel circle first.
    // Unshifted menus are armed immediately, even if the first move is a large one.
    if (Math.hypot(current.x - gesture.menu.position.x, current.y - gesture.menu.position.y) <= VIEW_CANCEL_RADIUS) {
      gesture.menu.needsCentering = false
    }

    gesture.menu.activeCommand = gesture.menu.needsCentering ? null : viewCommandAtPoint(current, gesture.menu.position)
    options.onMenu({ ...gesture.menu })
  }

  const up = (event: PointerEvent) => {
    if (event.button !== 2 || !gesture || event.pointerId !== gesture.id) {return}
    const current = point(event)
    const finished = gesture
    cancel()

    if (options.blocked() || !inside(current)) {return}

    if (finished.dragging || viewGestureStarted(finished.origin, current)) {
      // The final event may arrive without an intervening pointermove.
      const command = finished.menu && !finished.menu.needsCentering
        ? viewCommandAtPoint(current, finished.menu.position) : null

      if (command) {options.onCommand(command)}
    } else if (finished.model && finished.faceIndex != null) {
      options.onObject(finished.faceIndex, current)
    }
  }

  const pointerCancel = (event: PointerEvent) => {
    if (gesture && event.pointerId === gesture.id) {cancel()}
  }

  const key = (event: KeyboardEvent) => {
    if (gesture && event.key === 'Escape') {event.preventDefault(); cancel()}
  }

  const context = (event: MouseEvent) => { event.preventDefault() }
  canvas.addEventListener('pointerdown', down, true)
  canvas.addEventListener('contextmenu', context)
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
  window.addEventListener('pointercancel', pointerCancel)
  canvas.addEventListener('lostpointercapture', pointerCancel)
  window.addEventListener('blur', cancel)
  window.addEventListener('keydown', key)

  const dispose = () => {
    if (disposed) {return}
    disposed = true
    canvas.removeEventListener('pointerdown', down, true)
    canvas.removeEventListener('contextmenu', context)
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
    window.removeEventListener('pointercancel', pointerCancel)
    canvas.removeEventListener('lostpointercapture', pointerCancel)
    window.removeEventListener('blur', cancel)
    window.removeEventListener('keydown', key)
    cancel()
  }

  return { dispose, cancel }
}
