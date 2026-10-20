import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $gateway } from '@/store/gateway'

import { DfmClarificationPanel } from './dfm-viewer-pane'

describe('DFM clarification panel', () => {
  afterEach(() => {
    cleanup()
    $gateway.set(null)
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
                featureKind: 'screw_boss',
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
                featureKind: 'screw_boss',
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
