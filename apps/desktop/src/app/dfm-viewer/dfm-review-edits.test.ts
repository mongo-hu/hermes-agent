import { describe, expect, it } from 'vitest'

import type { ReviewContext } from './dfm-review'
import { captureReviewEdit, reviewEditUnchanged } from './dfm-review-edits'

function context(): ReviewContext {
  const refs = [1, 2].map(index => ({
    kind: 'face' as const,
    index,
    input_sha256: 'input',
    topology_snapshot_id: 'topology'
  }))

  return {
    project_id: 'project',
    revision: 1,
    snapshot_id: 'review-A',
    review_groups: [],
    features: [
      {
        feature_id: 'BOSS',
        kind: 'hollow_boss',
        confidence: 1,
        properties: { geometry_refs: refs, parts: { outer_wall: [refs[0]] } }
      }
    ],
    forming_scopes: [
      { forming_scope_id: 'SIDE', kind: 'undercut', mode: 'local', draw_direction: [0, 0, 1], geometry_refs: refs }
    ],
    annotations: [
      { annotation_id: 'LOOK', kind: 'appearance', value: 'appearance', status: 'active', geometry_refs: refs }
    ]
  }
}

describe('review edit conflict boundaries', () => {
  it('allows a newer review of the same target without depending on unrelated groups', () => {
    const old = context()
    const base = captureReviewEdit(old, 'scope', 'SIDE')
    const latest = context()
    latest.revision++
    latest.snapshot_id = 'review-B'
    latest.review_groups.push({
      review_group_id: 'unrelated',
      kind: 'ordinary_structure',
      target_refs: ['BOSS'],
      status: 'confirmed'
    })
    latest.forming_scopes[0].geometry_refs.reverse()
    expect(reviewEditUnchanged(base, latest)).toBe(true)
  })

  it('detects a changed or removed direction target instead of replacing its vector', () => {
    const base = captureReviewEdit(context(), 'scope', 'SIDE')
    const latest = context()
    latest.forming_scopes[0].draw_direction = [0, 1, 0]
    expect(reviewEditUnchanged(base, latest)).toBe(false)
    latest.forming_scopes = []
    expect(reviewEditUnchanged(base, latest)).toBe(false)
  })

  it('keeps equal face numbers in a different model or topology separate', () => {
    const base = captureReviewEdit(context(), 'feature', 'BOSS')
    const latest = context()
    latest.features[0].properties.geometry_refs![0].topology_snapshot_id = 'other-topology'
    expect(reviewEditUnchanged(base, latest)).toBe(false)
  })

  it('detects part edits and annotation changes even when their face counts match', () => {
    const original = context()
    const base = captureReviewEdit(original, 'feature', 'BOSS')
    const latest = context()
    latest.features[0].properties.parts = { outer_wall: [latest.features[0].properties.geometry_refs![1]] }
    expect(reviewEditUnchanged(base, latest)).toBe(false)
    const annotation = captureReviewEdit(original, 'annotation', 'LOOK')
    latest.annotations[0].value = 'non_appearance'
    expect(reviewEditUnchanged(annotation, latest)).toBe(false)
  })
})
