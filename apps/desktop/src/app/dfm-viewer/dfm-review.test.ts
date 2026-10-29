import { describe, expect, it } from 'vitest'

import {
  confirmedFeatureIds,
  directionUnlocked,
  parseReviewDirection,
  reviewBuckets,
  type ReviewContext,
  reviewGeometry,
  type ReviewGroup
} from './dfm-review'

function context(): ReviewContext {
  return {
    project_id: 'p',
    revision: 3,
    annotations: [],
    features: [
      {
        feature_id: 'BOSS_1',
        kind: 'hollow_boss',
        confidence: 1,
        properties: { geometry_refs: [{ kind: 'face', index: 1 }] }
      },
      {
        feature_id: 'BOSS_2',
        kind: 'hollow_boss',
        confidence: 1,
        properties: { geometry_refs: [{ kind: 'face', index: 2 }] }
      },
      {
        feature_id: 'WALL',
        kind: 'main_wall',
        confidence: 1,
        properties: { geometry_refs: [{ kind: 'face', index: 3 }] }
      }
    ],
    forming_scopes: [
      {
        forming_scope_id: 'SIDE',
        kind: 'undercut',
        mode: 'local',
        draw_direction: null,
        geometry_refs: [{ kind: 'face', index: 1 }]
      }
    ],
    review_groups: [
      { review_group_id: 'b1', kind: 'ordinary_structure', target_refs: ['BOSS_1'], status: 'pending_review' },
      { review_group_id: 'b2', kind: 'ordinary_structure', target_refs: ['BOSS_2'], status: 'confirmed' },
      { review_group_id: 'w', kind: 'ordinary_structure', target_refs: ['WALL'], status: 'pending_review' }
    ]
  }
}

describe('DFM review state', () => {
  it('groups by category without merging independent confirmations', () => {
    const buckets = reviewBuckets(context())
    expect(buckets.find(bucket => bucket.id === 'hollow_boss')?.groups.map(group => group.review_group_id)).toEqual([
      'b1'
    ])
    expect(buckets.find(bucket => bucket.id === 'hollow_boss')?.label).toBe('带孔 Boss 柱')
    expect(buckets.find(bucket => bucket.id === 'main_wall')?.groups).toHaveLength(1)
  })

  it('moves only confirmed structures into results and sends revised structures back to pending', () => {
    const state = context()
    expect([...confirmedFeatureIds(state.review_groups)]).toEqual(['BOSS_2'])
    state.review_groups[0].status = 'confirmed'
    expect([...confirmedFeatureIds(state.review_groups)]).toEqual(['BOSS_1', 'BOSS_2'])
    expect(
      reviewBuckets(state)
        .flatMap(bucket => bucket.groups)
        .map(group => group.target_refs[0])
    ).toEqual(['WALL'])
    state.review_groups[0].status = 'stale'
    state.review_groups.push({ ...state.review_groups[0], review_group_id: 'revised', status: 'pending_review' })
    expect([...confirmedFeatureIds(state.review_groups)]).toEqual(['BOSS_2'])
    expect(
      reviewBuckets(state)
        .flatMap(bucket => bucket.groups)
        .some(group => group.review_group_id === 'revised')
    ).toBe(true)
  })

  it('locates scope-only targets and deduplicates shared feature faces', () => {
    const group: ReviewGroup = {
      review_group_id: 'local',
      kind: 'local_structure',
      target_refs: ['SIDE', 'BOSS_1'],
      status: 'pending_review'
    }

    expect(reviewGeometry(context(), group)).toEqual([{ kind: 'face', index: 1 }])
  })

  it('unlocks local direction only after its own range and structures are confirmed', () => {
    const state = context()

    const group: ReviewGroup = {
      review_group_id: 'direction',
      kind: 'local_direction',
      target_refs: ['SIDE'],
      status: 'pending_review'
    }

    expect(directionUnlocked(state, group)).toBe(false)
    state.review_groups.push(
      ...['undercut_scope', 'local_structure'].map(kind => ({ ...group, kind, status: 'confirmed' as const }))
    )
    expect(directionUnlocked(state, group)).toBe(true)
    state.review_groups.push({ ...group, kind: 'local_structure', target_refs: ['OTHER'], status: 'pending_review' })
    expect(directionUnlocked(state, group)).toBe(true)
  })

  it('does not infer a direction from missing, zero or invalid input', () => {
    for (const value of [
      ['', '', ''],
      ['0', '0', '0'],
      ['NaN', '0', '1'],
      ['Infinity', '0', '1']
    ])
      {expect(parseReviewDirection(value)).toBeNull()}

    expect(parseReviewDirection(['1', '2', '-3'])).toEqual([1, 2, -3])
  })
})
