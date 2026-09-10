import { describe, expect, it } from 'vitest'

import { dfmHtmlReportPathFromToolComplete, dfmViewerTargetFromToolComplete } from './dfm-viewer-events'

describe('dfmViewerTargetFromToolComplete', () => {
  it('opens an embedded preview after STEP registration', () => {
    const target = dfmViewerTargetFromToolComplete({
      name: 'dfm_project',
      result: JSON.stringify({
        project_id: 'dfm_1',
        preview: {
          run_id: 'preview_1',
          status: 'ready',
          viewer_manifest: 'C:\\hermes\\dfm_viewer.json'
        },
        viewer_manifest: 'C:\\hermes\\dfm_viewer.json'
      })
    })

    expect(target).toEqual({
      manifestPath: 'C:\\hermes\\dfm_viewer.json',
      projectId: 'dfm_1',
      runId: 'preview_1',
      status: 'preview'
    })
  })

  it('replaces the preview with completed findings', () => {
    const target = dfmViewerTargetFromToolComplete({
      name: 'dfm_analysis',
      project_id: 'dfm_1',
      run_id: 'run_1',
      status: 'succeeded',
      viewer_manifest: 'C:\\hermes\\completed\\dfm_viewer.json'
    })

    expect(target).toEqual({
      manifestPath: 'C:\\hermes\\completed\\dfm_viewer.json',
      projectId: 'dfm_1',
      runId: 'run_1',
      status: 'completed'
    })
  })

  it('ignores unavailable previews and failed analyses', () => {
    expect(
      dfmViewerTargetFromToolComplete({
        name: 'dfm_project',
        result: { preview: { status: 'unavailable' } }
      })
    ).toBeNull()
    expect(
      dfmViewerTargetFromToolComplete({
        name: 'dfm_analysis',
        status: 'failed',
        viewer_manifest: 'C:\\unexpected.json'
      })
    ).toBeNull()
  })
})

describe('dfmHtmlReportPathFromToolComplete', () => {
  it('returns a completed HTML report path from the render_html result', () => {
    expect(
      dfmHtmlReportPathFromToolComplete({
        name: 'dfm_analysis',
        result: JSON.stringify({
          report: {
            kind: 'report_html',
            path: 'C:\\hermes\\runs\\run_1\\artifacts\\report.html'
          },
          run: { status: 'succeeded' }
        })
      })
    ).toBe('C:\\hermes\\runs\\run_1\\artifacts\\report.html')
  })

  it('ignores report-pending runs and non-HTML artifacts', () => {
    expect(
      dfmHtmlReportPathFromToolComplete({
        name: 'dfm_analysis',
        result: {
          report: { kind: 'report_html', path: 'C:\\pending\\report.html' },
          run: { status: 'reporting' }
        }
      })
    ).toBeNull()
    expect(
      dfmHtmlReportPathFromToolComplete({
        name: 'dfm_analysis',
        result: {
          report: { kind: 'report_markdown', path: 'C:\\done\\dfm_report.md' },
          run: { status: 'succeeded' }
        }
      })
    ).toBeNull()
  })
})
