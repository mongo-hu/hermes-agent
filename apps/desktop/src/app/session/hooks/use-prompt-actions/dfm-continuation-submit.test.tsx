import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import type { Translations } from '@/i18n'
import { textPart } from '@/lib/chat-messages'
import { DFM_CONTINUATION_TEXT, type DfmContinuationInput } from '@/lib/dfm-continuation'
import { $clarifyRequests } from '@/store/clarify'
import { $activeSessionId } from '@/store/session'

import { useSubmitPrompt } from './submit'
import { _submitInFlight, type GatewayRequest } from './utils'

const continuation: DfmContinuationInput = {
  project_id: 'project-1', snapshot_id: 'snapshot-1', expected_revision: 4,
  action_key: 'dfm-review:project-1:snapshot-1:4', expected_session_key: 'stored-1'
}

function setup(requestGateway: GatewayRequest, runtimeId: string | null = 'runtime-1') {
  const state = { current: { storedSessionId: 'stored-1', messages: [], busy: false,
    awaitingResponse: false, interrupted: false, needsInput: false } as unknown as ClientSessionState }
  const activeSessionIdRef = { current: runtimeId }
  const selectedStoredSessionIdRef = { current: 'stored-1' as string | null }
  const createBackendSessionForSend = vi.fn(async () => 'unwanted-new-runtime')
  const { result } = renderHook(() => useSubmitPrompt({
    activeSessionId: runtimeId, activeSessionIdRef, selectedStoredSessionIdRef, busyRef: { current: false },
    copy: {} as Translations['desktop'], newAnalysisOnModelSubmit: true, requestGateway,
    createBackendSessionForSend, syncAttachmentsForSubmit: vi.fn(async () => []),
    // Match use-session-state-cache: each updater receives a fresh shallow copy.
    updateSessionState: (_sid, update) => {
      state.current = update({ ...state.current, messages: state.current.messages })
      return state.current
    }
  }))
  return { state, activeSessionIdRef, selectedStoredSessionIdRef, createBackendSessionForSend,
    submit: () => result.current(DFM_CONTINUATION_TEXT, { attachments: [], dfmContinuation: continuation }) }
}

afterEach(() => {
  cleanup()
  _submitInFlight.clear()
  $activeSessionId.set(null)
  $clarifyRequests.set({})
})

describe('DFM continuation submit ownership and event ordering', () => {
  it('does not create another session when resuming the original session fails', async () => {
    const rpc = vi.fn().mockRejectedValue(new Error('original session unavailable'))
    const test = setup(rpc, null)
    await expect(test.submit()).rejects.toThrow('original session unavailable')
    expect(rpc).toHaveBeenCalledTimes(1)
    expect(rpc.mock.calls[0][0]).toBe('session.resume')
    expect(test.createBackendSessionForSend).not.toHaveBeenCalled()
    expect(test.activeSessionIdRef.current).toBeNull()
    expect(test.state.current.messages).toEqual([])
  })

  it('does not submit when a same-project continuation belongs to another selected session', async () => {
    const rpc = vi.fn()
    const test = setup(rpc)
    test.selectedStoredSessionIdRef.current = 'stored-2'
    await expect(test.submit()).rejects.toThrow('会话已切换')
    expect(rpc).not.toHaveBeenCalled()
    expect(test.createBackendSessionForSend).not.toHaveBeenCalled()
  })

  it('recovers only the original stored session after its old runtime has been reaped', async () => {
    const calls: { method: string; params?: Record<string, unknown> }[] = []
    const rpc = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })
      if (method === 'session.resume') return { session_id: 'runtime-2', pending_clarify: null }
      if (method === 'prompt.submit' && params?.session_id === 'runtime-1') throw new Error('session not found')
      if (method === 'prompt.submit' && params?.session_id === 'runtime-2') return { status: 'completed' }
      throw new Error(`Unexpected request: ${method}`)
    }) as GatewayRequest
    const test = setup(rpc)
    // Either one same-session recovery, or an explicit second click after the
    // dead binding is cleared, is safe. Neither may create a new conversation.
    let accepted = false
    await act(async () => {
      try { accepted = await test.submit() } catch { /* a retryable missing-session error */ }
      if (!accepted) accepted = await test.submit()
    })
    expect(accepted).toBe(true)
    expect(calls.map(call => call.method)).toEqual(['prompt.submit', 'session.resume', 'prompt.submit'])
    expect(calls[1].params).toMatchObject({ session_id: 'stored-1' })
    expect(test.activeSessionIdRef.current).toBe('runtime-2')
    expect(test.createBackendSessionForSend).not.toHaveBeenCalled()
    expect(test.state.current.messages).toEqual([])
  })

  it('does not automatically resubmit or replace a runtime after an ambiguous timeout', async () => {
    const rpc = vi.fn().mockRejectedValue(new Error('request timed out: prompt.submit'))
    const test = setup(rpc)
    await expect(test.submit()).rejects.toThrow('request timed out')
    expect(rpc).toHaveBeenCalledTimes(1)
    expect(test.activeSessionIdRef.current).toBe('runtime-1')
    expect(test.createBackendSessionForSend).not.toHaveBeenCalled()
  })

  it.each(['completed', 'active_run', 'duplicate'])('does not insert another turn for %s', async status => {
    const rpc = vi.fn().mockResolvedValue({ status })
    const test = setup(rpc)
    await act(async () => { expect(await test.submit()).toBe(true) })
    expect(rpc).toHaveBeenCalledTimes(1)
    expect(rpc.mock.calls[0][1]).toMatchObject({ session_id: 'runtime-1', dfm_continuation: continuation })
    expect(test.state.current.messages).toEqual([])
    expect(test.state.current.busy).toBe(false)
    expect(test.createBackendSessionForSend).not.toHaveBeenCalled()
  })

  it.each(['needs-input', 'completed'])('preserves a %s event that arrives before the submit acknowledgement', async event => {
    let acknowledge!: (value: unknown) => void
    const rpc = vi.fn(() => new Promise(resolve => { acknowledge = resolve })) as GatewayRequest
    const test = setup(rpc)
    let pending!: Promise<boolean>
    act(() => { pending = test.submit() })
    const reply = { id: 'reply', role: 'assistant' as const, parts: [textPart('response before acknowledgement')] }
    test.state.current = {
      ...test.state.current,
      busy: event !== 'completed', awaitingResponse: false, needsInput: event === 'needs-input',
      messages: [...test.state.current.messages, reply]
    }
    await act(async () => { acknowledge({ status: 'submitted' }); await pending })
    expect(test.state.current.awaitingResponse).toBe(false)
    expect(test.state.current.needsInput).toBe(event === 'needs-input')
    expect(test.state.current.busy).toBe(event !== 'completed')
    expect(test.state.current.messages.map(message => message.role)).toEqual(['user', 'assistant'])
  })
})
