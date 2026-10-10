import { describe, expect, it } from 'vitest'

import {
  classifyViewerIssueType,
  formatViewerValue,
  summarizeViewerIssueTypes
} from './dfm-viewer-issues'

const CHECK_LABELS_ZH = {
  C_BOSS_DRAFT: '普通凸台拔模角问题',
  C_BOSS_ROOT_FILLET: '普通凸台根部圆角问题',
  C_HOLE_BOTTOM_THK: '一般孔孔底厚度问题',
  C_HOLE_CLEARANCE: '一般孔孔边距/孔间距问题',
  C_HOLE_DRAFT: '一般孔孔壁拔模角问题',
  C_HOLE_THIN_STEEL: '一般孔薄钢问题',
  C_RIB_DRAFT: '加强筋拔模角问题',
  C_RIB_HEIGHT_RATIO: '加强筋高度比问题',
  C_RIB_ROOT_FILLET: '加强筋根部圆角问题',
  C_RIB_THK_RATIO: '加强筋厚度比问题',
  C_SCREW_BOSS_BOTTOM_THK: '螺钉柱孔底厚度问题',
  C_SCREW_BOSS_DRAFT: '螺钉柱拔模角问题',
  C_SCREW_BOSS_FILLET: '螺钉柱圆角问题',
  C_SCREW_BOSS_HOLE_DEPTH: '螺钉柱孔芯深度问题',
  C_SCREW_BOSS_THIN_STEEL: '螺钉柱薄钢问题',
  C_SCREW_BOSS_WALL_THK: '螺钉柱柱壁问题',
  C_WALL_DRAFT: '主体壁拔模角问题',
  C_WALL_FILLET: '主体壁圆角问题',
  C_WALL_THK_RANGE: '主体壁厚范围问题',
  C_WALL_THK_TRANSITION: '主体壁厚过渡问题'
}

describe('formatViewerValue', () => {
  it('shows numeric intervals instead of object strings', () => {
    expect(formatViewerValue({ lower: 0.2, upper: 0.25 })).toBe('[0.2, 0.25]')
  })

  it('retains enough precision near a failed threshold', () => {
    expect(formatViewerValue(0.4999999999)).toBe('0.4999999999')
  })
})

describe('summarizeViewerIssueTypes', () => {
  it('localizes every injection check, including raw labels in existing artifacts', () => {
    for (const [checkId, label] of Object.entries(CHECK_LABELS_ZH)) {
      expect(
        classifyViewerIssueType({
          check_id: checkId,
          issue_type_id: `legacy.${checkId.toLowerCase()}`,
          issue_type_label: checkId
        })
      ).toEqual([`legacy.${checkId.toLowerCase()}`, label])
    }
  })

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
