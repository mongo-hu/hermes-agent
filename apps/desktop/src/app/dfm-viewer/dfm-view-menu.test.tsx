import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { VIEW_COMMAND_SECTORS, viewCommandAtPoint } from './dfm-view-gestures'
import { DfmViewCompass, type DfmViewCompassHandle, DfmViewMenu } from './dfm-view-menu'

afterEach(cleanup)

describe('view direction presenter', () => {
  it('renders every command at its visual sector center with four identical cube poses', () => {
    const { container } = render(<DfmViewMenu activeCommand="right"
      onCancel={vi.fn()} onCommand={vi.fn()} position={{ x: 200, y: 200 }} />)

    const buttons = container.querySelectorAll<HTMLButtonElement>('[data-view-command]')

    expect(buttons).toHaveLength(6)

    for (const sector of VIEW_COMMAND_SECTORS) {
      const button = screen.getByRole('button', { name: sector.label })
      const label = button.querySelector<HTMLElement>('.dfm-view-sector-label')!
      const point = { x: parseFloat(label.style.left) * 2.88, y: parseFloat(label.style.top) * 2.88 }

      expect(viewCommandAtPoint(point, { x: 144, y: 144 })).toBe(sector.command)
      expect(button.getAttribute('aria-pressed')).toBe(String(sector.command === 'right'))
    }

    const cubes = container.querySelectorAll<HTMLElement>('.dfm-view-cube')
    expect(cubes).toHaveLength(4)
    expect(Array.from(cubes, cube => cube.className)).toEqual(Array(4).fill('dfm-view-cube'))
    expect(Array.from(cubes, cube => cube.style.transform)).toEqual(Array(4).fill(''))
    expect(Array.from(container.querySelectorAll('.dfm-view-cube-space'), cube => cube.getAttribute('data-face')))
      .toEqual(['top', 'right', 'bottom', 'left'])
    expect(container.querySelectorAll('.dfm-view-cube-face')).toHaveLength(24)
  })

  it('supports button activation and Escape without executing on right-button press or release', () => {
    const onCommand = vi.fn()
    const onCancel = vi.fn()
    render(<DfmViewMenu activeCommand="up" onCancel={onCancel}
      onCommand={onCommand} position={{ x: 160, y: 160 }} />)
    const up = screen.getByRole('button', { name: '上视' })

    fireEvent.pointerDown(up, { button: 2 })
    fireEvent.pointerUp(up, { button: 2 })
    fireEvent.click(up, { button: 2 })
    expect(onCommand).not.toHaveBeenCalled()
    // Native keyboard button activation dispatches a primary click with detail=0.
    fireEvent.click(up, { button: 0, detail: 0 })
    expect(onCommand).toHaveBeenCalledExactlyOnceWith('up')
    fireEvent.keyDown(up, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: '取消视图手势' }))
    expect(onCancel).toHaveBeenCalledTimes(2)
  })

  it('keeps active selection controlled by its host', () => {
    const props = { position: { x: 160, y: 160 }, onCommand: vi.fn(), onCancel: vi.fn() }
    const { rerender } = render(<DfmViewMenu {...props} activeCommand="down" />)
    fireEvent.pointerEnter(screen.getByRole('button', { name: '上视' }))
    expect(screen.getByRole('button', { name: '下视' }).getAttribute('aria-pressed')).toBe('true')
    rerender(<DfmViewMenu {...props} activeCommand={null} />)
    expect(screen.getByRole('button', { name: '下视' }).getAttribute('aria-pressed')).toBe('false')
  })
})

describe('passive compass presenter', () => {
  it('updates camera rotation through a ref without rendering an interactive control', () => {
    const ref = createRef<DfmViewCompassHandle>()
    const { container } = render(<DfmViewCompass ref={ref} />)
    const cube = container.querySelector<HTMLElement>('.dfm-view-cube')!

    expect(screen.getByRole('img', { name: '三维方位提示' })).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
    expect(container.querySelector('[tabindex]')).toBeNull()
    act(() => ref.current!.setCameraQuaternion({ x: 0, y: 0, z: 0, w: 1 }))
    const first = cube.style.transform
    act(() => ref.current!.setCameraQuaternion({ x: Math.SQRT1_2, y: 0, z: 0, w: Math.SQRT1_2 }))
    expect(cube.style.transform).not.toBe(first)
    expect(container.querySelector('.dfm-view-cube')).toBe(cube)
  })
})
