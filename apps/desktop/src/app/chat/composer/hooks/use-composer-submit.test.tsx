import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  ATTACHMENT_UPLOAD_TOKEN,
  clearSessionDraft,
  type ComposerAttachment,
  stashSessionDraft,
  takeSessionDraft
} from '@/store/composer'

import { useComposerSubmit } from './use-composer-submit'

afterEach(() => {
  cleanup()
  clearSessionDraft('session-a')
  clearSessionDraft('session-b')
})

function setup() {
  const attachment: ComposerAttachment = {
    id: 'file:model.stp',
    kind: 'file',
    label: 'model.stp',
    path: '/a/model.stp',
    [ATTACHMENT_UPLOAD_TOKEN]: 'a-upload'
  }
  const scopeRef = { current: 'session-a' as string | null }
  let resolveSubmit!: (accepted: boolean) => void
  let rejectSubmit!: (error: Error) => void
  const pending = new Promise<boolean>((resolve, reject) => {
    resolveSubmit = resolve
    rejectSubmit = reject
  })
  const onSubmit = vi.fn(() => pending)
  const loadIntoComposer = vi.fn()
  const stashAt = vi.fn((scope: string | null, text = '', attachments: ComposerAttachment[] = []) =>
    stashSessionDraft(scope, text, attachments)
  )
  stashSessionDraft('session-a', 'A draft', [attachment])

  const hook = renderHook(
    ({ scope }: { scope: string }) => {
      scopeRef.current = scope
      return useComposerSubmit({
        activeQueueSessionKey: scope,
        activeQueueSessionKeyRef: scopeRef,
        attachments: [attachment],
        busy: false,
        canSteer: false,
        clearDraft: vi.fn(),
        disabled: false,
        draftRef: { current: 'A draft' },
        drainNextQueued: vi.fn(async () => false),
        editorRef: { current: null },
        exitQueuedEdit: vi.fn(() => false),
        focusInput: vi.fn(),
        inputDisabled: false,
        loadIntoComposer,
        onCancel: vi.fn(),
        onSteer: vi.fn(() => false),
        onSubmit,
        queueCurrentDraft: vi.fn(() => false),
        queueEdit: null,
        queuedPrompts: [],
        sessionId: scope,
        setComposerText: vi.fn(),
        stashAt
      })
    },
    { initialProps: { scope: 'session-a' } }
  )

  return { ...hook, attachment, loadIntoComposer, onSubmit, rejectSubmit, resolveSubmit, stashAt }
}

describe('composer submit draft ownership', () => {
  it.each(['success', 'false', 'throw'] as const)('keeps the next session intact after a late %s', async outcome => {
    const task = setup()
    act(() => task.result.current.dispatchSubmit('A draft', [task.attachment]))
    expect(task.onSubmit).toHaveBeenCalledWith('A draft', { attachments: [task.attachment] })

    const nextAttachment = { ...task.attachment, path: '/b/model.stp', [ATTACHMENT_UPLOAD_TOKEN]: 'b-upload' }
    stashSessionDraft('session-b', 'B draft', [nextAttachment])
    task.rerender({ scope: 'session-b' })
    await act(async () => {
      if (outcome === 'throw') task.rejectSubmit(new Error('upload failed'))
      else task.resolveSubmit(outcome === 'success')
    })

    expect(task.loadIntoComposer).not.toHaveBeenCalled()
    expect(takeSessionDraft('session-b')).toEqual({ text: 'B draft', attachments: [nextAttachment] })
    expect(takeSessionDraft('session-a')).toEqual(
      outcome === 'success' ? { text: '', attachments: [] } : { text: 'A draft', attachments: [task.attachment] }
    )
    if (outcome === 'success') expect(task.stashAt).not.toHaveBeenCalled()
    else expect(task.stashAt).toHaveBeenCalledWith('session-a', 'A draft', [task.attachment])
  })

  it('still restores a failed submit when its session remains current', async () => {
    const task = setup()
    act(() => task.result.current.dispatchSubmit('A draft', [task.attachment]))
    await act(async () => task.resolveSubmit(false))

    expect(task.loadIntoComposer).toHaveBeenCalledWith('A draft', [task.attachment])
    expect(task.stashAt).toHaveBeenCalledWith('session-a', 'A draft', [task.attachment])
    expect(takeSessionDraft('session-a')).toEqual({ text: 'A draft', attachments: [task.attachment] })
  })
})
