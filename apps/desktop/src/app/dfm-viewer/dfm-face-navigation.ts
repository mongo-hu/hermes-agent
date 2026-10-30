import type { ReviewContext, ReviewNavigation } from './dfm-review'
import type { GeometryReference, TopologyEdge, TopologyFace } from './dfm-viewer-geometry'

interface Candidate extends Omit<ReviewNavigation, 'sequence'> {
  refs: GeometryReference[]
  fallback?: boolean
}

/** Build once per snapshot. Picking a face is then a map lookup, including on large models. */
export function createFaceNavigationIndex(
  context: Pick<ReviewContext, 'features' | 'annotations' | 'forming_scopes'>,
  faces: TopologyFace[],
  edges: TopologyEdge[]
): Map<number, Omit<ReviewNavigation, 'sequence'>> {
  const faceById = new Map(faces.map(face => [face.geometry_ref.index, face.geometry_ref]))
  const edgeById = new Map(edges.map(edge => [edge.geometry_ref.index, edge]))

  const sameTopology = (a: GeometryReference, b: GeometryReference) =>
    a.input_sha256 === b.input_sha256 && a.topology_snapshot_id === b.topology_snapshot_id

  const resolve = (refs: GeometryReference[]) =>
    new Set(
      refs.flatMap(ref => {
        if (ref.kind === 'face') {
          const face = faceById.get(ref.index)

          return face && sameTopology(face, ref) ? [ref.index] : []
        }

        const edge = ref.kind === 'edge' ? edgeById.get(ref.index) : undefined

        return edge && sameTopology(edge.geometry_ref, ref)
          ? edge.adjacent_face_indices.filter(id => faceById.has(id))
          : []
      })
    )

  const candidates: Candidate[] = []

  for (const feature of context.features) {
    // Reference walls belong to the related structure, not to the Boss itself.
    const parts = feature.properties.parts as Record<string, GeometryReference[]> | undefined

    const related = resolve(
      Object.entries(parts ?? {})
        .filter(([name]) => name === 'adjacent_main_wall' || name === 'corresponding_outer_wall')
        .flatMap(([, refs]) => refs)
    )

    const owned = resolve(
      Object.entries(parts ?? {})
        .filter(([name]) => name !== 'adjacent_main_wall' && name !== 'corresponding_outer_wall')
        .flatMap(([, refs]) => refs)
    )

    candidates.push({
      category: 'structure',
      objectId: feature.feature_id,
      refs: (feature.properties.geometry_refs ?? []).filter(
        ref => ref.kind !== 'face' || !related.has(ref.index) || owned.has(ref.index)
      )
    })
  }

  for (const annotation of context.annotations.filter(item => item.status !== 'stale')) {
    candidates.push({
      category: 'annotation',
      objectId: annotation.annotation_id,
      refs: annotation.geometry_refs,
      fallback: annotation.geometry_refs.every(ref => ref.kind !== 'face')
    })
  }

  for (const scope of context.forming_scopes.filter(item => item.status !== 'stale')) {
    candidates.push({
      category: 'forming',
      objectId: scope.forming_scope_id,
      refs: scope.geometry_refs,
      fallback: scope.mode === 'main'
    })
  }

  const priority = { structure: 0, annotation: 1, forming: 2 }

  const ranked = candidates
    .map(candidate => ({ ...candidate, faces: resolve(candidate.refs) }))
    .sort(
      (a, b) =>
        priority[a.category] - priority[b.category] ||
        Number(Boolean(a.fallback)) - Number(Boolean(b.fallback)) ||
        a.faces.size - b.faces.size ||
        a.objectId.localeCompare(b.objectId)
    )

  const index = new Map<number, Omit<ReviewNavigation, 'sequence'>>()

  for (const candidate of ranked) {
    for (const face of candidate.faces) {
      if (!index.has(face)) {index.set(face, { category: candidate.category, objectId: candidate.objectId })}
    }
  }

  return index
}
