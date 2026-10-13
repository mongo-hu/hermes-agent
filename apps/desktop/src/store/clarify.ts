import { atom, computed } from 'nanostores'

import { $activeSessionId } from './session'

export interface DfmClarificationItem {
  choices: string[]
  clarificationId: string
  factName: string
  featureRefs: string[]
  question: string
}

export interface DfmClarificationBatchContext {
  items: DfmClarificationItem[]
  mode: 'dfm_analysis_batch'
  projectId: string
}

export interface ClarifyRequest {
  context?: DfmClarificationBatchContext | null
  requestId: string
  question: string
  choices: string[] | null
  sessionId: string | null
}

export function readDfmClarificationBatch(value: unknown): DfmClarificationBatchContext | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null
  }

  const row = value as Record<string, unknown>

  if (row.mode !== 'dfm_analysis_batch' || typeof row.project_id !== 'string' || !Array.isArray(row.items)) {
    return null
  }

  const items = row.items.flatMap(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      return []
    }

    const candidate = item as Record<string, unknown>

    if (
      typeof candidate.clarification_id !== 'string' ||
      typeof candidate.fact_name !== 'string' ||
      typeof candidate.question !== 'string'
    ) {
      return []
    }

    return [
      {
        choices: Array.isArray(candidate.choices)
          ? candidate.choices.filter((choice): choice is string => typeof choice === 'string')
          : [],
        clarificationId: candidate.clarification_id,
        factName: candidate.fact_name,
        featureRefs: Array.isArray(candidate.feature_refs)
          ? candidate.feature_refs.filter((ref): ref is string => typeof ref === 'string')
          : [],
        question: candidate.question
      }
    ]
  })

  return items.length > 0 ? { items, mode: row.mode, projectId: row.project_id } : null
}

// Pending clarify requests keyed by the runtime session id that raised them.
// Storing per-session (instead of one shared slot) lets a *background* session
// park its clarify request while the user is looking at a different chat, then
// resolve it once they switch over — without a second concurrent clarify
// clobbering the first. A request with no session id lands under the empty key.
const keyFor = (sessionId: string | null | undefined): string => sessionId ?? ''

export const $clarifyRequests = atom<Record<string, ClarifyRequest>>({})

// The clarify request for the currently-viewed session. The inline ClarifyTool
// only ever mounts inside the active session's transcript, so it reads this
// focus-scoped view rather than reaching into the whole map.
export const $clarifyRequest = computed(
  [$clarifyRequests, $activeSessionId],
  (requests, activeId) => requests[keyFor(activeId)] ?? null
)

export function setClarifyRequest(request: ClarifyRequest): void {
  $clarifyRequests.set({ ...$clarifyRequests.get(), [keyFor(request.sessionId)]: request })
}

export function clearClarifyRequest(requestId?: string, sessionId?: string | null): void {
  const requests = $clarifyRequests.get()

  // Targeted clear when the caller knows the session (the common path from the
  // inline ClarifyTool answering its own request).
  if (sessionId !== undefined) {
    const key = keyFor(sessionId)
    const current = requests[key]

    if (!current || (requestId && current.requestId !== requestId)) {
      return
    }

    const next = { ...requests }
    delete next[key]
    $clarifyRequests.set(next)

    return
  }

  // Fallback with no session hint: drop every entry matching the request id
  // (or clear all when none is given).
  const next: Record<string, ClarifyRequest> = {}
  let changed = false

  for (const [key, value] of Object.entries(requests)) {
    if (requestId && value.requestId !== requestId) {
      next[key] = value
    } else {
      changed = true
    }
  }

  if (changed) {
    $clarifyRequests.set(next)
  }
}
