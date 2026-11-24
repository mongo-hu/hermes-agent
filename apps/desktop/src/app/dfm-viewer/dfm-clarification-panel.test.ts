import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { DfmClarificationItem } from '@/store/clarify'
import { $gateway } from '@/store/gateway'

import { DfmClarificationPanel } from './dfm-viewer-pane'

describe('DFM clarification panel', () => {
  afterEach(() => {
    cleanup()
    $gateway.set(null)
  })

  it('shows every pending question and never bulk-fills the material default', async () => {
    const respond = vi.fn().mockResolvedValue({ ok: true })
    $gateway.set({ request: respond } as never)
    const item = (factName: string, fields: Partial<DfmClarificationItem>): DfmClarificationItem => ({
      checkId: null, choices: [], clarificationId: `clarification_${factName}`, defaultValue: null,
      factName, featureId: null, featureKind: '', featureRefs: [], hasDefault: false,
      impactScope: '', question: factName, runtimeKey: factName, ...fields
    })

    render(createElement(DfmClarificationPanel, {
      features: [], onFeaturesHighlight: vi.fn(), priorityFeatureId: null,
      request: {
        choices: null, question: '补充分析条件', requestId: 'test-material', sessionId: 'session-1',
        context: { mode: 'dfm_analysis_batch', projectId: 'dfm-test', items: [
          item('material', { choices: ['ABS', 'PC'], defaultValue: 'ABS', hasDefault: true, question: '材料？' }),
          item('finish', { choices: ['普通', '抛光'], defaultValue: '普通', hasDefault: true, question: '表面要求？',
            impactScope: '特征实例级' })
        ] }
      }
    }))

    const material = screen.getByRole<HTMLSelectElement>('combobox', { name: /材料？/ })
    const finish = screen.getByRole<HTMLSelectElement>('combobox', { name: /表面要求？/ })
    fireEvent.click(screen.getByRole('button', { name: /一键填充默认值/ }))
    expect(finish.value).toBe('普通')
    expect(material.value).toBe('')
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '提交全部并继续分析（1/2）' }).disabled).toBe(true)
    expect(respond).not.toHaveBeenCalled()

    fireEvent.change(material, { target: { value: 'PC' } })
    fireEvent.click(screen.getByRole('button', { name: '提交全部并继续分析（2/2）' }))
    await waitFor(() => expect(respond).toHaveBeenCalledWith('clarify.respond', {
      request_id: 'test-material',
      answer: JSON.stringify({ answers: [
        { fact_name: 'material', feature_id: null, value: 'PC' },
        { fact_name: 'finish', feature_id: null, value: '普通' }
      ] })
    }))
  })

  it('keeps answers independent when two feature instances share a fact name', () => {
    $gateway.set({ request: vi.fn() } as never)

    render(
      createElement(DfmClarificationPanel, {
        features: [],
        onFeaturesHighlight: vi.fn(),
        priorityFeatureId: null,
        request: {
          choices: null,
          context: {
            items: [
              {
                checkId: 'C_BOSS',
                choices: ['外观', '非外观'],
                clarificationId: 'clarification_appr_region_req#feature.boss.1',
                defaultValue: null,
                factName: 'appr_region_req',
                featureId: 'feature.boss.1',
                featureKind: 'hollow_boss',
                featureRefs: ['feature.boss.1'],
                hasDefault: false,
                impactScope: '特征实例级',
                question: '外观要求？',
                runtimeKey: 'appr_region_req'
              },
              {
                checkId: 'C_BOSS',
                choices: ['外观', '非外观'],
                clarificationId: 'clarification_appr_region_req#feature.boss.2',
                defaultValue: null,
                factName: 'appr_region_req',
                featureId: 'feature.boss.2',
                featureKind: 'hollow_boss',
                featureRefs: ['feature.boss.2'],
                hasDefault: false,
                impactScope: '特征实例级',
                question: '外观要求？',
                runtimeKey: 'appr_region_req'
              }
            ],
            mode: 'dfm_analysis_batch',
            projectId: 'dfm-test'
          },
          question: '补充分析条件',
          requestId: 'request-1',
          sessionId: 'session-1'
        }
      })
    )

    const selects = screen.getAllByRole('combobox') as HTMLSelectElement[]

    fireEvent.change(selects[0], { target: { value: '外观' } })
    fireEvent.change(selects[1], { target: { value: '非外观' } })

    expect(selects[0].value).toBe('外观')
    expect(selects[1].value).toBe('非外观')
  })
})
