import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { DfmViewerTarget } from '@/store/dfm-viewer'

import { DfmWorkbench } from './dfm-workbench'

vi.mock('./dfm-viewer-pane', () => ({
  DfmViewerPane: ({ target }: { target: DfmViewerTarget }) => (
    <div data-manifest={target.manifestPath} data-revision={target.revision} data-testid="model-surface">
      DFM model surface
    </div>
  )
}))

afterEach(cleanup)

describe('DFM workbench', () => {
  it('renders the analysis process and model review simultaneously', () => {
    render(
      <DfmWorkbench
        analysisView={<div>Analysis conversation</div>}
        target={{ manifestPath: 'viewer.json', status: 'completed' }}
      />
    )

    expect(screen.getByRole('region', { name: '分析过程' })).toBeTruthy()
    expect(screen.getByRole('region', { name: '三维模型与问题' })).toBeTruthy()
    expect(screen.getByText('Analysis conversation')).toBeTruthy()
    expect(screen.getByText('DFM model surface')).toBeTruthy()
    expect(screen.queryByRole('tab', { name: '分析过程' })).toBeNull()
  })

  it('keeps the model and conversation while reflecting a remote failure and later review recovery', () => {
    const target: DfmViewerTarget = {
      manifestPath: 'viewer.json', projectId: 'dfm_1', revision: 7, status: 'completed'
    }
    const analysisView = <div>Analysis conversation</div>
    const { rerender } = render(<DfmWorkbench analysisView={analysisView} target={target} />)
    const model = screen.getByTestId('model-surface')
    const conversation = screen.getByText('Analysis conversation')

    rerender(<DfmWorkbench analysisView={analysisView} target={{
      ...target, failure: { code: 'geometry_protocol_invalid', message: '几何会话恢复失败。' }
    }} />)

    const status = screen.getByRole('status', { name: 'DFM 分析状态' })
    expect(status.textContent).toBe('分析未完成')
    expect(status.title).toBe('几何会话恢复失败。')
    expect(screen.queryByText('分析完成')).toBeNull()
    expect(screen.getByTestId('model-surface')).toBe(model)
    expect(model.getAttribute('data-manifest')).toBe('viewer.json')
    expect(screen.getByText('Analysis conversation')).toBe(conversation)

    rerender(<DfmWorkbench analysisView={analysisView} target={{ ...target, revision: 8, status: 'discovery' }} />)

    expect(status.textContent).toBe('特征审核')
    expect(status.title).toBe('')
    expect(model.getAttribute('data-revision')).toBe('8')
    expect(screen.getByText('Analysis conversation')).toBe(conversation)
  })
})
