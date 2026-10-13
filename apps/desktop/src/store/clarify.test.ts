import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  $clarifyRequest,
  $clarifyRequests,
  type ClarifyRequest,
  clearClarifyRequest,
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
            choices: ['ABS', 'PC'],
            clarification_id: 'clarification_material',
            fact_name: 'material',
            feature_refs: ['feature.main-wall'],
            question: '请选择材料'
          }
        ],
        mode: 'dfm_analysis_batch',
        project_id: 'dfm-1'
      })
    ).toEqual({
      items: [
        {
          choices: ['ABS', 'PC'],
          clarificationId: 'clarification_material',
          factName: 'material',
          featureRefs: ['feature.main-wall'],
          question: '请选择材料'
        }
      ],
      mode: 'dfm_analysis_batch',
      projectId: 'dfm-1'
    })
  })
})
