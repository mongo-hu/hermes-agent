import { Matrix4, Quaternion, Vector3 } from 'three'
import { describe, expect, it } from 'vitest'

import {
  compassCubeTransform, placeViewMenu, VIEW_CANCEL_RADIUS, VIEW_COMMAND_SECTORS,
  viewCommandAtPoint, viewGestureStarted
} from './dfm-view-gestures'

const center = { x: 200, y: 200 }

const pointAt = (angle: number, radius = 100) => ({
  x: center.x + Math.sin(angle * Math.PI / 180) * radius,
  y: center.y - Math.cos(angle * Math.PI / 180) * radius
})

describe('view gesture sectors', () => {
  it('uses the six approved directions and their unequal angular boundaries without empty slots', () => {
    for (const sector of VIEW_COMMAND_SECTORS) {
      expect(viewCommandAtPoint(pointAt(sector.angle), center)).toBe(sector.command)
      expect(viewCommandAtPoint(pointAt(sector.start + 0.001), center)).toBe(sector.command)
      expect(viewCommandAtPoint(pointAt(sector.end - 0.001), center)).toBe(sector.command)
    }

    for (let angle = 0; angle < 360; angle += 0.1) {
      expect(viewCommandAtPoint(pointAt(angle), center)).not.toBeNull()
    }

    expect(viewCommandAtPoint(pointAt(50), center)).toBe('align')
    expect(viewCommandAtPoint(pointAt(130), center)).toBe('right')
    expect(viewCommandAtPoint(pointAt(225), center)).toBe('fit')
    expect(viewCommandAtPoint(pointAt(310), center)).toBe('left')
  })

  it('starts at eight pixels but cancels at the displayed center through 32 pixels', () => {
    expect(viewGestureStarted(center, { x: 207.9, y: 200 })).toBe(false)
    expect(viewGestureStarted(center, { x: 208, y: 200 })).toBe(true)
    expect(viewCommandAtPoint(center, center)).toBeNull()
    expect(viewCommandAtPoint({ x: center.x, y: center.y - VIEW_CANCEL_RADIUS }, center)).toBeNull()
    expect(viewCommandAtPoint({ x: center.x, y: center.y - VIEW_CANCEL_RADIUS - 0.1 }, center)).toBe('up')
    expect(viewCommandAtPoint({ x: NaN, y: 0 }, center)).toBeNull()
  })

  it('clamps the center without rotating sectors and uses that same center for hit testing', () => {
    const bounds = { left: 0, top: 0, right: 800, bottom: 600 }
    const layout = placeViewMenu({ x: 795, y: 5 }, bounds)!

    expect(layout.position.x + layout.radius).toBeLessThan(bounds.right)
    expect(layout.position.y - layout.radius).toBeGreaterThan(bounds.top)
    expect(viewCommandAtPoint(layout.position, layout.position)).toBeNull()
    expect(viewCommandAtPoint({ x: layout.position.x, y: layout.position.y - 60 }, layout.position)).toBe('up')
    expect(viewCommandAtPoint({ x: layout.position.x + 60, y: layout.position.y }, layout.position)).toBe('right')
    expect(placeViewMenu(center, bounds)?.position).toEqual(center)
  })

  it('shrinks within a small viewer and declines a menu that cannot remain readable', () => {
    expect(placeViewMenu({ x: 125, y: 125 }, { left: 0, top: 0, right: 250, bottom: 250 })?.radius).toBe(117)
    expect(placeViewMenu(center, { left: 0, top: 0, right: 180, bottom: 180 })).toBeNull()
  })
})

describe('passive compass orientation', () => {
  it.each([
    { eye: [0, -1, 0], up: [0, 0, 1], face: [0, 0, 1] },
    { eye: [0, 0, 1], up: [0, 1, 0], face: [0, -1, 0] },
    { eye: [1, 0, 0], up: [0, 0, 1], face: [1, 0, 0] },
    { eye: [-1, 0, 0], up: [0, 0, 1], face: [-1, 0, 0] },
    { eye: [0, 0, -1], up: [0, -1, 0], face: [0, 1, 0] }
  ])('shows the corresponding model face for camera at $eye', ({ eye, up, face }) => {
    const rotation = new Matrix4().lookAt(new Vector3(...eye), new Vector3(), new Vector3(...up))
    const quaternion = new Quaternion().setFromRotationMatrix(rotation)
    const css = compassCubeTransform(quaternion)
    const matrix = new Matrix4().fromArray(css.slice(9, -1).split(',').map(Number))
    const facing = new Vector3(...face).transformDirection(matrix)

    expect(facing.x).toBeCloseTo(0)
    expect(facing.y).toBeCloseTo(0)
    expect(facing.z).toBeCloseTo(1)
  })

  it('normalizes camera quaternions and never emits an invalid CSS matrix', () => {
    const value = { x: 0.2, y: 0.3, z: -0.4, w: 0.7 }
    expect(compassCubeTransform(value)).toBe(compassCubeTransform({ x: 0.4, y: 0.6, z: -0.8, w: 1.4 }))
    expect(compassCubeTransform({ x: 0, y: 0, z: 0, w: 0 })).toBe('none')
  })
})
