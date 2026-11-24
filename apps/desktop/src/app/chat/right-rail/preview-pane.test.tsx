import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'
import { I18nProvider } from '@/i18n/context'

import { PreviewPane } from './preview-pane'

describe('PreviewPane console state', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(Date.now()), 0)
    )
    vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
  })

  afterEach(() => {
    cleanup()
    $connection.set(null)
    vi.unstubAllGlobals()
  })

  it('reloads a local HTML report when the workbench requests it', () => {
    const target = {
      kind: 'file' as const, label: 'report.html', path: 'D:/reports/report.html', previewKind: 'html' as const,
      renderMode: 'preview' as const, source: 'D:/reports/report.html', url: 'file:///D:/reports/report.html'
    }
    const { container, rerender } = render(<PreviewPane embedded reloadRequest={0} target={target} />)
    const webview = container.querySelector('webview')!
    const reloadIgnoringCache = vi.fn()
    Object.assign(webview, { reloadIgnoringCache })

    rerender(<PreviewPane embedded reloadRequest={1} target={target} />)
    expect(reloadIgnoringCache).toHaveBeenCalledTimes(1)
    expect(container.querySelector('webview')).toBe(webview)
    rerender(<PreviewPane embedded reloadRequest={1} target={target} />)
    expect(reloadIgnoringCache).toHaveBeenCalledTimes(1)
  })

  it('loads an HTML report directly and retries a missing file without replacing the webview', () => {
    const readFileText = vi.fn()
    $connection.set({ mode: 'remote' } as never)
    vi.stubGlobal('window', { ...window, hermesDesktop: { readFileText } })
    const url = 'file:///D:/reports/report.html'
    const { container } = render(
      <I18nProvider configClient={null} initialLocale="en">
        <PreviewPane embedded target={{
          kind: 'file', label: 'report.html', path: 'D:/reports/report.html', previewKind: 'html',
          renderMode: 'preview', source: 'D:/reports/report.html', url
        }} />
      </I18nProvider>
    )
    const webview = container.querySelector('webview')!
    const reloadIgnoringCache = vi.fn()
    Object.assign(webview, { reloadIgnoringCache })
    expect(webview.getAttribute('src')).toBe(url)
    expect(webview.getAttribute('webpreferences')).toBe('contextIsolation=yes,nodeIntegration=no,sandbox=yes')
    expect(readFileText).not.toHaveBeenCalled()
    act(() => webview.dispatchEvent(Object.assign(new Event('did-fail-load'), {
      errorCode: -6, errorDescription: 'ERR_FILE_NOT_FOUND', validatedURL: url
    })))
    expect(screen.getByText('ERR_FILE_NOT_FOUND')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(reloadIgnoringCache).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('ERR_FILE_NOT_FOUND')).toBeNull()
    expect(container.querySelector('webview')).toBe(webview)
    expect(readFileText).not.toHaveBeenCalled()
  })

  it('does not watch backend-only remote filesystem previews locally', () => {
    const watchPreviewFile = vi.fn(async () => ({ id: 'watch-1', path: '/remote/file.txt' }))
    const onPreviewFileChanged = vi.fn(() => vi.fn())
    $connection.set({ mode: 'remote' } as never)
    vi.stubGlobal('window', {
      ...window,
      hermesDesktop: {
        onPreviewFileChanged,
        watchPreviewFile
      }
    })

    render(
      <PreviewPane
        setTitlebarToolGroup={vi.fn()}
        target={{
          kind: 'file',
          label: 'file.txt',
          path: '/remote/file.txt',
          previewKind: 'text',
          source: '/remote/file.txt',
          url: 'file:///remote/file.txt'
        }}
      />
    )

    expect(watchPreviewFile).not.toHaveBeenCalled()
    expect(onPreviewFileChanged).not.toHaveBeenCalled()
  })

  it('does not rebuild the pane titlebar group for streamed console logs', () => {
    const setTitlebarToolGroup = vi.fn()

    const rendered = render(
      <PreviewPane
        setTitlebarToolGroup={setTitlebarToolGroup}
        target={{
          kind: 'url',
          label: 'Preview',
          source: 'http://localhost:5174',
          url: 'http://localhost:5174'
        }}
      />
    )

    const initialCalls = setTitlebarToolGroup.mock.calls.length
    const webview = rendered.container.querySelector('webview')

    expect(webview).toBeInstanceOf(HTMLElement)

    act(() => {
      webview?.dispatchEvent(
        Object.assign(new Event('console-message'), {
          level: 0,
          message: 'streamed log line',
          sourceId: 'http://localhost:5174/src/main.tsx'
        })
      )
    })

    expect(setTitlebarToolGroup).toHaveBeenCalledTimes(initialCalls)
  })
})
