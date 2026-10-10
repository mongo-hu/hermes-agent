import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $dfmViewerTargets, clearDfmViewers, showDfmViewer } from '@/store/dfm-viewer'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const sessionId = 'tool-lifecycle-session'
const states = new Map<string, ClientSessionState>()
let handleEvent: ((event: RpcEvent) => void) | null = null

function Harness() {
  const activeSessionIdRef = useRef<string | null>(sessionId)
  const sessionStateByRuntimeIdRef = useRef(states)
  const queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState: (id, updater) => {
      const next = updater(states.get(id) ?? createClientSessionState())

      states.set(id, next)

      return next
    }
  })

  useEffect(() => {
    handleEvent = stream.handleGatewayEvent
  }, [stream.handleGatewayEvent])

  return null
}

afterEach(() => {
  cleanup()
  states.clear()
  handleEvent = null
  clearDfmViewers()
  vi.restoreAllMocks()
})

describe('desktop tool lifecycle', () => {
  it('renders one completed row when an id-less generating event precedes a DFM call', async () => {
    render(<Harness />)
    await waitFor(() => expect(handleEvent).not.toBeNull())

    act(() => {
      handleEvent!({ payload: { name: 'dfm_analysis' }, session_id: sessionId, type: 'tool.generating' })
      handleEvent!({
        payload: { context: '', name: 'dfm_analysis', tool_id: 'call-1' },
        session_id: sessionId,
        type: 'tool.start'
      })
      handleEvent!({
        payload: { name: 'dfm_analysis', result: { feature_counts: { main_wall: 1 } }, tool_id: 'call-1' },
        session_id: sessionId,
        type: 'tool.complete'
      })
    })

    const toolParts = states
      .get(sessionId)
      ?.messages.flatMap(message => message.parts.filter(part => part.type === 'tool-call'))

    expect(toolParts).toHaveLength(1)
    expect(toolParts?.[0]).toMatchObject({ toolCallId: 'call-1', result: { feature_counts: { main_wall: 1 } } })
  })

  it('advances the in-place Discovery manifest revision after its bound confirmation', async () => {
    render(<Harness />)
    await waitFor(() => expect(handleEvent).not.toBeNull())

    showDfmViewer(
      sessionId,
      {
        manifestPath: 'C:\\dfm\\discovery_viewer.json',
        projectId: 'dfm_1',
        revision: 17,
        status: 'discovery'
      },
      { activate: false }
    )
    act(() => {
      handleEvent!({
        payload: {
          name: 'clarify',
          result: {
            dfm_discovery_review: {
              project_id: 'dfm_1',
              revision: 20,
              status: 'discovery_confirmed',
              viewer_manifest: 'C:\\dfm\\discovery_viewer.json'
            }
          },
          tool_id: 'confirm-discovery'
        },
        session_id: sessionId,
        type: 'tool.complete'
      })
    })

    expect($dfmViewerTargets.get()[sessionId]).toEqual({
      manifestPath: 'C:\\dfm\\discovery_viewer.json',
      projectId: 'dfm_1',
      revision: 20,
      runId: undefined,
      status: 'discovery'
    })
  })
})
