import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n/context'
import { $previewTarget, clearSessionPreviewRegistry, registerDfmReport, type PreviewTarget } from '@/store/preview'
import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'

import { PreviewAttachment } from './preview-attachment'

const reportPath = 'D:\\results\\report.html'
const { readDesktopFileText } = vi.hoisted(() => ({ readDesktopFileText: vi.fn() }))

vi.mock('@/lib/desktop-fs', () => ({ isDesktopFsRemoteMode: () => true, readDesktopFileText }))

function reportTarget(): PreviewTarget {
  return {
    kind: 'file',
    label: 'report.html',
    path: reportPath,
    previewKind: 'html',
    source: reportPath,
    url: 'file:///D:/results/report.html'
  }
}

function renderLink() {
  return render(
    <I18nProvider configClient={null} initialLocale="zh">
      <PreviewAttachment label="Open report.html" source="explicit-link" target={reportPath} variant="link" />
    </I18nProvider>
  )
}

describe('PreviewAttachment link', () => {
  const normalizePreviewTarget = vi.fn(async () => reportTarget())
  const openPreviewInBrowser = vi.fn(async () => undefined)

  beforeEach(() => {
    $previewTarget.set(null)
    window.localStorage.clear()
    clearSessionPreviewRegistry()
    $activeSessionId.set('runtime-1')
    $selectedStoredSessionId.set('stored-1')
    readDesktopFileText.mockClear()
    normalizePreviewTarget.mockClear()
    openPreviewInBrowser.mockClear()

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { normalizePreviewTarget, openPreviewInBrowser }
    })
  })

  afterEach(() => {
    cleanup()
    $previewTarget.set(null)
    window.localStorage.clear()
    clearSessionPreviewRegistry()
    $activeSessionId.set(null)
    $selectedStoredSessionId.set(null)
    vi.restoreAllMocks()
  })

  it('opens the report in the internal Preview tab on a normal click', async () => {
    renderLink()

    fireEvent.click(screen.getByRole('link', { name: 'Open report.html' }))

    await waitFor(() => {
      expect($previewTarget.get()).toEqual({ ...reportTarget(), renderMode: 'preview' })
    })
    expect(openPreviewInBrowser).not.toHaveBeenCalled()
  })

  it('opens the report externally only on a modifier click', async () => {
    renderLink()

    fireEvent.click(screen.getByRole('link', { name: 'Open report.html' }), { ctrlKey: true })

    await waitFor(() => {
      expect(openPreviewInBrowser).toHaveBeenCalledWith('file:///D:/results/report.html')
    })
    expect($previewTarget.get()).toBeNull()
  })

  it('retains structured report ownership without downloading large HTML as text', async () => {
    registerDfmReport('stored-1', { path: reportPath, projectId: 'dfm-1', runId: 'run-1' }, reportTarget())
    renderLink()
    fireEvent.click(screen.getByRole('link', { name: 'Open report.html' }))
    await waitFor(() =>
      expect($previewTarget.get()?.dfmReport).toEqual({
        sessionId: 'stored-1',
        path: reportPath,
        projectId: 'dfm-1',
        runId: 'run-1'
      })
    )
    expect(readDesktopFileText).not.toHaveBeenCalled()
    expect(openPreviewInBrowser).not.toHaveBeenCalled()
  })

  it('does not complete an asynchronous report click into a different session', async () => {
    let finish!: (target: PreviewTarget) => void
    normalizePreviewTarget.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          finish = resolve
        })
    )
    registerDfmReport('stored-1', { path: reportPath, projectId: 'dfm-1', runId: 'run-1' }, reportTarget())
    renderLink()
    fireEvent.click(screen.getByRole('link', { name: 'Open report.html' }))
    act(() => $selectedStoredSessionId.set('stored-2'))
    await act(async () => finish(reportTarget()))
    expect($previewTarget.get()).toBeNull()
    expect(readDesktopFileText).not.toHaveBeenCalled()
  })
})
