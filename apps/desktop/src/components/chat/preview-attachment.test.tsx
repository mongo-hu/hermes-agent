import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n/context'
import { $previewTarget, clearSessionPreviewRegistry, type PreviewTarget } from '@/store/preview'

import { PreviewAttachment } from './preview-attachment'

const reportPath = 'D:\\results\\report.html'

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
})
