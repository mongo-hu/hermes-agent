import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DfmViewerTarget } from '@/store/dfm-viewer'
import { $previewTarget, clearSessionPreviewRegistry, registerDfmReport } from '@/store/preview'
import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'

import { DfmWorkbench } from './dfm-workbench'

vi.mock('./dfm-viewer-pane', () => ({
  DfmViewerPane: ({ target }: { target: DfmViewerTarget }) => (
    <div data-manifest={target.manifestPath} data-revision={target.revision} data-testid="model-surface">
      DFM model surface
    </div>
  )
}))

vi.mock('../chat/right-rail/preview-pane', () => ({
  PreviewPane: ({ target, reloadRequest }: { target: { url: string }; reloadRequest: number }) => (
    <div data-testid="html-report" data-url={target.url} data-reload={reloadRequest}>
      HTML report
    </div>
  )
}))

beforeEach(() => {
  clearSessionPreviewRegistry()
  $selectedStoredSessionId.set('stored-1')
  $activeSessionId.set('runtime-1')
})

afterEach(() => {
  cleanup()
  clearSessionPreviewRegistry()
  $selectedStoredSessionId.set(null)
  $activeSessionId.set(null)
})

function registerReport(sessionId = 'stored-1', runId = 'run-1') {
  const path = `D:/project/${runId}/report.html`
  return registerDfmReport(
    sessionId,
    { path, projectId: 'dfm-1', runId },
    {
      kind: 'file',
      label: 'report.html',
      path,
      previewKind: 'html',
      source: path,
      url: `file:///${path}`
    }
  )
}

describe('DFM workbench', () => {
  it('registers reports without stealing focus and preserves the mounted model across report switches', () => {
    const target: DfmViewerTarget = {
      manifestPath: 'viewer.json',
      projectId: 'dfm-1',
      runId: 'run-1',
      status: 'completed'
    }
    render(<DfmWorkbench analysisView={<div>Conversation</div>} target={target} />)
    const model = screen.getByTestId('model-surface')
    act(() => {
      registerReport()
    })
    expect($previewTarget.get()).toBeNull()
    expect(screen.queryByTestId('html-report')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '查看报告' }))
    const report = screen.getByTestId('html-report')
    const conversation = screen.getByText('Conversation')
    expect(report.getAttribute('data-url')).toBe('file:///D:/project/run-1/report.html')
    expect(conversation.closest('[hidden]')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '并排查看' }))
    expect(conversation.closest('[hidden]')).toBeNull()
    expect(screen.getByTestId('html-report')).toBe(report)
    fireEvent.click(screen.getByRole('button', { name: '展开阅读' }))
    expect(conversation.closest('[hidden]')).not.toBeNull()
    expect(screen.getByTestId('html-report')).toBe(report)
    expect(screen.getByTestId('model-surface')).toBe(model)
    expect(model.closest('[hidden]')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '重新加载报告' }))
    expect(report.getAttribute('data-reload')).toBe('1')
    fireEvent.click(screen.getByRole('tab', { name: '模型' }))
    expect(conversation.closest('[hidden]')).toBeNull()
    expect(model.closest('[hidden]')).toBeNull()
    expect(screen.getByTestId('html-report')).toBe(report)
    expect(report.closest('[hidden]')).not.toBeNull()
    fireEvent.click(screen.getByRole('tab', { name: '报告' }))
    expect(screen.getByTestId('html-report')).toBe(report)
  })

  it('does not expose another session or run report beside the current model', () => {
    registerReport('stored-other')
    registerReport('stored-1', 'old-run')
    render(
      <DfmWorkbench
        analysisView={<div />}
        target={{ manifestPath: 'viewer.json', projectId: 'dfm-1', runId: 'new-run', status: 'completed' }}
      />
    )
    expect(screen.queryByRole('button', { name: '查看报告' })).toBeNull()
    expect(screen.getByRole('tab', { name: '报告' }).getAttribute('disabled')).not.toBeNull()
  })

  it('offers a report even when its model preview is unavailable', () => {
    registerReport()
    render(<DfmWorkbench analysisView={<div>Conversation</div>} target={null} />)
    fireEvent.click(screen.getByRole('button', { name: '查看报告' }))
    expect(screen.getByTestId('html-report')).toBeTruthy()
    expect(screen.getByText('Conversation')).toBeTruthy()
  })
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
