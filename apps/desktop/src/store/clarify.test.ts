import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  $clarifyRequest,
  $clarifyRequests,
  type ClarifyRequest,
  clearClarifyRequest,
  prioritizeDfmClarificationItems,
  readDfmClarificationBatch,
  setClarifyRequest
} from './clarify'
import { $activeSessionId } from './session'

function clarify(sessionId: string | null, requestId: string): ClarifyRequest {
  return {
    requestId,
    question: `question-${requestId}`,
    choices: null,
    sessionId
  }
}

describe('clarify store', () => {
  beforeEach(() => {
    $clarifyRequests.set({})
    $activeSessionId.set(null)
  })

  afterEach(() => {
    $clarifyRequests.set({})
    $activeSessionId.set(null)
  })

  it('keeps clarify requests from concurrent sessions independent', () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    setClarifyRequest(clarify('session-b', 'req-b'))

    expect($clarifyRequests.get()['session-a']?.requestId).toBe('req-a')
    expect($clarifyRequests.get()['session-b']?.requestId).toBe('req-b')
  })

  it('exposes only the active session via the focus-scoped view', () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    setClarifyRequest(clarify('session-b', 'req-b'))

    $activeSessionId.set('session-a')
    expect($clarifyRequest.get()?.requestId).toBe('req-a')

    $activeSessionId.set('session-b')
    expect($clarifyRequest.get()?.requestId).toBe('req-b')

    $activeSessionId.set('session-c')
    expect($clarifyRequest.get()).toBeNull()
  })

  it('clears only the targeted session, leaving the other pending', () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    setClarifyRequest(clarify('session-b', 'req-b'))

    clearClarifyRequest('req-a', 'session-a')

    expect($clarifyRequests.get()['session-a']).toBeUndefined()
    expect($clarifyRequests.get()['session-b']?.requestId).toBe('req-b')
  })

  it('ignores a stale clear whose request id no longer matches', () => {
    setClarifyRequest(clarify('session-a', 'req-a2'))

    clearClarifyRequest('req-a1', 'session-a')

    expect($clarifyRequests.get()['session-a']?.requestId).toBe('req-a2')
  })

  it('clears by request id across sessions when no session hint is given', () => {
    setClarifyRequest(clarify('session-a', 'shared'))
    setClarifyRequest(clarify('session-b', 'other'))

    clearClarifyRequest('shared')

    expect($clarifyRequests.get()['session-a']).toBeUndefined()
    expect($clarifyRequests.get()['session-b']?.requestId).toBe('other')
  })

  it('normalizes a dynamic DFM analysis batch from the gateway', () => {
    expect(
      readDfmClarificationBatch({
        items: [
          {
            check_id: 'C_WALL',
            choices: ['ABS', 'PC'],
            clarification_id: 'clarification_material',
            default_value: 'ABS',
            fact_name: 'material',
            feature_id: 'feature.main-wall',
            feature_kind: 'main_wall',
            feature_refs: ['feature.main-wall'],
            has_default: true,
            impact_scope: '特征实例级',
            runtime_key: 'material',
            question: '请选择材料'
          }
        ],
        mode: 'dfm_analysis_batch',
        project_id: 'dfm-1'
      })
    ).toEqual({
      items: [
        {
          checkId: 'C_WALL',
          choices: ['ABS', 'PC'],
          clarificationId: 'clarification_material',
          defaultValue: 'ABS',
          factName: 'material',
          featureId: 'feature.main-wall',
          featureKind: 'main_wall',
          featureRefs: ['feature.main-wall'],
          hasDefault: true,
          impactScope: '特征实例级',
          question: '请选择材料',
          runtimeKey: 'material'
        }
      ],
      mode: 'dfm_analysis_batch',
      projectId: 'dfm-1'
    })
  })

  it('stably prioritizes questions related to the selected feature', () => {
    const items = [
      {
        checkId: null,
        choices: [],
        clarificationId: 'boss-3',
        defaultValue: null,
        factName: 'boss_3',
        featureId: 'screw-boss-3',
        featureKind: 'screw_boss',
        featureRefs: ['screw-boss-3'],
        hasDefault: false,
        impactScope: '特征实例级',
        question: 'boss 3',
        runtimeKey: 'boss'
      },
      {
        checkId: null,
        choices: [],
        clarificationId: 'shared',
        defaultValue: null,
        factName: 'shared',
        featureId: null,
        featureKind: '',
        featureRefs: ['main-wall', 'screw-boss-2', 'screw-boss-3'],
        hasDefault: false,
        impactScope: '整件级',
        question: 'shared',
        runtimeKey: 'shared'
      },
      {
        checkId: null,
        choices: [],
        clarificationId: 'project',
        defaultValue: null,
        factName: 'project',
        featureId: null,
        featureKind: '',
        featureRefs: [],
        hasDefault: false,
        impactScope: '整件级',
        question: 'project',
        runtimeKey: 'project'
      },
      {
        checkId: null,
        choices: [],
        clarificationId: 'boss-2',
        defaultValue: null,
        factName: 'boss_2',
        featureId: 'screw-boss-2',
        featureKind: 'screw_boss',
        featureRefs: ['screw-boss-2'],
        hasDefault: false,
        impactScope: '特征实例级',
        question: 'boss 2',
        runtimeKey: 'boss'
      }
    ]

    expect(prioritizeDfmClarificationItems(items, 'screw-boss-2').map(item => item.clarificationId)).toEqual([
      'shared',
      'boss-2',
      'boss-3',
      'project'
    ])
    expect(items.map(item => item.clarificationId)).toEqual(['boss-3', 'shared', 'project', 'boss-2'])
  })
})
