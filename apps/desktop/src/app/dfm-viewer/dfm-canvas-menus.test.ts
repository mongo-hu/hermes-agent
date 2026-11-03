import { afterEach, describe, expect, it, vi } from 'vitest'

import { attachCanvasMenus, type CanvasMenuController, type CanvasViewMenu } from './dfm-canvas-menus'

let controller: CanvasMenuController | null = null
afterEach(() => { controller?.dispose(); controller = null; document.body.replaceChildren() })

function setup({ model = false, faceIndex = null, width = 800, height = 600 }: {
  model?: boolean; faceIndex?: number | null; width?: number; height?: number
} = {}) {
  const canvas = document.createElement('canvas')
  document.body.append(canvas)
  canvas.getBoundingClientRect = () => ({ x: 40, y: 60, left: 40, top: 60, right: 40 + width,
    bottom: 60 + height, width, height, toJSON: () => ({}) })
  const captured = new Set<number>()
  canvas.setPointerCapture = vi.fn(id => { captured.add(id) })
  canvas.hasPointerCapture = id => captured.has(id)
  canvas.releasePointerCapture = vi.fn(id => {
    captured.delete(id)
    pointer(canvas, 'lostpointercapture', 0, 0, { pointerId: id })
  })

  const options = {
    hitTest: vi.fn(() => ({ model, faceIndex })),
    onMenu: vi.fn<(menu: CanvasViewMenu | null) => void>(),
    onObject: vi.fn(), onCommand: vi.fn(), onStart: vi.fn(), blocked: vi.fn(() => false)
  }

  controller = attachCanvasMenus(canvas, options)
  const lastMenu = () => options.onMenu.mock.calls.at(-1)?.[0] ?? null
  const down = (x = 400, y = 300, extra = {}) => pointer(canvas, 'pointerdown', x, y, extra)
  const move = (x: number, y: number, extra = {}) => pointer(window, 'pointermove', x, y, extra)
  const up = (x: number, y: number, extra = {}) => pointer(window, 'pointerup', x, y, { buttons: 0, ...extra })

  return { canvas, options, lastMenu, down, move, up }
}

function pointer(target: EventTarget, type: string, x: number, y: number, extra: {
  button?: number; buttons?: number; pointerId?: number
} = {}) {
  // jsdom's MouseEvent provides real DOM dispatch; pointer identity is added explicitly.
  const event = new MouseEvent(type, { bubbles: true, cancelable: true,
    clientX: x + 40, clientY: y + 60, button: 2, buttons: 2, ...extra })

  Object.defineProperty(event, 'pointerId', { value: extra.pointerId ?? 7 })
  target.dispatchEvent(event)

  return event
}

describe('right-pointer canvas menus', () => {
  it('leaves left/middle input untouched and suppresses only the canvas native context menu', () => {
    const { canvas, options, down, move, up } = setup()
    expect(down(400, 300, { button: 0, buttons: 1 }).defaultPrevented).toBe(false)
    expect(down(400, 300, { button: 1, buttons: 4 }).defaultPrevented).toBe(false)
    move(450, 300, { buttons: 1 })
    up(450, 300, { button: 0 })
    expect(options.onStart).not.toHaveBeenCalled()
    expect(options.hitTest).not.toHaveBeenCalled()
    expect(options.onCommand).not.toHaveBeenCalled()
    const event = new MouseEvent('contextmenu', { cancelable: true })
    canvas.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
  })

  it('opens at eight pixels, keeps the 32-pixel center cancelled, and executes once on release', () => {
    const { canvas, down, move, up, lastMenu, options } = setup()
    expect(down().defaultPrevented).toBe(true)
    move(407, 300)
    expect(lastMenu()).toBeNull()
    move(408, 300)
    expect(lastMenu()?.activeCommand).toBeNull()
    move(432, 300)
    expect(lastMenu()?.activeCommand).toBeNull()
    move(433, 300)
    expect(lastMenu()?.activeCommand).toBe('right')
    expect(options.onCommand).not.toHaveBeenCalled()
    up(433, 300)
    expect(options.onCommand).toHaveBeenCalledExactlyOnceWith('right')
    expect(lastMenu()).toBeNull()
    expect(canvas.releasePointerCapture).toHaveBeenCalledWith(7)
    up(433, 300)
    expect(options.onCommand).toHaveBeenCalledTimes(1)
  })

  it('uses the latest release point instead of stale highlighted direction, including release at center', () => {
    const { down, move, up, options } = setup()
    down()
    move(470, 300)
    up(400, 220)
    expect(options.onCommand).toHaveBeenCalledExactlyOnceWith('up')
    options.onCommand.mockClear()
    down()
    move(470, 300)
    up(400, 300)
    expect(options.onCommand).not.toHaveBeenCalled()
  })

  it('locks a raw-model start even without a mapped face and never changes into a view gesture', () => {
    const { options, down, move, up, lastMenu } = setup({ model: true })
    down()
    options.hitTest.mockReturnValue({ model: false, faceIndex: null })
    move(480, 300)
    up(480, 300)
    expect(options.hitTest).toHaveBeenCalledTimes(1)
    expect(lastMenu()).toBeNull()
    expect(options.onCommand).not.toHaveBeenCalled()
    expect(options.onObject).not.toHaveBeenCalled()
  })

  it('opens the initial model object only for a short click, never for a drag that returns', () => {
    const { down, move, up, options } = setup({ model: true, faceIndex: 12 })
    down()
    up(403, 301)
    expect(options.onObject).toHaveBeenCalledExactlyOnceWith(12, { x: 403, y: 301 })
    options.onObject.mockClear()
    down()
    move(410, 300)
    move(400, 300)
    up(400, 300)
    expect(options.onObject).not.toHaveBeenCalled()
    down()
    up(408, 300)
    expect(options.onObject).not.toHaveBeenCalled()
  })

  it('keeps a blank start as a view gesture and shows nothing for a blank short click', () => {
    const { down, move, up, options } = setup()
    down()
    up(400, 300)
    expect(options.onObject).not.toHaveBeenCalled()
    expect(options.onMenu.mock.calls.filter(([menu]) => menu)).toHaveLength(0)
    down()
    options.hitTest.mockReturnValue({ model: true, faceIndex: 12 })
    move(470, 300)
    up(470, 300)
    expect(options.onCommand).toHaveBeenCalledExactlyOnceWith('right')
  })

  it.each(['escape', 'blur', 'outside', 'pointercancel', 'lostcapture', 'external', 'buttons', 'blocked'])(
    'cancels on %s and prevents any subsequent release action', reason => {
      const { canvas, down, move, up, lastMenu, options } = setup()
      down()
      move(470, 300)

      if (reason === 'escape') {window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))}

      if (reason === 'blur') {window.dispatchEvent(new Event('blur'))}

      if (reason === 'outside') {move(801, 300)}

      if (reason === 'pointercancel') {pointer(window, 'pointercancel', 470, 300)}

      if (reason === 'lostcapture') {pointer(canvas, 'lostpointercapture', 470, 300)}

      if (reason === 'external') {controller!.cancel()}

      if (reason === 'buttons') {move(470, 300, { buttons: 0 })}

      if (reason === 'blocked') {options.blocked.mockReturnValue(true); move(470, 300)}
      expect(lastMenu()).toBeNull()
      expect(canvas.releasePointerCapture).toHaveBeenCalledWith(7)
      options.blocked.mockReturnValue(false)
      move(470, 300)
      up(470, 300)
      expect(options.onCommand).not.toHaveBeenCalled()
      expect(options.onObject).not.toHaveBeenCalled()
    }
  )

  it('ignores other pointers and cancels an outside release even without an outside move', () => {
    const { down, move, up, lastMenu, options } = setup()
    down()
    move(470, 300)
    move(400, 220, { pointerId: 99 })
    pointer(window, 'pointercancel', 470, 300, { pointerId: 99 })
    up(400, 220, { pointerId: 99 })
    expect(lastMenu()?.activeCommand).toBe('right')
    up(801, 300)
    expect(options.onCommand).not.toHaveBeenCalled()
    expect(lastMenu()).toBeNull()
  })

  it('requires entering a clamped menu center before edge gestures can execute', () => {
    const { down, move, up, lastMenu, options } = setup()
    down(5, 5)
    move(13, 5)
    const placed = lastMenu()!
    expect(placed.position).toEqual({ x: 152, y: 152 })
    expect(placed.needsCentering).toBe(true)
    expect(placed.activeCommand).toBeNull()
    up(13, 5)
    expect(options.onCommand).not.toHaveBeenCalled()
    down(5, 5)
    move(60, 5)
    expect(lastMenu()?.activeCommand).toBeNull()
    move(152, 152)
    expect(lastMenu()?.needsCentering).toBe(false)
    move(152, 100)
    expect(lastMenu()?.activeCommand).toBe('up')
    up(152, 100)
    expect(options.onCommand).toHaveBeenCalledExactlyOnceWith('up')
  })

  it('handles unreadably small hosts and removes all listeners/capture on dispose', () => {
    const { canvas, down, move, up, options } = setup({ width: 180, height: 180 })
    down(90, 90)
    move(140, 90)
    up(140, 90)
    expect(options.onMenu.mock.calls.filter(([menu]) => menu)).toHaveLength(0)
    expect(options.onCommand).not.toHaveBeenCalled()
    const starts = options.onStart.mock.calls.length
    controller!.dispose()
    down(90, 90)
    move(140, 90)
    up(140, 90)
    expect(options.onStart).toHaveBeenCalledTimes(starts)
    expect(canvas.hasPointerCapture(7)).toBe(false)
  })
})
