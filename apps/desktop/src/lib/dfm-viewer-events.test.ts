import { describe, expect, it } from 'vitest'

import {
  dfmHtmlReportPathFromToolComplete,
  dfmViewerTargetFromSessionMessages,
  dfmViewerTargetFromToolComplete
} from './dfm-viewer-events'

describe('dfmViewerTargetFromToolComplete', () => {
  it('clears a historical review failure after an authoritative review read', () => {
    const manifestPath = 'C:/hermes/discovery/dfm_discovery_viewer.json'
    const target = dfmViewerTargetFromToolComplete({
      name: 'dfm_analysis',
      args: { action: 'review_context', project_id: 'dfm_1' },
      result: { ok: true, project_id: 'dfm_1', phase: 'discovery',
        status: 'clarification_required', revision: 26, viewer_manifest: manifestPath }
    }, { projectId: 'dfm_1', manifestPath, status: 'discovery', revision: 24,
      failure: { code: 'review_group_stale', message: 'already closed' } })
    expect(target?.revision).toBe(26)
    expect(target?.failure).toBeUndefined()
  })

  it.each(['clarification_required', 'forming_scope_review_required'])(
    'refreshes the committed discovery view while the workflow is %s',
    status => {
      const state = { project_id: 'dfm_1', phase: 'discovery', status, revision: 24 }
      const manifestPath = 'C:\\hermes\\discovery\\dfm_discovery_viewer.json'
      for (const name of ['clarify', 'dfm_analysis']) {
        const target = dfmViewerTargetFromToolComplete({
          name,
          result: name === 'clarify'
            ? { dfm_discovery_review: state, viewer_manifest: manifestPath }
            : { ...state, viewer_manifest: manifestPath }
        })
        expect(target).toMatchObject({ manifestPath, projectId: 'dfm_1', revision: 24, status: 'discovery' })
      }
    }
  )

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

  it('opens editable Discovery regions before planning', () => {
    const target = dfmViewerTargetFromToolComplete({
      name: 'dfm_analysis',
      result: {
        project_id: 'dfm_1',
        revision: 17,
        status: 'discovery_review_required',
        viewer_manifest: 'C:\\hermes\\discovery\\dfm_discovery_viewer.json'
      }
    })

    expect(target).toEqual({
      manifestPath: 'C:\\hermes\\discovery\\dfm_discovery_viewer.json',
      projectId: 'dfm_1',
      revision: 17,
      runId: undefined,
      status: 'discovery'
    })
  })

  it('refreshes Discovery regions after the bound confirmation question', () => {
    const target = dfmViewerTargetFromToolComplete({
      name: 'clarify',
      result: {
        dfm_discovery_review: {
          project_id: 'dfm_1',
          revision: 20,
          status: 'discovery_confirmed',
          viewer_manifest: 'C:\\hermes\\discovery\\dfm_discovery_viewer.json'
        }
      }
    })

    expect(target).toEqual({
      manifestPath: 'C:\\hermes\\discovery\\dfm_discovery_viewer.json',
      projectId: 'dfm_1',
      revision: 20,
      runId: undefined,
      status: 'discovery'
    })
  })

  it('opens the completed manifest from a successful run result', () => {
    const target = dfmViewerTargetFromToolComplete({
      name: 'dfm_analysis',
      result: {
        project_id: 'dfm_1',
        run: {
          run_id: 'run_1',
          status: 'succeeded',
          artifacts: [{ kind: 'dfm_viewer', path: 'C:\\hermes\\runs\\run_1\\dfm_viewer.json' }]
        }
      }
    })

    expect(target).toEqual({
      manifestPath: 'C:\\hermes\\runs\\run_1\\dfm_viewer.json',
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

  it('returns a completed HTML report path from a background render event', () => {
    expect(
      dfmHtmlReportPathFromToolComplete({
        name: 'dfm_analysis',
        status: 'succeeded',
        report_html: 'C:\\hermes\\runs\\run_1\\artifacts\\report.html'
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

describe('dfmViewerTargetFromSessionMessages', () => {
  it('restores the latest successful DFM viewer from persisted tool messages', () => {
    const target = dfmViewerTargetFromSessionMessages([
      { content: 'analyze this part', role: 'user' },
      {
        content: JSON.stringify({
          project_id: 'dfm_old',
          preview: {
            run_id: 'preview_old',
            status: 'ready',
            viewer_manifest: 'C:\\hermes\\old\\dfm_viewer.json'
          }
        }),
        role: 'tool',
        tool_name: 'dfm_project'
      },
      {
        content: JSON.stringify({
          project_id: 'dfm_1',
          run: {
            artifacts: [{ kind: 'dfm_viewer', path: 'C:\\hermes\\runs\\run_1\\dfm_viewer.json' }],
            run_id: 'run_1',
            status: 'succeeded'
          }
        }),
        role: 'tool',
        tool_name: 'dfm_analysis'
      }
    ])

    expect(target).toEqual({
      manifestPath: 'C:\\hermes\\runs\\run_1\\dfm_viewer.json',
      projectId: 'dfm_1',
      runId: 'run_1',
      status: 'completed'
    })
  })

  it('returns null for a history without a usable DFM manifest', () => {
    expect(
      dfmViewerTargetFromSessionMessages([
        { content: 'hello', role: 'user' },
        { content: '{"status":"failed"}', role: 'tool', tool_name: 'dfm_analysis' }
      ])
    ).toBeNull()
  })
})

describe('DFM analysis failures', () => {
  const preview = { manifestPath: 'C:/model/dfm_viewer.json', projectId: 'dfm_1', status: 'preview' as const }
  const error = { code: 'geometry_protocol_invalid', message: 'Duplicate coverage identities.' }

  it('keeps the current model and surfaces a matching analysis failure', () => {
    expect(
      dfmViewerTargetFromToolComplete(
        {
          name: 'dfm_analysis',
          args: { project_id: 'dfm_1' },
          result: { ok: false, error }
        },
        preview
      )
    ).toEqual({ ...preview, failure: error })
  })

  it('does not attach another project or an unidentified failure to the visible model', () => {
    for (const project_id of ['dfm_other', undefined]) {
      expect(
        dfmViewerTargetFromToolComplete(
          {
            name: 'dfm_analysis',
            args: { project_id },
            result: { ok: false, error }
          },
          preview
        )
      ).toBeNull()
    }
  })

  it('clears the failure when a later discovery succeeds', () => {
    const target = dfmViewerTargetFromToolComplete(
      {
        name: 'dfm_analysis',
        result: {
          project_id: 'dfm_1',
          status: 'discovery_review_required',
          viewer_manifest: 'C:/model/discovery.json'
        }
      },
      { ...preview, failure: error }
    )
    expect(target?.status).toBe('discovery')
    expect(target?.failure).toBeUndefined()
  })

  it('restores the failure after reopening a session using its persisted tool-call arguments', () => {
    expect(
      dfmViewerTargetFromSessionMessages([
        {
          role: 'tool',
          tool_name: 'dfm_project',
          content: { project_id: 'dfm_1', preview: { status: 'ready', viewer_manifest: preview.manifestPath } }
        },
        {
          role: 'assistant',
          content: null,
          tool_calls: [
            {
              id: 'discover_1',
              function: { name: 'dfm_analysis', arguments: '{"project_id":"dfm_1","action":"discover"}' }
            }
          ]
        },
        {
          role: 'tool',
          tool_call_id: 'discover_1',
          tool_name: 'dfm_analysis',
          content: JSON.stringify({ ok: false, error })
        }
      ])
    ).toEqual({ ...preview, runId: undefined, failure: error })
  })

  it('surfaces a background failed run without replacing the manifest', () => {
    expect(
      dfmViewerTargetFromToolComplete(
        {
          name: 'dfm_analysis',
          project_id: 'dfm_1',
          status: 'failed',
          message: 'Worker stopped.'
        },
        preview
      )?.failure?.message
    ).toBe('Worker stopped.')
  })
})
