import { reviewBuckets, type ReviewCategory, type ReviewContext } from './dfm-review'

/** Display selection only; independent of approval checkboxes and edit drafts. */
export interface DfmHighlightSelection {
  bucketId: string
  objectId?: string
  category?: ReviewCategory
}

export function sameHighlightSelection(a: DfmHighlightSelection | null, b: DfmHighlightSelection): boolean {
  if (!a) {return false}

  if (a.objectId || b.objectId) {
    return Boolean(a.objectId && a.objectId === b.objectId && a.category === b.category)
  }

  return a.bucketId === b.bucketId
}

export function objectHighlightSelection(
  category: ReviewCategory,
  objectId: string,
  context: ReviewContext | null,
  featureKind?: string
): DfmHighlightSelection {
  const bucket = context && reviewBuckets(context, true).find(item =>
    item.category === category && item.groups.some(group => group.target_refs.includes(objectId)))

  return { bucketId: bucket?.id ?? (category === 'structure' ? `confirmed:${featureKind}` : `${category}:${objectId}`),
    objectId, category }
}
