import { afterEach, describe, expect, it } from 'vitest'

import {
  $composerAttachments,
  ATTACHMENT_UPLOAD_TOKEN,
  addComposerAttachment,
  clearComposerAttachments,
  clearSessionDraft,
  type ComposerAttachment,
  removeComposerAttachment,
  SESSION_DRAFTS_STORAGE_KEY,
  stashSessionDraft,
  takeSessionDraft,
  updateComposerAttachment
} from './composer'

function attachment(overrides: Partial<ComposerAttachment> & Pick<ComposerAttachment, 'id'>): ComposerAttachment {
  return { kind: 'file', label: 'doc.pdf', ...overrides }
}

describe('updateComposerAttachment', () => {
  afterEach(() => {
    $composerAttachments.set([])
  })

  it('replaces an existing attachment in place', () => {
    addComposerAttachment(attachment({ id: 'file:a', uploadState: 'uploading' }))

    const updated = updateComposerAttachment(attachment({ id: 'file:a', attachedSessionId: 'sess-1' }))

    expect(updated).toBe(true)
    const current = $composerAttachments.get()
    expect(current).toHaveLength(1)
    expect(current[0]?.attachedSessionId).toBe('sess-1')
    expect(current[0]?.uploadState).toBeUndefined()
  })

  it('does NOT resurrect an attachment the user removed mid-upload', () => {
    // Drop → eager upload starts → user removes the chip → upload resolves.
    // The late success must not re-add the removed attachment.
    addComposerAttachment(attachment({ id: 'file:a', uploadState: 'uploading' }))
    removeComposerAttachment('file:a')

    const updated = updateComposerAttachment(attachment({ id: 'file:a', attachedSessionId: 'sess-1' }))

    expect(updated).toBe(false)
    expect($composerAttachments.get()).toHaveLength(0)
  })

  it('does not let an old upload overwrite or clear a re-added attachment with the same id', () => {
    addComposerAttachment(attachment({ id: 'file:a' }))
    const original = $composerAttachments.get()[0]!
    removeComposerAttachment(original.id)
    addComposerAttachment(original)
    const readded = $composerAttachments.get()[0]!
    expect(readded[ATTACHMENT_UPLOAD_TOKEN]).not.toBe(original[ATTACHMENT_UPLOAD_TOKEN])
    expect(updateComposerAttachment({ ...original, refText: '@file:old' }, original[ATTACHMENT_UPLOAD_TOKEN])).toBe(
      false
    )
    clearComposerAttachments([original])
    expect($composerAttachments.get()).toEqual([readded])
  })

  it('clears only submitted instances and retains attachments added during the send', () => {
    addComposerAttachment(attachment({ id: 'file:a' }))
    const sent = $composerAttachments.get()[0]!
    addComposerAttachment(attachment({ id: 'file:b' }))
    clearComposerAttachments([sent])
    expect($composerAttachments.get().map(item => item.id)).toEqual(['file:b'])
  })
})

describe('session drafts', () => {
  afterEach(() => {
    for (const scope of ['session-a', 'session-b', null]) {
      clearSessionDraft(scope)
    }

    window.localStorage.clear()
  })

  it('keeps drafts isolated per session scope', () => {
    stashSessionDraft('session-a', 'draft a', [])
    stashSessionDraft('session-b', 'draft b', [attachment({ id: 'image:b', kind: 'image' })])

    expect(takeSessionDraft('session-a')).toEqual({ attachments: [], text: 'draft a' })
    expect(takeSessionDraft('session-b').text).toBe('draft b')
    expect(takeSessionDraft('session-b').attachments.map(a => a.id)).toEqual(['image:b'])
  })

  it('scopes the unsaved new-session draft separately from real sessions', () => {
    stashSessionDraft(null, 'new chat draft', [])
    stashSessionDraft('session-a', 'session draft', [])

    expect(takeSessionDraft(null).text).toBe('new chat draft')
    expect(takeSessionDraft(undefined).text).toBe('new chat draft')
    expect(takeSessionDraft('session-a').text).toBe('session draft')
  })

  it('persists draft text (not attachments) to localStorage', () => {
    stashSessionDraft('session-a', 'survives reload', [attachment({ id: 'file:a' })])

    const persisted = JSON.parse(window.localStorage.getItem(SESSION_DRAFTS_STORAGE_KEY) ?? '{}') as Record<
      string,
      string
    >

    expect(persisted['session-a']).toBe('survives reload')
  })

  it('evicts empty drafts instead of leaving stale entries behind', () => {
    stashSessionDraft('session-a', 'saved', [])
    stashSessionDraft('session-a', '   ', [])

    expect(takeSessionDraft('session-a')).toEqual({ attachments: [], text: '' })
  })

  it('clears a stashed draft after an accepted submit', () => {
    stashSessionDraft('session-a', 'sent prompt', [attachment({ id: 'file:a' })])
    clearSessionDraft('session-a')

    expect(takeSessionDraft('session-a')).toEqual({ attachments: [], text: '' })
  })

  it('returns clones so callers cannot mutate the stash', () => {
    stashSessionDraft('session-a', 'draft', [attachment({ id: 'file:a' })])

    const taken = takeSessionDraft('session-a')
    taken.attachments[0]!.label = 'mutated'

    expect(takeSessionDraft('session-a').attachments[0]?.label).toBe('doc.pdf')
  })

  it('preserves upload identity in memory across draft clones without persisting attachments', () => {
    const original = attachment({ id: 'file:a', [ATTACHMENT_UPLOAD_TOKEN]: 'temporary-token' })
    stashSessionDraft('session-a', 'draft', [original])
    const restored = takeSessionDraft('session-a').attachments[0]!
    expect(restored).not.toBe(original)
    expect(restored[ATTACHMENT_UPLOAD_TOKEN]).toBe(original[ATTACHMENT_UPLOAD_TOKEN])
    expect(JSON.stringify({ attachments: [restored] })).not.toContain('temporary-token')
    expect(window.localStorage.getItem(SESSION_DRAFTS_STORAGE_KEY)).not.toContain('temporary-token')
  })
})
