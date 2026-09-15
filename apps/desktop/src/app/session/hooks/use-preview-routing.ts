import { useStore } from '@nanostores/react'
import { type MutableRefObject, useCallback, useEffect } from 'react'

import { gatewayEventCompletedFileDiff } from '@/lib/gateway-events'
import {
  $previewTarget,
  $sessionPreviewRegistry,
  beginPreviewServerRestart,
  completePreviewServerRestart,
  getSessionPreviewRecord,
  progressPreviewServerRestart,
  requestPreviewReload,
  setPreviewTarget
} from '@/store/preview'
import { $currentCwd } from '@/store/session'
import type { RpcEvent } from '@/types/hermes'

type EventHandler = (event: RpcEvent) => void

interface PreviewRoutingOptions {
  activeSessionIdRef: MutableRefObject<string | null>
  baseHandleGatewayEvent: EventHandler
  currentCwd: string
  currentView: string
  requestGateway: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
  routedSessionId: string | null
  selectedStoredSessionId: string | null
}

function asRecord(payload: unknown): Record<string, unknown> {
  return payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {}
}

function activePreviewSessionId(
  activeSessionIdRef: MutableRefObject<string | null>,
  routedSessionId: string | null,
  selectedStoredSessionId: string | null
): string {
  return selectedStoredSessionId || routedSessionId || activeSessionIdRef.current || ''
}

export function usePreviewRouting({
  activeSessionIdRef,
  baseHandleGatewayEvent,
  currentCwd,
  currentView,
  requestGateway,
  routedSessionId,
  selectedStoredSessionId
}: PreviewRoutingOptions) {
  const previewRegistry = useStore($sessionPreviewRegistry)
  const previewSessionId = activePreviewSessionId(activeSessionIdRef, routedSessionId, selectedStoredSessionId)

  // Restore the latest registered preview when its session becomes active.
  // Most tool results remain opt-in through their inline preview card; the DFM
  // completion path deliberately registers its final HTML report for auto-open.
  useEffect(() => {
    if (currentView !== 'chat' || !previewSessionId) {
      setPreviewTarget(null)

      return
    }

    // Older Desktop builds registered automatic DFM reports under the
    // ephemeral runtime id. Fall back to that active alias so an already
    // generated report becomes visible after upgrading without another run.
    const runtimeSessionId = activeSessionIdRef.current
    const record =
      getSessionPreviewRecord(previewSessionId) ??
      (runtimeSessionId && runtimeSessionId !== previewSessionId ? getSessionPreviewRecord(runtimeSessionId) : null)

    setPreviewTarget(record?.normalized ?? null)
  }, [activeSessionIdRef, currentView, previewRegistry, previewSessionId])

  const restartPreviewServer = useCallback(
    async (url: string, context?: string) => {
      const sessionId = activeSessionIdRef.current

      if (!sessionId) {
        throw new Error('No active session for background restart')
      }

      const cwd = $currentCwd.get() || currentCwd || ''

      const result = await requestGateway<{ task_id?: string }>('preview.restart', {
        context: context || undefined,
        cwd: cwd || undefined,
        session_id: sessionId,
        url
      })

      const taskId = result.task_id || ''

      if (!taskId) {
        throw new Error('Background restart did not return a task id')
      }

      beginPreviewServerRestart(taskId, url)

      return taskId
    },
    [activeSessionIdRef, currentCwd, requestGateway]
  )

  const handleDesktopGatewayEvent = useCallback<EventHandler>(
    event => {
      baseHandleGatewayEvent(event)

      if (event.type === 'preview.restart.complete') {
        const { task_id, text } = asRecord(event.payload)

        if (typeof task_id === 'string' && task_id) {
          completePreviewServerRestart(task_id, typeof text === 'string' ? text : '')
        }
      } else if (event.type === 'preview.restart.progress') {
        const { task_id, text } = asRecord(event.payload)

        if (typeof task_id === 'string' && task_id) {
          progressPreviewServerRestart(task_id, typeof text === 'string' ? text : '')
        }
      }

      if (event.session_id && event.session_id !== activeSessionIdRef.current) {
        return
      }

      // Only refresh an already-open live preview when a file changes; never
      // open one unprompted. (Preview links are surfaced from the tool row into
      // the status stack — see tool-fallback.tsx.)
      if ($previewTarget.get()?.kind === 'url' && gatewayEventCompletedFileDiff(event)) {
        requestPreviewReload()
      }
    },
    [activeSessionIdRef, baseHandleGatewayEvent]
  )

  return { handleDesktopGatewayEvent, restartPreviewServer }
}
