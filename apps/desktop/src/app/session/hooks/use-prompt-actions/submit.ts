import { type MutableRefObject, useCallback } from 'react'

import { PROMPT_SUBMIT_REQUEST_TIMEOUT_MS } from '@/hermes'
import type { Translations } from '@/i18n'
import { type ChatMessage, textPart } from '@/lib/chat-messages'
import { optimisticAttachmentRef } from '@/lib/chat-runtime'
import { setMutableRef } from '@/lib/mutable-ref'
import { isDfmStepModelPath } from '@/lib/product-mode'
import {
  $composerAttachments,
  clearComposerAttachments,
  type ComposerAttachment,
  terminalContextBlocksFromDraft
} from '@/store/composer'
import { clearNotifications, notify, notifyError } from '@/store/notifications'
import { requestDesktopOnboarding } from '@/store/onboarding'
import { setActiveSessionId, setAwaitingResponse, setBusy, setMessages } from '@/store/session'
import { $clarifyRequests, restorePendingClarify } from '@/store/clarify'

import type { ClientSessionState } from '../../../types'

import {
  _submitInFlight,
  type GatewayRequest,
  inlineErrorMessage,
  isGatewayTimeoutError,
  isProviderSetupError,
  isSessionBusyError,
  isSessionNotFoundError,
  type SubmitTextOptions,
  withSessionBusyRetry
} from './utils'

interface SubmitPromptDeps {
  activeSessionId: string | null
  activeSessionIdRef: MutableRefObject<string | null>
  busyRef: MutableRefObject<boolean>
  copy: Translations['desktop']
  createBackendSessionForSend: (preview?: string | null) => Promise<string | null>
  newAnalysisOnModelSubmit: boolean
  requestGateway: GatewayRequest
  selectedStoredSessionIdRef: MutableRefObject<string | null>
  syncAttachmentsForSubmit: (
    sessionId: string,
    attachments: ComposerAttachment[],
    options?: { updateComposerAttachments?: boolean }
  ) => Promise<ComposerAttachment[]>
  updateSessionState: (
    sessionId: string,
    updater: (state: ClientSessionState) => ClientSessionState,
    storedSessionId?: string | null
  ) => ClientSessionState
}

const DFM_NEW_ANALYSIS_INSTRUCTION =
  '为本次附带的 STEP 模型执行一次独立的 DFM 分析。即使文件与历史任务相同，也须新建 DFM 项目、登记本次附件并重新确认适用条件；不要沿用旧项目、材料或运行结果。'

/** The prompt submit pipeline, extracted from usePromptActions. */
export function useSubmitPrompt(deps: SubmitPromptDeps) {
  const {
    activeSessionId,
    activeSessionIdRef,
    busyRef,
    copy,
    createBackendSessionForSend,
    newAnalysisOnModelSubmit,
    requestGateway,
    selectedStoredSessionIdRef,
    syncAttachmentsForSubmit,
    updateSessionState
  } = deps

  return useCallback(
    async (rawText: string, options?: SubmitTextOptions) => {
      // Review recovery is an idle-only continuation of the original session.
      // Keep it outside the composer/new-session/interrupt-and-queue pipeline.
      if (options?.dfmContinuation) {
        const continuation = options.dfmContinuation
        const owner = continuation.expected_session_key
        const currentOwner = () => selectedStoredSessionIdRef.current || activeSessionIdRef.current
        if (currentOwner() !== owner) {throw new Error('会话已切换，请返回原工程后继续。')}
        if (_submitInFlight.has(owner) || busyRef.current) {throw new Error('当前会话正在执行，请完成后重试继续。')}
        _submitInFlight.add(owner)
        try {
          let sid = activeSessionIdRef.current
          if (!sid) {
            const pendingBeforeResume = $clarifyRequests.get()
            const resumed = await requestGateway<{ session_id: string; pending_clarify?: unknown }>('session.resume', {
              session_id: owner, source: 'desktop'
            })
            if (currentOwner() !== owner) {throw new Error('会话已切换，请返回原工程后继续。')}
            if (!resumed?.session_id) {throw new Error('无法恢复原会话，请重试继续。')}
            sid = resumed.session_id
            activeSessionIdRef.current = sid
            setActiveSessionId(sid)
            if ('pending_clarify' in resumed) {restorePendingClarify(resumed.pending_clarify, sid, pendingBeforeResume[sid] ?? null)}
          }
          if (currentOwner() !== owner) {throw new Error('会话已切换，请返回原工程后继续。')}
          let previous!: ClientSessionState
          const optimistic = updateSessionState(sid, state => {
            previous = state
            return {
              ...state, busy: true, awaitingResponse: true, interrupted: false, needsInput: false,
              messages: state.messages.some(message => message.id === continuation.action_key) ? state.messages :
                [...state.messages, { id: continuation.action_key, role: 'user', parts: [textPart(rawText)], attachmentRefs: [] }]
            }
          }, owner)
          const undoOptimistic = () => updateSessionState(sid, state => {
            // The session cache passes a shallow copy to each updater. Compare
            // only this turn's state, preserving unrelated usage/cache updates.
            const untouched = state.messages === optimistic.messages && state.busy === optimistic.busy &&
              state.awaitingResponse === optimistic.awaitingResponse && state.needsInput === optimistic.needsInput &&
              state.interrupted === optimistic.interrupted
            return {
              ...state,
              ...(untouched ? { busy: previous.busy, awaitingResponse: previous.awaitingResponse,
                needsInput: previous.needsInput, interrupted: previous.interrupted } : {}),
              messages: previous.messages.some(message => message.id === continuation.action_key) ? state.messages :
                state.messages.filter(message => message.id !== continuation.action_key)
            }
          }, owner)
          try {
            const result = await requestGateway<{ status?: string; running?: boolean }>('prompt.submit', {
              session_id: sid, text: rawText, attachments: [], dfm_continuation: continuation
            }, PROMPT_SUBMIT_REQUEST_TIMEOUT_MS)
            if (result.status === 'completed' || result.status === 'active_run' || result.status === 'duplicate') {
              undoOptimistic()
            }
            // Streaming events can precede this ACK. Never replace their busy,
            // needs-input or completed state with an older submit state.
          } catch (error) {
            undoOptimistic()
            if (isSessionNotFoundError(error) && currentOwner() === owner && activeSessionIdRef.current === sid) {
              activeSessionIdRef.current = null
              setActiveSessionId(null)
            }
            throw error
          }
          return true
        } finally {
          _submitInFlight.delete(owner)
        }
      }
      const visibleText = rawText.trim()
      const usingComposerAttachments = !options?.attachments

      // Drop undefined/null holes a session switch or draft restore can leave in
      // the attachments array (same bug class as AttachmentList #49624). Without
      // this, the sibling iterations below (a.kind / a.label / a.refText, and the
      // sync step) throw "Cannot read properties of undefined (reading 'refText')"
      // and break the chat surface.
      const attachments = (options?.attachments ?? $composerAttachments.get()).filter((a): a is ComposerAttachment =>
        Boolean(a)
      )
      const dfmModel = newAnalysisOnModelSubmit
        ? attachments.find(
            attachment => attachment.kind === 'file' && isDfmStepModelPath(attachment.path || attachment.label)
          )
        : undefined
      const startSeparateAnalysis = Boolean(dfmModel && (activeSessionId || selectedStoredSessionIdRef.current))

      const terminalContextBlocks = terminalContextBlocksFromDraft(rawText).join('\n\n')
      const hasImage = attachments.some(a => a.kind === 'image')

      // Refs are recomputed after sync (file.attach rewrites @file: refs to
      // workspace-relative paths the remote gateway can resolve). Seed the
      // optimistic message with the pre-sync refs, then rewrite once synced.
      // Images use their base64 preview so the thumbnail renders inline without
      // a (remote-mode 403-prone) /api/media fetch — see optimisticAttachmentRef.
      let attachmentRefs = attachments.map(optimisticAttachmentRef).filter((r): r is string => Boolean(r))

      const buildContextText = (atts: ComposerAttachment[]): string => {
        // atts may be the post-sync array, which can reintroduce holes; filter
        // before touching a.refText / a.kind.
        const present = atts.filter((a): a is ComposerAttachment => Boolean(a))

        const contextRefs = present
          .map(a => a.refText)
          .filter(Boolean)
          .join('\n')

        return (
          [contextRefs, terminalContextBlocks, visibleText, dfmModel && DFM_NEW_ANALYSIS_INSTRUCTION]
            .filter(Boolean)
            .join('\n\n') ||
          (present.some(a => a.kind === 'image') ? 'What do you see in this image?' : '')
        )
      }

      // Queue drains fire on the busy→false settle edge, where busyRef (synced
      // from $busy by a separate effect) may still read true — honoring it would
      // bounce the drained send. The drain lock serializes them; the user path
      // keeps the guard so a stray Enter mid-turn can't double-submit.
      const hasSendable = Boolean(visibleText || terminalContextBlocks || attachments.length || hasImage)

      if (!hasSendable || (!options?.fromQueue && busyRef.current)) {
        return false
      }

      // One submit in flight per session — drop any concurrent re-fire so a
      // stalled turn can't stack the same prompt into multiple real turns.
      const submitLockKey = selectedStoredSessionIdRef.current || activeSessionId || '__pending_new__'

      if (_submitInFlight.has(submitLockKey)) {
        return false
      }

      _submitInFlight.add(submitLockKey)
      let submitLockReleased = false

      const releaseSubmitLock = () => {
        if (!submitLockReleased) {
          submitLockReleased = true
          _submitInFlight.delete(submitLockKey)
        }
      }

      const optimisticId = `user-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

      const buildUserMessage = (): ChatMessage => ({
        id: optimisticId,
        role: 'user',
        parts: [textPart(visibleText || (attachmentRefs.length ? '' : attachments.map(a => a.label).join(', ')))],
        attachmentRefs
      })

      const releaseBusy = () => {
        releaseSubmitLock()
        setMutableRef(busyRef, false)
        setBusy(false)
        setAwaitingResponse(false)
      }

      // Idempotent optimistic insert — re-running with the resolved sessionId
      // after createBackendSessionForSend just overwrites with the same id.
      const seedOptimistic = (sid: string) =>
        updateSessionState(
          sid,
          state => ({
            ...state,
            messages: state.messages.some(m => m.id === optimisticId)
              ? state.messages
              : [...state.messages, buildUserMessage()],
            busy: true,
            awaitingResponse: true,
            pendingBranchGroup: null,
            sawAssistantPayload: false,
            // Fresh submit = new turn — clear any leftover interrupt flag, else
            // mutateStream/completeAssistantMessage drop every delta of this turn
            // (what made drained-after-interrupt sends go silent).
            interrupted: false
          }),
          selectedStoredSessionIdRef.current
        )

      // After sync rewrites refs, refresh the optimistic message in place so the
      // transcript shows the resolved @file: ref rather than the local path.
      const rewriteOptimistic = (sid: string) =>
        updateSessionState(
          sid,
          state => ({
            ...state,
            messages: state.messages.map(message => (message.id === optimisticId ? buildUserMessage() : message))
          }),
          selectedStoredSessionIdRef.current
        )

      const dropOptimistic = (sid: null | string) => {
        if (!sid) {
          setMessages(current => current.filter(m => m.id !== optimisticId))

          return
        }

        updateSessionState(
          sid,
          state => ({
            ...state,
            messages: state.messages.filter(m => m.id !== optimisticId),
            busy: false,
            awaitingResponse: false,
            pendingBranchGroup: null
          }),
          selectedStoredSessionIdRef.current
        )
      }

      setMutableRef(busyRef, true)
      setBusy(true)
      setAwaitingResponse(true)
      clearNotifications()

      let sessionId: null | string = startSeparateAnalysis ? null : activeSessionId

      if (sessionId) {
        seedOptimistic(sessionId)
      } else if (!startSeparateAnalysis) {
        setMessages(current => [...current, buildUserMessage()])
      }

      if (!startSeparateAnalysis && !sessionId && selectedStoredSessionIdRef.current) {
        // A stored session is SELECTED but its runtime binding is gone (the
        // live session was orphan-reaped, or a timeout/reconnect cleared
        // activeSessionId). Continuing the selected conversation must mean
        // resuming it — minting a brand-new backend session here silently
        // splits the user's chat in two (#55578 symptom b). Only fall through
        // to session creation when NO stored session is selected (a genuine
        // new-chat draft).
        try {
          const resumed = await requestGateway<{ session_id: string }>('session.resume', {
            session_id: selectedStoredSessionIdRef.current
          })

          if (resumed?.session_id) {
            sessionId = resumed.session_id
            activeSessionIdRef.current = sessionId
          }
        } catch {
          // Resume failed (session gone from state.db, gateway hiccup) —
          // fall through to creating a fresh session rather than dead-ending
          // the user's message.
        }

        if (sessionId) {
          seedOptimistic(sessionId)
        }
      }

      if (!sessionId) {
        try {
          sessionId = await createBackendSessionForSend(dfmModel ? `DFM · ${dfmModel.label}` : visibleText)
        } catch (err) {
          dropOptimistic(null)
          releaseBusy()
          notifyError(err, copy.sessionUnavailable)

          return false
        }

        if (!sessionId) {
          dropOptimistic(null)
          releaseBusy()
          notify({ kind: 'error', title: copy.sessionUnavailable, message: copy.createSessionFailed })

          return false
        }

        seedOptimistic(sessionId)
      }

      try {
        const syncedAttachments = await syncAttachmentsForSubmit(sessionId, attachments, {
          updateComposerAttachments: usingComposerAttachments
        })

        // Rewrite the optimistic message + prompt text with the synced refs so
        // the gateway receives @file: paths that resolve in its workspace.
        // (Images keep their inline base64 preview — see optimisticAttachmentRef.)
        attachmentRefs = syncedAttachments.map(optimisticAttachmentRef).filter((r): r is string => Boolean(r))
        rewriteOptimistic(sessionId)
        const text = buildContextText(syncedAttachments)

        // On sleep/wake the gateway's in-memory session may have been cleared
        // while the desktop app still holds the old session ID. Detect this,
        // resume the stored session to re-register it, and retry once.
        let submitErr: unknown = null

        try {
          await withSessionBusyRetry(() =>
            requestGateway('prompt.submit', { session_id: sessionId, text }, PROMPT_SUBMIT_REQUEST_TIMEOUT_MS)
          )
        } catch (firstErr) {
          if (
            (isSessionNotFoundError(firstErr) || isGatewayTimeoutError(firstErr)) &&
            selectedStoredSessionIdRef.current
          ) {
            // Re-register the session in the gateway and get a fresh live ID.
            // Timeouts recover the same way as "session not found": a starved
            // backend loop (#55578 symptom d) rejects the submit even though
            // the stored session is fine — resume + retry instead of erroring
            // out and losing the session binding.
            const resumed = await requestGateway<{ session_id: string }>('session.resume', {
              session_id: selectedStoredSessionIdRef.current,
              source: 'desktop'
            })

            const recoveredId = resumed?.session_id

            if (recoveredId) {
              activeSessionIdRef.current = recoveredId
              await withSessionBusyRetry(() =>
                requestGateway('prompt.submit', { session_id: recoveredId, text }, PROMPT_SUBMIT_REQUEST_TIMEOUT_MS)
              )
              sessionId = recoveredId
            } else {
              submitErr = firstErr
            }
          } else {
            submitErr = firstErr
          }
        }

        if (submitErr !== null) {
          throw submitErr
        }

        if (usingComposerAttachments && activeSessionIdRef.current === sessionId) {
          clearComposerAttachments(syncedAttachments)
        }

        // Submit landed — the turn now runs (busy stays true), but the submit
        // window is closed, so release the lock for the next (sequential) send.
        releaseSubmitLock()

        return true
      } catch (err) {
        releaseBusy()

        // A queued drain that raced a not-yet-settled turn gets a transient
        // "session busy" (4009). Don't surface an error bubble/toast — the entry
        // stays queued and the composer's bounded auto-drain retries when idle.
        if (options?.fromQueue && isSessionBusyError(err)) {
          return false
        }

        const message = inlineErrorMessage(err, copy.promptFailed)

        updateSessionState(sessionId, state => ({
          ...state,
          messages: [
            ...state.messages,
            {
              id: `assistant-error-${Date.now()}`,
              role: 'assistant',
              parts: [],
              error: message || copy.promptFailed,
              branchGroupId: state.pendingBranchGroup ?? undefined
            }
          ],
          busy: false,
          awaitingResponse: false,
          pendingBranchGroup: null,
          sawAssistantPayload: true
        }))

        if (isProviderSetupError(err)) {
          requestDesktopOnboarding(copy.providerCredentialRequired)

          return false
        }

        notifyError(err, copy.promptFailed)

        return false
      }
    },
    [
      activeSessionId,
      activeSessionIdRef,
      busyRef,
      copy,
      createBackendSessionForSend,
      newAnalysisOnModelSubmit,
      requestGateway,
      selectedStoredSessionIdRef,
      syncAttachmentsForSubmit,
      updateSessionState
    ]
  )
}
