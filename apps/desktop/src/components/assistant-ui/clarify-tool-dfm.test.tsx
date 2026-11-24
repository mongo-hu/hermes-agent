import type { ToolCallMessagePartProps } from '@assistant-ui/react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n/context'
import { $clarifyRequests, type ClarifyRequest, setClarifyRequest } from '@/store/clarify'
import { $gateway } from '@/store/gateway'
import { $activeSessionId } from '@/store/session'

import { ClarifyTool } from './clarify-tool'

vi.mock('@assistant-ui/react', () => ({ useAuiState: () => true }))
vi.mock('@/components/assistant-ui/tool/fallback', () => ({ ToolFallback: () => null }))

const request = vi.fn(async () => ({ ok: true }))
const base: ClarifyRequest = { requestId: 'question-1', sessionId: 'runtime-1', question: 'Choose', choices: ['Continue choice'] }
const review: ClarifyRequest['context'] = {
  mode: 'dfm_discovery_review', projectId: 'project-1', snapshotId: 'snapshot-1', revision: 4
}
const batch: ClarifyRequest['context'] = { mode: 'dfm_analysis_batch', projectId: 'project-1', items: [] }

function mount() {
  const props = { args: { question: base.question, choices: base.choices }, result: undefined } as ToolCallMessagePartProps
  return render(<I18nProvider configClient={null} initialLocale="en"><ClarifyTool {...props} /></I18nProvider>)
}

beforeEach(() => {
  request.mockClear()
  $activeSessionId.set('runtime-1')
  $clarifyRequests.set({})
  $gateway.set({ request } as never)
})

afterEach(() => {
  cleanup()
  $clarifyRequests.set({})
  $activeSessionId.set(null)
  $gateway.set(null)
})

describe('DFM clarification remains in the workbench', () => {
  it.each([review, batch])('does not submit hidden choice shortcuts for $mode', context => {
    setClarifyRequest({ ...base, context })
    mount()
    expect(screen.getByText(/右侧/)).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByRole('textbox')).toBeNull()
    fireEvent.keyDown(window, { key: 'a' })
    fireEvent.keyDown(window, { key: 'Enter' })
    expect(request).not.toHaveBeenCalled()
    expect($clarifyRequests.get()['runtime-1'].requestId).toBe(base.requestId)
  })

  it('drops generic keyboard submission when a DFM question replaces a selected choice', () => {
    setClarifyRequest(base)
    mount()
    fireEvent.keyDown(window, { key: 'a' })
    act(() => setClarifyRequest({ ...base, requestId: 'review-2', context: review }))
    expect(screen.queryByRole('button')).toBeNull()
    fireEvent.keyDown(window, { key: 'Enter' })
    expect(request).not.toHaveBeenCalled()
    expect($clarifyRequests.get()['runtime-1'].requestId).toBe('review-2')
  })

  it('keeps normal non-DFM choice submission working', async () => {
    setClarifyRequest(base)
    mount()
    fireEvent.keyDown(window, { key: 'a' })
    fireEvent.keyDown(window, { key: 'Enter' })
    await waitFor(() => expect(request).toHaveBeenCalledWith('clarify.respond', {
      request_id: 'question-1', session_id: 'runtime-1', answer: 'Continue choice'
    }))
  })
})
