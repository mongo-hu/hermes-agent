import { describe, expect, it } from 'vitest'

import {
  dfmStpPickerOptions,
  isDfmSessionSummary,
  isDfmStpModelPath,
  optimisticSessionPreviewForProduct,
  sanitizeDfmDisplayText
} from './product-mode'

describe('DFM model intake', () => {
  it('accepts only .stp paths and configures the native picker accordingly', () => {
    expect(isDfmStpModelPath('D:\\parts\\housing.stp')).toBe(true)
    expect(isDfmStpModelPath('housing.STP')).toBe(true)
    expect(isDfmStpModelPath('housing.step')).toBe(false)
    expect(isDfmStpModelPath('drawing.pdf')).toBe(false)
    expect(dfmStpPickerOptions()).toEqual({
      filters: [{ extensions: ['stp'], name: 'STP 三维模型' }],
      multiple: false,
      title: '选择 STP 三维模型'
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
