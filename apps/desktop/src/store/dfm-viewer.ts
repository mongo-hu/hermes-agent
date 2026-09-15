import { atom, computed } from 'nanostores'

import { PREVIEW_PANE_ID, RIGHT_RAIL_DFM_TAB_ID, selectRightRailTab } from './layout'
import { setPaneOpen } from './panes'
import { $activeSessionId, $selectedStoredSessionId } from './session'

export interface DfmViewerTarget {
  manifestPath: string
  projectId?: string
  runId?: string
  status: 'completed' | 'preview'
}

type DfmViewerTargets = Record<string, DfmViewerTarget>

export const $dfmViewerTargets = atom<DfmViewerTargets>({})

export const $dfmViewerTarget = computed(
  [$dfmViewerTargets, $activeSessionId, $selectedStoredSessionId],
  (targets, activeSessionId, storedSessionId) =>
    targets[activeSessionId || ''] ?? targets[storedSessionId || ''] ?? null
)

function currentSessionId(): string {
  return $activeSessionId.get() || $selectedStoredSessionId.get() || ''
}

export function showDfmViewer(
  sessionId: string,
  target: DfmViewerTarget,
  { activate = true }: { activate?: boolean } = {}
) {
  const id = sessionId || currentSessionId()

  if (!id) {
    return
  }

  $dfmViewerTargets.set({ ...$dfmViewerTargets.get(), [id]: target })

  if (activate) {
    setPaneOpen(PREVIEW_PANE_ID, true)
    selectRightRailTab(RIGHT_RAIL_DFM_TAB_ID)
  }
}

export function dismissDfmViewer(sessionId = currentSessionId()) {
  if (!sessionId) {
    return
  }

  const current = $dfmViewerTargets.get()

  if (!current[sessionId]) {
    return
  }

  const next = { ...current }
  delete next[sessionId]
  $dfmViewerTargets.set(next)
}

export function clearDfmViewers() {
  $dfmViewerTargets.set({})
}
