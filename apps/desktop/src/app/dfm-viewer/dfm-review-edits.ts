import type { ReviewContext } from './dfm-review'
import type { GeometryReference } from './dfm-viewer-geometry'

export type ReviewEditKind = 'feature' | 'scope' | 'annotation'

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(canonical)
  }

  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([key, item]) => [key, canonical(item)])
    )
  }

  return value
}

function refsKey(refs: GeometryReference[] | undefined) {
  return (refs ?? []).map(ref => JSON.stringify(canonical(ref))).sort()
}

function modelKey(context: ReviewContext) {
  const refs = [
    ...context.features.flatMap(item => item.properties.geometry_refs ?? []),
    ...context.forming_scopes.filter(item => item.status !== 'stale').flatMap(item => item.geometry_refs)
  ]

  return JSON.stringify(
    [...new Set(refs.map(ref => `${ref.input_sha256 ?? ''}:${ref.topology_snapshot_id ?? ''}`))].sort()
  )
}

function targetKey(context: ReviewContext, kind: ReviewEditKind, id: string) {
  let value: unknown = null

  if (kind === 'scope') {
    const item = context.forming_scopes.find(row => row.forming_scope_id === id && row.status !== 'stale')

    if (item) {
      value = { kind: item.kind, mode: item.mode, direction: item.draw_direction, refs: refsKey(item.geometry_refs) }
    }
  } else if (kind === 'annotation') {
    const item = context.annotations.find(row => row.annotation_id === id && row.status !== 'stale')

    if (item) {
      value = { kind: item.kind, value: item.value, refs: refsKey(item.geometry_refs) }
    }
  } else {
    const item = context.features.find(row => row.feature_id === id)

    if (item) {
      value = {
        kind: item.kind,
        subtype: item.properties.subtype,
        refs: refsKey(item.properties.geometry_refs),
        parts: Object.fromEntries(
          Object.entries((item.properties.parts ?? {}) as Record<string, GeometryReference[]>).map(([name, refs]) => [
            name,
            refsKey(refs)
          ])
        ),
        unassigned: item.properties.diagnostics?.unassigned_geometry_refs
      }
    }
  }

  return JSON.stringify(canonical(value))
}

export interface ReviewEditBase {
  projectId: string
  kind: ReviewEditKind
  id: string
  model: string
  content: string
}

export function captureReviewEdit(context: ReviewContext, kind: ReviewEditKind, id: string): ReviewEditBase {
  return { projectId: context.project_id, kind, id, model: modelKey(context), content: targetKey(context, kind, id) }
}

/** Rebase only when the user's target and model are unchanged, regardless of other groups. */
export function reviewEditUnchanged(base: ReviewEditBase, context: ReviewContext) {
  return (
    base.projectId === context.project_id &&
    base.model === modelKey(context) &&
    base.content === targetKey(context, base.kind, base.id)
  )
}

export function isReviewConflict(cause: unknown) {
  return /geometry_edit_conflict|snapshot_conflict|manifest_conflict|review_group_stale/.test(
    cause instanceof Error ? cause.message : String(cause)
  )
}
