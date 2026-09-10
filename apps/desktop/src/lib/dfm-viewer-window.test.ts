import { beforeEach, describe, expect, it, vi } from 'vitest'

import { openDfmViewerWindow } from './dfm-viewer-window'

describe('openDfmViewerWindow', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        openDfmViewerWindow: vi.fn(async () => ({ ok: true }))
      }
    })
  })

  it('passes the completed run viewer manifest to Electron', async () => {
    await openDfmViewerWindow('C:\\hermes\\runs\\run_1\\artifacts\\dfm_viewer.json')

    expect(window.hermesDesktop.openDfmViewerWindow).toHaveBeenCalledWith(
      'C:\\hermes\\runs\\run_1\\artifacts\\dfm_viewer.json'
    )
  })
})
