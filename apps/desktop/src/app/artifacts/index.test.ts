import { afterEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'
import type { SessionInfo, SessionMessage } from '@/types/hermes'

import { artifactImageSrc, collectArtifactsForSession, isDfmDeliverableArtifact } from './artifact-utils'

function makeSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    ended_at: null,
    id: 'session-1',
    input_tokens: 0,
    is_active: false,
    last_active: 1000,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    source: null,
    started_at: 1000,
    title: 'Session',
    tool_call_count: 0,
    ...overrides
  }
}

describe('collectArtifactsForSession', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.clearAllMocks()
    $connection.set(null)
  })

  it('indexes plain https links from assistant text', () => {
    const artifacts = collectArtifactsForSession(makeSession(), [
      {
        content: 'Reference: https://example.com/docs/getting-started',
        role: 'assistant',
        timestamp: 2000
      }
    ])

    expect(artifacts).toHaveLength(1)
    expect(artifacts[0]).toMatchObject({
      href: 'https://example.com/docs/getting-started',
      kind: 'link',
      value: 'https://example.com/docs/getting-started'
    })
  })

  it('indexes http links present in tool JSON payloads', () => {
    const messages: SessionMessage[] = [
      {
        content: JSON.stringify({ source_url: 'https://example.com/changelog/latest' }),
        role: 'tool',
        timestamp: 3000
      }
    ]

    const artifacts = collectArtifactsForSession(makeSession({ id: 'session-2' }), messages)

    expect(artifacts).toHaveLength(1)
    expect(artifacts[0]).toMatchObject({
      href: 'https://example.com/changelog/latest',
      kind: 'link',
      value: 'https://example.com/changelog/latest'
    })
  })

  it('indexes Windows DFM artifact paths from tool JSON payloads', () => {
    const path = 'C:\\Users\\lenovo\\.hermes\\workspace\\dfm\\projects\\dfm_1\\artifacts\\run.json'

    const artifacts = collectArtifactsForSession(makeSession({ id: 'dfm-session' }), [
      {
        content: JSON.stringify({ run: { artifacts: [{ path, relative_path: 'artifacts/run.json' }] } }),
        role: 'tool',
        timestamp: 4000
      }
    ])

    expect(artifacts).toHaveLength(1)
    expect(artifacts[0]).toMatchObject({
      href: 'file:///C:/Users/lenovo/.hermes/workspace/dfm/projects/dfm_1/artifacts/run.json',
      kind: 'file',
      label: 'run.json',
      value: path
    })
  })

  it('indexes generated DFM PowerPoint reports as files', () => {
    const path = 'C:\\Users\\lenovo\\.hermes\\workspace\\dfm\\projects\\dfm_1\\artifacts\\dfm_report.pptx'

    const artifacts = collectArtifactsForSession(makeSession({ id: 'dfm-pptx' }), [
      {
        content: JSON.stringify({ artifact: { kind: 'report_presentation', path } }),
        role: 'tool',
        timestamp: 4500
      }
    ])

    expect(artifacts).toHaveLength(1)
    expect(artifacts[0]).toMatchObject({
      kind: 'file',
      label: 'dfm_report.pptx',
      value: path
    })
  })

  it('indexes UNC artifact paths from tool JSON payloads', () => {
    const path = '\\\\analysis-server\\dfm-results\\mold\\report.json'

    const artifacts = collectArtifactsForSession(makeSession(), [
      { content: JSON.stringify({ artifact: { path } }), role: 'tool', timestamp: 5000 }
    ])

    expect(artifacts).toHaveLength(1)
    expect(artifacts[0]).toMatchObject({
      href: 'file://analysis-server/dfm-results/mold/report.json',
      kind: 'file',
      value: path
    })
  })

  it('resolves remote image artifact thumbnails through the desktop fs bridge', async () => {
    const api = vi.fn(async ({ path }: { path: string }) => {
      if (path.startsWith('/api/fs/read-data-url?')) {
        return { dataUrl: 'data:image/jpeg;base64,cmVtb3Rl' }
      }

      throw new Error(`unexpected path ${path}`)
    })

    vi.stubGlobal('window', { hermesDesktop: { api } })
    $connection.set({ baseUrl: 'https://gw', mode: 'remote', token: 'secret' } as never)

    const path = '/Users/me/.hermes/skills/work-esab/references/images/manual-step03.jpeg'
    const downloadHref = `https://gw/api/files/download?path=${encodeURIComponent(path)}&token=secret`

    await expect(artifactImageSrc(path, downloadHref)).resolves.toBe('data:image/jpeg;base64,cmVtb3Rl')

    expect(api).toHaveBeenCalledWith({
      path: '/api/fs/read-data-url?path=%2FUsers%2Fme%2F.hermes%2Fskills%2Fwork-esab%2Freferences%2Fimages%2Fmanual-step03.jpeg'
    })
  })

  it('resolves local image thumbnails through the desktop fs bridge', async () => {
    const readFileDataUrl = vi.fn(async () => 'data:image/png;base64,bG9jYWw=')

    vi.stubGlobal('window', { hermesDesktop: { readFileDataUrl } })

    await expect(artifactImageSrc('C:\\dfm-results\\part\\report.png')).resolves.toBe(
      'data:image/png;base64,bG9jYWw='
    )
    expect(readFileDataUrl).toHaveBeenCalledWith('C:\\dfm-results\\part\\report.png')
  })

  it('keeps generated DFM outputs and rejects source or QA files', () => {
    expect(
      isDfmDeliverableArtifact({
        value: 'C:\\Users\\lenovo\\.hermes\\workspace\\dfm\\projects\\dfm_1\\artifacts\\dfm_report.pdf'
      })
    ).toBe(true)
    expect(isDfmDeliverableArtifact({ value: '\\\\analysis-server\\dfm-results\\mold\\report.json' })).toBe(true)
    expect(
      isDfmDeliverableArtifact({
        value:
          'C:\\Users\\96155\\AppData\\Local\\hermes\\workspace\\dfm\\projects\\dfm_1\\runs\\run_1\\artifacts\\report.html'
      })
    ).toBe(true)
    expect(isDfmDeliverableArtifact({ value: 'C:\\Users\\96155\\Desktop\\DFM分析报告_产品分析文件.html' })).toBe(true)
    expect(
      isDfmDeliverableArtifact({ value: 'D:\\work\\DFM-agent\\hermes-agent\\apps\\desktop\\src\\app.tsx' })
    ).toBe(false)
    expect(
      isDfmDeliverableArtifact({ value: 'D:\\work\\DFM-agent\\hermes-agent\\output\\playwright\\dfm-home.png' })
    ).toBe(false)
  })
})
