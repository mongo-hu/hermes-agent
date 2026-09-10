export interface GeometryReference {
  index: number
  kind: 'edge' | 'face' | 'solid' | 'vertex'
}

export interface RenderPrimitive {
  primitive_id: string
  triangles: number[][]
  vertices: number[][]
}

export interface TopologyFace {
  geometry_ref: GeometryReference
  triangle_refs: { primitive_id: string; triangle_id: number }[]
}

export interface MergedFaceGroup {
  count: number
  faceIndex: number
  start: number
}

export interface MergedFaceMeshData {
  groups: MergedFaceGroup[]
  indices: Uint32Array
  positions: Float32Array
  triangleFaceIndices: Uint32Array
}

const MAX_UINT32 = 0xffffffff
const MAX_FLOAT32 = 3.4028234663852886e38

/** Resolve only topology identities that the shared scene/map contract can render. */
export function resolveGeometryRefFaceIndices(refs: GeometryReference[] | undefined): Set<number> {
  return new Set(refs?.filter(ref => ref.kind === 'face').map(ref => ref.index) ?? [])
}

function validatePrimitive(primitive: RenderPrimitive, seen: Set<string>): void {
  if (!primitive.primitive_id || seen.has(primitive.primitive_id)) {
    throw new Error(`无效或重复的渲染图元：${primitive.primitive_id || 'unknown'}`)
  }

  seen.add(primitive.primitive_id)

  if (!Array.isArray(primitive.vertices) || primitive.vertices.length < 3) {
    throw new Error(`渲染图元 ${primitive.primitive_id} 的顶点数据无效`)
  }

  for (const vertex of primitive.vertices) {
    if (
      !Array.isArray(vertex) ||
      vertex.length !== 3 ||
      vertex.some(value => !Number.isFinite(value) || Math.abs(value) > MAX_FLOAT32)
    ) {
      throw new Error(`渲染图元 ${primitive.primitive_id} 包含无法渲染的坐标`)
    }
  }

  if (!Array.isArray(primitive.triangles) || primitive.triangles.length === 0) {
    throw new Error(`渲染图元 ${primitive.primitive_id} 的三角形索引无效`)
  }

  for (const triangle of primitive.triangles) {
    if (
      !Array.isArray(triangle) ||
      triangle.length !== 3 ||
      triangle.some(index => !Number.isInteger(index) || index < 0 || index >= primitive.vertices.length)
    ) {
      throw new Error(`渲染图元 ${primitive.primitive_id} 包含越界的三角形索引`)
    }
  }
}

/** Merge RenderScene Schema 2 primitives using TopologyMap Schema 2 face identity. */
export function mergeRenderScene(primitives: RenderPrimitive[], topologyFaces: TopologyFace[]): MergedFaceMeshData {
  if (primitives.length === 0) {
    throw new Error('共享渲染场景中没有可显示的图元')
  }

  const seen = new Set<string>()
  const primitiveById = new Map<string, RenderPrimitive>()
  const faceIndicesByPrimitive = new Map<string, number[]>()
  let positionCount = 0
  let indexCount = 0

  for (const primitive of primitives) {
    validatePrimitive(primitive, seen)
    primitiveById.set(primitive.primitive_id, primitive)
    faceIndicesByPrimitive.set(primitive.primitive_id, new Array(primitive.triangles.length).fill(0))
    positionCount += primitive.vertices.length * 3
    indexCount += primitive.triangles.length * 3
  }

  if (!Number.isSafeInteger(positionCount) || !Number.isSafeInteger(indexCount)) {
    throw new Error('共享渲染场景过大，无法安全渲染')
  }

  for (const face of topologyFaces) {
    const faceIndex = face.geometry_ref?.index

    if (
      face.geometry_ref?.kind !== 'face' ||
      !Number.isInteger(faceIndex) ||
      faceIndex <= 0 ||
      faceIndex > MAX_UINT32
    ) {
      throw new Error('拓扑映射包含无效的面标识')
    }

    for (const ref of face.triangle_refs ?? []) {
      const primitive = primitiveById.get(ref.primitive_id)
      const mappedFaces = faceIndicesByPrimitive.get(ref.primitive_id)

      if (!primitive || !mappedFaces) {
        throw new Error(`面 #${faceIndex} 引用了不存在的渲染图元 ${ref.primitive_id || 'unknown'}`)
      }
      if (!Number.isInteger(ref.triangle_id) || ref.triangle_id < 0 || ref.triangle_id >= primitive.triangles.length) {
        throw new Error(`面 #${faceIndex} 包含越界的三角形引用`)
      }
      if (mappedFaces[ref.triangle_id] !== 0) {
        throw new Error(`渲染图元 ${ref.primitive_id} 的三角形 #${ref.triangle_id} 被重复映射`)
      }

      mappedFaces[ref.triangle_id] = faceIndex
    }
  }

  const positions = new Float32Array(positionCount)
  const indices = new Uint32Array(indexCount)
  const triangleFaceIndices = new Uint32Array(indexCount / 3)
  const groups: MergedFaceGroup[] = []
  let positionOffset = 0
  let vertexOffset = 0
  const indicesByFace = new Map<number, number[]>()

  for (const primitive of primitives) {
    const flattenedVertices = primitive.vertices.flat()
    const mappedFaces = faceIndicesByPrimitive.get(primitive.primitive_id)!
    positions.set(flattenedVertices, positionOffset)

    primitive.triangles.forEach((triangle, triangleIndex) => {
      const faceIndex = mappedFaces[triangleIndex]

      if (faceIndex === 0) {
        throw new Error(`渲染图元 ${primitive.primitive_id} 的三角形 #${triangleIndex} 没有对应的拓扑面`)
      }

      const faceIndices = indicesByFace.get(faceIndex) ?? []
      faceIndices.push(...triangle.map(index => index + vertexOffset))
      indicesByFace.set(faceIndex, faceIndices)
    })

    positionOffset += flattenedVertices.length
    vertexOffset += primitive.vertices.length
  }

  let indexOffset = 0
  let triangleOffset = 0

  for (const [faceIndex, faceIndices] of indicesByFace) {
    indices.set(faceIndices, indexOffset)
    const triangleCount = faceIndices.length / 3
    triangleFaceIndices.fill(faceIndex, triangleOffset, triangleOffset + triangleCount)
    groups.push({ count: faceIndices.length, faceIndex, start: indexOffset })

    indexOffset += faceIndices.length
    triangleOffset += triangleCount
  }

  return { groups, indices, positions, triangleFaceIndices }
}
