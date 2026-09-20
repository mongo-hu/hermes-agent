import { describe, expect, it } from 'vitest'

import { summarizeViewerIssueTypes } from './dfm-viewer-issues'

describe('summarizeViewerIssueTypes', () => {
  it('groups failed results by business Check without using severity', () => {
    expect(
      summarizeViewerIssueTypes([
        {
          check_id: 'check.main_wall_minimum_draft',
          metric_id: 'injection.geometry.draft'
        },
        {
          check_id: 'check.main_wall_minimum_thickness',
          metric_id: 'injection.geometry.wall_thickness'
        },
        {
          check_id: 'check.main_wall_minimum_thickness',
          metric_id: 'injection.geometry.wall_thickness'
        }
      ])
    ).toEqual([
      { count: 1, issue_type_id: 'check.main_wall_minimum_draft', label: '拔模角问题' },
      { count: 2, issue_type_id: 'check.main_wall_minimum_thickness', label: '壁厚问题' }
    ])
  })

  it('falls back to the technical metric for older viewer artifacts', () => {
    expect(summarizeViewerIssueTypes([{ metric_id: 'injection.geometry.undercut' }])).toEqual([
      { count: 1, issue_type_id: 'injection.geometry.undercut', label: '倒扣问题' }
    ])
  })
})
