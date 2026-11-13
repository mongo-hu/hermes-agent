import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Intro } from './intro'

afterEach(cleanup)

describe('DFM analysis intro', () => {
  it('presents the focused DFM workflow and opens the model picker', () => {
    const onStartAnalysis = vi.fn()

    render(<Intro onStartAnalysis={onStartAnalysis} />)

    expect(screen.getByRole('heading', { name: '新建 DFM 分析' })).toBeTruthy()
    expect(screen.getByText('导入 3D 模型')).toBeTruthy()
    expect(screen.getByText('补充工艺参数')).toBeTruthy()
    expect(screen.getByText('审阅并交付')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: '选择 STP 模型并开始' }))

    expect(onStartAnalysis).toHaveBeenCalledOnce()
  })
})
