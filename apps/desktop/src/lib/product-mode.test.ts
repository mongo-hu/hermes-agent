import { describe, expect, it } from 'vitest'

import {
  dfmStepPickerOptions,
  isDfmSessionSummary,
  isDfmStepModelPath,
  optimisticSessionPreviewForProduct,
  sanitizeDfmDisplayText
} from './product-mode'

describe('DFM model intake', () => {
  it('accepts only STEP model extensions and configures the native picker accordingly', () => {
    expect(isDfmStepModelPath('D:\\parts\\housing.stp')).toBe(true)
    expect(isDfmStepModelPath('housing.STP')).toBe(true)
    expect(isDfmStepModelPath('housing.step')).toBe(true)
    expect(isDfmStepModelPath('housing.STEP')).toBe(true)
    expect(isDfmStepModelPath('drawing.pdf')).toBe(false)
    expect(dfmStepPickerOptions()).toEqual({
      filters: [{ extensions: ['step', 'stp'], name: 'STEP / STP 三维模型' }],
      multiple: false,
      title: '选择 STEP / STP 三维模型'
    })
  })
})

describe('isDfmSessionSummary', () => {
  it('keeps DFM analysis sessions and manufacturing-model attachments', () => {
    expect(isDfmSessionSummary({ title: '注塑模具 DFM 分析报告' })).toBe(true)
    expect(isDfmSessionSummary({ preview: 'Attached model: product-housing.step' })).toBe(true)
    expect(isDfmSessionSummary({ preview: '请检查这个零件的可制造性' })).toBe(true)
  })

  it('filters historical general-agent conversations', () => {
    expect(isDfmSessionSummary({ title: 'Hermes Configuration File Summary' })).toBe(false)
    expect(isDfmSessionSummary({ title: 'Browser Access Request' })).toBe(false)
  })

  it('marks a newly-created DFM session before its attachment preview is persisted', () => {
    expect(optimisticSessionPreviewForProduct(null)).toBe('DFM analysis')
    expect(optimisticSessionPreviewForProduct('开始分析')).toBe('DFM · 开始分析')
    expect(isDfmSessionSummary({ preview: optimisticSessionPreviewForProduct(null) })).toBe(true)
  })

  it('removes legacy branding from rendered copy without changing stored data', () => {
    const source = 'Run hermes_cli with ~/.hermes/config.yaml (Hermes backend)'

    expect(sanitizeDfmDisplayText(source)).toBe('Run DFM_cli with ~/.DFM/config.yaml (DFM backend)')
    expect(source).toBe('Run hermes_cli with ~/.hermes/config.yaml (Hermes backend)')
  })
})
