import { describe, expect, it } from 'vitest'

import {
  confirmableReviewGroupIds,
  confirmedFeatureIds,
  directionUnlocked,
  formingDirectionLabel,
  formingPlanMissing,
  parseReviewDirection,
  removableReviewGroup,
  reviewBuckets,
  type ReviewContext,
  reviewEditTimeout,
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
        mechanism_kind: 'undetermined', undercut_geometry_refs: [{ kind: 'face', index: 1 }], candidate_solutions: [], coordinate_system: 'model', direction_source: null,
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
  it('uses the backend execution budget for edits instead of the generic API timeout', () => {
    expect(reviewEditTimeout({ edit_timeout_ms: 235_000 })).toBe(235_000)

    for (const value of [undefined, 0, -1, NaN, Infinity]) {
      expect(reviewEditTimeout({ edit_timeout_ms: value })).toBeGreaterThan(15_000)
    }
  })

  it('groups by category without merging independent confirmations', () => {
    const buckets = reviewBuckets(context())
    expect(
      buckets.find(bucket => bucket.id === 'structure:hollow_boss')?.groups.map(group => group.review_group_id)
    ).toEqual(['b1'])
    expect(buckets.find(bucket => bucket.id === 'structure:hollow_boss')?.label).toBe('带孔 Boss 柱')
    expect(buckets.find(bucket => bucket.id === 'structure:main_wall')?.groups).toHaveLength(1)
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
      kind: 'forming_scope',
      target_refs: ['SIDE', 'BOSS_1'],
      status: 'pending_review'
    }

    expect(reviewGeometry(context(), group)).toEqual([{ kind: 'face', index: 1 }])
  })

  it('keeps the global draw scope separate from wall features and structure counts', () => {
    const state = context()

    const main: ReviewGroup = {
      review_group_id: 'main',
      kind: 'product_forming',
      target_refs: ['MAIN'],
      status: 'confirmed'
    }

    state.forming_scopes.push({
      forming_scope_id: 'MAIN',
      kind: 'main_forming',
      mode: 'main',
      mechanism_kind: 'none', undercut_geometry_refs: [], candidate_solutions: [], coordinate_system: 'model', direction_source: 'user_confirmed',
      draw_direction: [0, 0, 1],
      geometry_refs: state.features.flatMap(feature => feature.properties.geometry_refs ?? [])
    })
    state.review_groups.push(main)
    expect(reviewGeometry(state, main)).toHaveLength(3)
    const wall = state.review_groups.find(group => group.target_refs.includes('WALL'))!
    expect(reviewGeometry(state, wall)).toEqual([{ kind: 'face', index: 3 }])
    expect(confirmedFeatureIds(state.review_groups)).toEqual(new Set(['BOSS_2']))
    expect(state.features.some(feature => feature.feature_id === 'MAIN')).toBe(false)
  })

  it('confirms one complete forming plan independently of structural review', () => {
    const state = context()

    const group: ReviewGroup = {
      review_group_id: 'plan', kind: 'forming_scope', target_refs: ['SIDE'], status: 'pending_review'
    }

    expect(directionUnlocked(state, group)).toBe(false)
    state.forming_scopes[0].mechanism_kind = 'slider'
    expect(directionUnlocked(state, group)).toBe(false)
    state.forming_scopes[0].draw_direction = [1, 0, 0]
    expect(directionUnlocked(state, group)).toBe(true)
    expect(state.review_groups[0].status).toBe('pending_review')
    state.forming_scopes[0].geometry_refs = []
    expect(directionUnlocked(state, group)).toBe(false)
    state.forming_scopes[0].geometry_refs = [{ kind: 'face', index: 1 }]
    state.forming_scopes[0].status = 'stale'
    expect(directionUnlocked(state, group)).toBe(false)
  })

  it('does not infer a direction from missing, zero or invalid input', () => {
    for (const value of [
      ['', '', ''],
      ['0', '0', '0'],
      ['NaN', '0', '1'],
      ['Infinity', '0', '1']
    ]) {
      expect(parseReviewDirection(value)).toBeNull()
    }

    expect(parseReviewDirection(['1', '2', '-3'])).toEqual([1, 2, -3].map(value => value / Math.hypot(1, 2, -3)))
  })

  it('accepts only the current pending groups whose forming plans are complete', () => {
    const state = context()
    state.forming_scopes.push({ forming_scope_id: 'MAIN', kind: 'main_forming', mode: 'main',
      mechanism_kind: 'none', undercut_geometry_refs: [], candidate_solutions: [], coordinate_system: 'model',
      direction_source: 'user_confirmed', draw_direction: [0, 0, 1], geometry_refs: [{ kind: 'face', index: 3 }] })
    state.review_groups.push(
      { review_group_id: 'plan', kind: 'forming_scope', target_refs: ['SIDE'], status: 'pending_review' },
      { review_group_id: 'main', kind: 'product_forming', target_refs: ['MAIN'], status: 'pending_review' },
      { review_group_id: 'unknown', kind: 'future_kind', target_refs: [], status: 'pending_review' }
    )
    expect(confirmableReviewGroupIds(state)).toEqual(['b1', 'w', 'main'])

    for (const direction of [null, [], [0, 0, 0], [0, 1, NaN], [0, 0, Infinity]]) {
      state.forming_scopes[1].draw_direction = direction
      expect(confirmableReviewGroupIds(state)).toEqual(['b1', 'w'])
    }

    state.forming_scopes[1].draw_direction = [0, 0, 1]

    const ready = { ...state.forming_scopes[0], mechanism_kind: 'slider' as const,
      draw_direction: [1, 0, 0], status: 'candidate' as const }

    state.forming_scopes[0] = ready
    expect(confirmableReviewGroupIds(state)).toEqual(['b1', 'w', 'plan', 'main'])

    for (const incomplete of [
      { ...ready, mechanism_kind: 'undetermined' as const },
      { ...ready, draw_direction: null },
      { ...ready, geometry_refs: [] },
      { ...ready, status: 'stale' as const }
    ]) {
      state.forming_scopes[0] = incomplete
      expect(confirmableReviewGroupIds(state)).toEqual(['b1', 'w', 'main'])
    }

    state.forming_scopes[0] = ready
    state.review_groups.find(group => group.review_group_id === 'plan')!.status = 'confirmed'
    expect(confirmableReviewGroupIds(state)).toEqual(['b1', 'w', 'main'])
  })

  it('offers removal only for pending recognition objects and local ranges', () => {
    const state = context()
    expect(removableReviewGroup(state, state.review_groups[0])).toBe(true)
    expect(removableReviewGroup(state, state.review_groups[1])).toBe(false)

    const range: ReviewGroup = { review_group_id: 'plan', kind: 'forming_scope',
      target_refs: ['SIDE'], status: 'pending_review' }

    expect(removableReviewGroup(state, range)).toBe(true)
    state.forming_scopes[0].status = 'stale'
    expect(removableReviewGroup(state, range)).toBe(false)
    state.forming_scopes[0].status = 'candidate'
    state.forming_scopes[0].mode = 'main'
    expect(removableReviewGroup(state, range)).toBe(false)

    for (const kind of ['product_forming', 'future_kind']) {
      expect(removableReviewGroup(state, { ...range, kind })).toBe(false)
    }
  })

  it('reports only actual missing local-plan fields and does not require an undercut face', () => {
    const scope = { ...context().forming_scopes[0], geometry_refs: [], undercut_geometry_refs: [],
      mechanism_kind: 'undetermined' as const, draw_direction: null }
    expect(formingPlanMissing(scope)).toEqual(['成型面', '成型机构', '退出方向'])
    expect(formingDirectionLabel(scope)).toBe('方向待补充')
    const ready = { ...scope, geometry_refs: [{ kind: 'face' as const, index: 1 }],
      mechanism_kind: 'slider' as const, draw_direction: [0, -3, 0] }
    expect(formingPlanMissing(ready)).toEqual([])
    expect(formingDirectionLabel(ready)).toBe('−Y')
    expect(formingDirectionLabel({ ...ready, draw_direction: [1, 1, 0] })).toBe('[0.707, 0.707, 0]')
  })
})
