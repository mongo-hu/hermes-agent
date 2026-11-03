export type DfmViewCommand = 'up' | 'align' | 'right' | 'down' | 'fit' | 'left'
export interface ViewPoint { x: number; y: number }
export interface ViewBounds { left: number; top: number; right: number; bottom: number }
export interface ViewQuaternion { x: number; y: number; z: number; w: number }

export const VIEW_DRAG_THRESHOLD = 8
export const VIEW_CANCEL_RADIUS = 32
export const VIEW_MENU_RADIUS = 144
const MIN_MENU_RADIUS = 112

/** Clockwise from screen-up. Boundaries are the midpoints of adjacent commands. */
export const VIEW_COMMAND_SECTORS = [
  { command: 'up', label: '上视', angle: 0, start: -45, end: 22.5, face: 'top' },
  { command: 'align', label: '摆正视图', angle: 45, start: 22.5, end: 67.5 },
  { command: 'right', label: '右视', angle: 90, start: 67.5, end: 135, face: 'right' },
  { command: 'down', label: '下视', angle: 180, start: 135, end: 202.5, face: 'bottom' },
  { command: 'fit', label: '适合窗口', angle: 225, start: 202.5, end: 247.5 },
  { command: 'left', label: '左视', angle: 270, start: 247.5, end: 315, face: 'left' }
] as const

export function viewGestureStarted(origin: ViewPoint, pointer: ViewPoint): boolean {
  return Math.hypot(pointer.x - origin.x, pointer.y - origin.y) >= VIEW_DRAG_THRESHOLD
}

export function viewCommandAtPoint(pointer: ViewPoint, center: ViewPoint): DfmViewCommand | null {
  const dx = pointer.x - center.x
  const dy = pointer.y - center.y

  if (!Number.isFinite(dx) || !Number.isFinite(dy) || Math.hypot(dx, dy) <= VIEW_CANCEL_RADIUS) {
    return null
  }

  const angle = (Math.atan2(dx, -dy) * 180 / Math.PI + 360) % 360

  return VIEW_COMMAND_SECTORS.find(sector =>
    (angle - sector.start + 360) % 360 < sector.end - sector.start
  )?.command ?? null
}

/**
 * Translate, never rotate or mirror, at an edge. The returned position MUST be
 * used for both rendering and viewCommandAtPoint, in the same coordinate space.
 * A very small viewport keeps its normal toolbar rather than showing a clipped menu.
 */
export function placeViewMenu(origin: ViewPoint, bounds: ViewBounds): { position: ViewPoint; radius: number } | null {
  const padding = 8

  const radius = Math.min(VIEW_MENU_RADIUS,
    (bounds.right - bounds.left) / 2 - padding,
    (bounds.bottom - bounds.top) / 2 - padding)

  if (![origin.x, origin.y, bounds.left, bounds.top, bounds.right, bounds.bottom].every(Number.isFinite) ||
    radius < MIN_MENU_RADIUS) {
    return null
  }

  return {
    position: {
      x: Math.max(bounds.left + radius + padding, Math.min(origin.x, bounds.right - radius - padding)),
      y: Math.max(bounds.top + radius + padding, Math.min(origin.y, bounds.bottom - radius - padding))
    },
    radius
  }
}

/** World Z-up -> inverse camera rotation -> CSS screen axes (Y points down). */
export function compassCubeTransform(quaternion: ViewQuaternion): string {
  const length = Math.hypot(quaternion.x, quaternion.y, quaternion.z, quaternion.w)

  if (!Number.isFinite(length) || length === 0) {
    return 'none'
  }

  const x = quaternion.x / length
  const y = quaternion.y / length
  const z = quaternion.z / length
  const w = quaternion.w / length
  const r11 = 1 - 2 * (y * y + z * z)
  const r12 = 2 * (x * y - z * w)
  const r13 = 2 * (x * z + y * w)
  const r21 = 2 * (x * y + z * w)
  const r22 = 1 - 2 * (x * x + z * z)
  const r23 = 2 * (y * z - x * w)
  const r31 = 2 * (x * z - y * w)
  const r32 = 2 * (y * z + x * w)
  const r33 = 1 - 2 * (x * x + y * y)
  // Cube front is world -Y, top is +Z, right is +X. CSS matrix is column-major.
  const matrix = [r11, -r12, r13, 0, -r31, r32, -r33, 0, -r21, r22, -r23, 0, 0, 0, 0, 1]

  return `matrix3d(${matrix.map(value => Number(value.toFixed(10))).join(',')})`
}
