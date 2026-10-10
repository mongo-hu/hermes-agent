import { atom, computed } from 'nanostores'

import { PREVIEW_PANE_ID, RIGHT_RAIL_DFM_TAB_ID, selectRightRailTab } from './layout'
import { setPaneOpen } from './panes'
import { $activeSessionId, $selectedStoredSessionId } from './session'

export interface DfmViewerTarget {
  manifestPath: string
  projectId?: string
  revision?: number
  runId?: string
  status: 'completed' | 'discovery' | 'preview'
}

type DfmViewerTargets = Record<string, DfmViewerTarget>
type DfmViewerDismissals = Record<string, true>

export const $dfmViewerTargets = atom<DfmViewerTargets>({})
export const $dismissedDfmViewers = atom<DfmViewerDismissals>({})

function currentSessionAliases(): string[] {
  return [...new Set([$selectedStoredSessionId.get(), $activeSessionId.get()].filter((id): id is string => !!id))]
}

function targetSessionIds(targets: DfmViewerTargets): string[] {
  return currentSessionAliases().filter(id => Boolean(targets[id]))
}

function canonicalSessionId(sessionId: string): string {
  const activeSessionId = $activeSessionId.get()
  const storedSessionId = $selectedStoredSessionId.get()

  if (storedSessionId && (!sessionId || sessionId === activeSessionId || sessionId === storedSessionId)) {
    return storedSessionId
  }

  return sessionId || storedSessionId || activeSessionId || ''
}

export const $dfmViewerTarget = computed(
  [$dfmViewerTargets, $dismissedDfmViewers, $activeSessionId, $selectedStoredSessionId],
  (targets, dismissed) => {
    const id = targetSessionIds(targets).find(candidate => !dismissed[candidate])

    return id ? targets[id] : null
  }
)

// Closing the tab hides it without discarding the session's last model. This
// separate value powers the explicit reopen affordance.
export const $recoverableDfmViewerTarget = computed(
  [$dfmViewerTargets, $activeSessionId, $selectedStoredSessionId],
  targets => {
    const id = targetSessionIds(targets)[0]

    return id ? targets[id] : null
  }
)

function currentSessionId(): string {
  return $selectedStoredSessionId.get() || $activeSessionId.get() || ''
}

export function showDfmViewer(
  sessionId: string,
  target: DfmViewerTarget,
  { activate = true }: { activate?: boolean } = {}
) {
  const id = canonicalSessionId(sessionId)

  if (!id) {
    return
  }

  const targets = { ...$dfmViewerTargets.get(), [id]: target }
  const activeSessionId = $activeSessionId.get()

  // A live gateway event arrives with the ephemeral runtime id. Once the
  // persisted id is known, keep one canonical session record so a later resume
  // cannot race two aliases with different dismissal state.
  if (id === $selectedStoredSessionId.get() && activeSessionId && activeSessionId !== id) {
    delete targets[activeSessionId]
  }

  $dfmViewerTargets.set(targets)

  const aliases = new Set([id, ...currentSessionAliases()])

  if ([...aliases].some(alias => $dismissedDfmViewers.get()[alias])) {
    const dismissed = { ...$dismissedDfmViewers.get() }

    for (const alias of aliases) {
      delete dismissed[alias]
    }

    $dismissedDfmViewers.set(dismissed)
  }

  if (activate) {
    setPaneOpen(PREVIEW_PANE_ID, true)
    selectRightRailTab(RIGHT_RAIL_DFM_TAB_ID)
  }
}

export function dismissDfmViewer(sessionId = currentSessionId()) {
  const targets = $dfmViewerTargets.get()
  const aliases = new Set([sessionId, ...currentSessionAliases()].filter(id => Boolean(targets[id])))

  if (aliases.size === 0) {
    return
  }

  const dismissed = { ...$dismissedDfmViewers.get() }

  for (const alias of aliases) {
    dismissed[alias] = true
  }

  $dismissedDfmViewers.set(dismissed)
}

export function restoreDfmViewer(sessionId = currentSessionId()): boolean {
  const targets = $dfmViewerTargets.get()
  const aliases = new Set([sessionId, ...currentSessionAliases()].filter(id => Boolean(targets[id])))

  if (aliases.size === 0) {
    return false
  }

  const dismissed = { ...$dismissedDfmViewers.get() }

  for (const alias of aliases) {
    delete dismissed[alias]
  }

  $dismissedDfmViewers.set(dismissed)
  setPaneOpen(PREVIEW_PANE_ID, true)
  selectRightRailTab(RIGHT_RAIL_DFM_TAB_ID)

  return true
}

export function clearDfmViewers() {
  $dfmViewerTargets.set({})
  $dismissedDfmViewers.set({})
}
