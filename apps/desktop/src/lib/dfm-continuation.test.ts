import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ReviewContext } from '@/app/dfm-viewer/dfm-review'
import { $clarifyRequests, readDfmClarificationContext, restorePendingClarify, setClarifyRequest } from '@/store/clarify'

import { continueDfmReview } from './dfm-continuation'

const context: ReviewContext = {
  project_id: 'p', snapshot_id: 'snapshot', revision: 9, features: [], annotations: [], forming_scopes: [],
  review_groups: [{ review_group_id: 'g', kind: 'ordinary_structure', target_refs: ['f'], status: 'confirmed' }]
}
const request = {
  requestId: 'review', sessionId: 'runtime', question: 'review', choices: null,
  context: { mode: 'dfm_discovery_review' as const, projectId: 'p', snapshotId: 'snapshot', revision: 3 }
}
const args = () => ({ context, request, runtimeSessionId: 'runtime', sessionKey: 'stored',
  respond: vi.fn().mockResolvedValue({ ok: true }), submit: vi.fn().mockResolvedValue(true) })

afterEach(() => $clarifyRequests.set({}))

describe('DFM review continuation', () => {
  it('answers the matching wait using the successful save revision without another turn', async () => {
    setClarifyRequest(request)
    const input = args()
    await continueDfmReview(input)
    expect(input.respond).toHaveBeenCalledWith({
      request_id: 'review', session_id: 'runtime', answer: '确认最终特征区域并继续',
      dfm_context: { project_id: 'p', snapshot_id: 'snapshot', expected_revision: 9 }
    })
    expect(input.submit).not.toHaveBeenCalled()
    expect($clarifyRequests.get()).toEqual({})
  })
  it('does not continue while any review remains pending or stale', async () => {
    for (const status of ['pending_review', 'stale'] as const) {
      const input = args()
      await continueDfmReview({ ...input, context: { ...context, review_groups: [{ ...context.review_groups[0], status }] } })
      expect(input.respond).not.toHaveBeenCalled()
      expect(input.submit).not.toHaveBeenCalled()
    }
  })
  it('rejects a request belonging to another session, project or later revision', async () => {
    for (const stale of [
      { ...request, sessionId: 'other' },
      { ...request, context: { ...request.context, projectId: 'other' } },
      { ...request, context: { ...request.context, revision: 10 } }
    ]) {
      const input = args()
      await expect(continueDfmReview({ ...input, request: stale })).rejects.toThrow('待处理')
      expect(input.respond).not.toHaveBeenCalled()
      expect(input.submit).not.toHaveBeenCalled()
    }
  })
  it('allows an explicitly saved geometry edit to replace the snapshot while the original waiter remains', async () => {
    const input = args()
    await continueDfmReview({ ...input, context: { ...context, snapshot_id: 'edited-snapshot' } })
    expect(input.respond).toHaveBeenCalledWith(expect.objectContaining({
      dfm_context: { project_id: 'p', snapshot_id: 'edited-snapshot', expected_revision: 9 }
    }))
    expect(input.submit).not.toHaveBeenCalled()
  })
  it('preserves the request when responding fails', async () => {
    setClarifyRequest(request)
    const input = args()
    input.respond.mockRejectedValue(new Error('conflict'))
    await expect(continueDfmReview(input)).rejects.toThrow('conflict')
    expect($clarifyRequests.get().runtime.requestId).toBe('review')
    expect(input.submit).not.toHaveBeenCalled()
  })
  it('uses the same dedupe key for automatic and explicit retry of one saved review', async () => {
    const input = { ...args(), request: null }
    await continueDfmReview(input)
    await continueDfmReview({ ...input, manual: true })
    expect(input.submit.mock.calls[0][0]).toMatchObject({ expected_session_key: 'stored', manual_resume: false })
    expect(input.submit.mock.calls[1][0]).toMatchObject({
      ...input.submit.mock.calls[0][0], manual_resume: true
    })
  })
  it('hydrates an existing request without replacing another session and rejects malformed identity', () => {
    setClarifyRequest(request)
    const payload = { session_id: 'second', request_id: 'r2', question: 'review', context: {
      mode: 'dfm_discovery_review', project_id: 'p2', snapshot_id: 's2', revision: 4
    } }
    restorePendingClarify(payload, 'second')
    restorePendingClarify({ ...payload, request_id: 'wrong' }, 'runtime')
    expect(Object.keys($clarifyRequests.get())).toHaveLength(2)
    expect($clarifyRequests.get().runtime).toEqual(request)
    expect($clarifyRequests.get().second.context).toEqual(readDfmClarificationContext(payload.context))
  })
  it('does not let a delayed reconnect snapshot clear or overwrite a newer socket request', () => {
    setClarifyRequest(request)
    restorePendingClarify(null, 'runtime', null)
    restorePendingClarify({ session_id: 'runtime', request_id: 'old', question: 'old' }, 'runtime', null)
    expect($clarifyRequests.get().runtime).toEqual(request)
    restorePendingClarify(null, 'runtime', request)
    expect($clarifyRequests.get()).toEqual({})
  })
})
