import { useStore } from '@nanostores/react'
import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Box3,
  BufferGeometry,
  Color,
  DirectionalLight,
  DoubleSide,
  EdgesGeometry,
  Float32BufferAttribute,
  Group,
  HemisphereLight,
  LineBasicMaterial,
  LineSegments,
  MathUtils,
  Mesh,
  MeshStandardMaterial,
  PerspectiveCamera,
  Quaternion,
  Raycaster,
  Scene,
  Sphere,
  SRGBColorSpace,
  Uint32BufferAttribute,
  Vector2,
  Vector3,
  WebGLRenderer
} from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'

import { openDfmViewerWindow } from '@/lib/dfm-viewer-window'
import { $clarifyRequest, type ClarifyRequest, clearClarifyRequest, type DfmClarificationItem } from '@/store/clarify'
import type { DfmViewerTarget } from '@/store/dfm-viewer'
import { $gateway } from '@/store/gateway'

import { DfmAnnotationEditor } from './dfm-annotation-editor'
import { createDirectionAxis, type DirectionAxisState } from './dfm-direction-axis'
import { DfmDirectionEditor, type DirectionDraft } from './dfm-direction-editor'
import { createFaceNavigationIndex } from './dfm-face-navigation'
import { DfmFeatureEditor } from './dfm-feature-editor'
import { DfmFeatureGroups } from './dfm-feature-groups'
import type { ReviewNavigation } from './dfm-review'
import { type AnnotationKind, confirmedFeatureIds, type ReviewAnnotation, type ReviewContext, type ReviewGroup, type ReviewScope } from './dfm-review'
import { parseDirectionVector, reviewEditTimeout } from './dfm-review'
import { captureReviewEdit, isReviewConflict, type ReviewEditBase, reviewEditUnchanged } from './dfm-review-edits'
import { DfmReviewPanel } from './dfm-review-panel'
import { createFocusIndex, selectionCameraPose } from './dfm-viewer-focus'
import { resolveReviewDisplayRefs, type TopologyEdge } from './dfm-viewer-geometry'
import {
  type GeometryReference,
  mergeRenderScene,
  type RenderPrimitive,
  type RenderTriangleReference,
  resolveGeometryRefFaceIndices,
  resolveTriangleRefPositions,
  type TopologyFace
} from './dfm-viewer-geometry'
import {
  classifyViewerIssueType,
  formatViewerValue,
  summarizeViewerIssueTypes,
  type ViewerIssueTypeCount
} from './dfm-viewer-issues'

interface ViewerIssue {
  actual: unknown
  check_id?: string
  evaluation_id: string
  expected: unknown
  geometry_refs: GeometryReference[]
  triangle_refs?: RenderTriangleReference[]
  metric_id: string
  operator: string
  issue_type_id?: string
  issue_type_label?: string
  title: string
}

interface ViewerFeature {
  confidence: unknown
  diagnostics: Record<string, unknown>
  feature_id: string
  geometry_refs: GeometryReference[]
  kind: string
  method: string
  parameters: Record<string, unknown>
  subtype: string
}

interface ViewerManifest {
  contract_version: 'hermes.dfm.viewer/v2'
  project_id?: string
  snapshot_id?: string
  feature_count?: number
  features?: ViewerFeature[]
  issue_count: number
  issue_type_counts?: ViewerIssueTypeCount[]
  issues: ViewerIssue[]
  scene_path: string
  scope_id: string
  project_revision?: number
  review_status?: 'confirmed' | 'pending'
  review_groups?: ReviewGroup[]
  status?: 'completed' | 'partial' | 'discovery' | 'preview'
  topology_path: string
  verification_level: string
}

interface RenderSceneDocument {
  primitives: RenderPrimitive[]
  render_mesh_snapshot: { render_mesh_snapshot_id?: string; triangle_count: number }
  schema_version: 2
}

interface TopologyDocument {
  faces: TopologyFace[]
  edges?: TopologyEdge[]
  schema_version: 2
}

interface SceneResources {
  directionAxis: ReturnType<typeof createDirectionAxis>
  focus: (faces: ReadonlySet<number>) => void
  faceGroups: Map<number, number>
  fit: () => void
  geometry: BufferGeometry
  issueOverlay: Mesh | null
  modelGroup: Group
  featureMaterial: MeshStandardMaterial
  normalMaterial: MeshStandardMaterial
  pickedMaterial: MeshStandardMaterial
  problemMaterial: MeshStandardMaterial
}

function dirname(filePath: string): string {
  const index = Math.max(filePath.lastIndexOf('/'), filePath.lastIndexOf('\\'))

  return index < 0 ? '' : filePath.slice(0, index)
}

function joinPath(base: string, child: string): string {
  const separator = base.includes('\\') ? '\\' : '/'

  return `${base.replace(/[\\/]$/, '')}${separator}${child}`
}

async function readJson<T>(filePath: string): Promise<T> {
  const readJsonFile = window.hermesDesktop.readJsonFile

  if (readJsonFile) {
    return (await readJsonFile(filePath)) as T
  }

  const result = await window.hermesDesktop.readFileText(filePath)

  return JSON.parse(result.text) as T
}

function ModelCanvas({
  activeFeatures,
  activeIssue,
  document,
  fitRequest,
  focusRequest,
  onFacePick,
  pickedFaceIndex,
  selectedFaceIndices,
  topologyFaces,
  directionAxis,
  onDirectionChange,
  onAxisSelected
}: {
  activeFeatures: ViewerFeature[]
  activeIssue: ViewerIssue | null
  document: RenderSceneDocument
  fitRequest: number
  focusRequest: { sequence: number; faceIndices: number[] }
  onFacePick: (faceIndex: number) => void
  pickedFaceIndex: number | null
  selectedFaceIndices?: Set<number> | null
  directionAxis: DirectionAxisState | null
  onDirectionChange: (direction: [number, number, number], origin: [number, number, number]) => void
  onAxisSelected: () => void
  topologyFaces: TopologyFace[]
}) {
  const hostRef = useRef<HTMLDivElement>(null)
  const resourcesRef = useRef<SceneResources | null>(null)
  const onFacePickRef = useRef(onFacePick)
  const directionRef = useRef(directionAxis)
  const directionChangeRef = useRef(onDirectionChange)
  const axisSelectedRef = useRef(onAxisSelected)
  directionRef.current = directionAxis
  directionChangeRef.current = onDirectionChange
  axisSelectedRef.current = onAxisSelected

  const issuePositions = useMemo(
    () =>
      resolveTriangleRefPositions(
        document.primitives,
        activeIssue?.triangle_refs,
        document.render_mesh_snapshot.render_mesh_snapshot_id
      ),
    [activeIssue, document]
  )

  const problemFaces = useMemo(
    () => (issuePositions.length ? new Set<number>() : resolveGeometryRefFaceIndices(activeIssue?.geometry_refs)),
    [activeIssue, issuePositions]
  )

  const originalFeatureFaces = useMemo(() => {
    const faceIndices = new Set<number>()

    for (const feature of activeFeatures) {
      for (const faceIndex of resolveGeometryRefFaceIndices(feature.geometry_refs)) {
        faceIndices.add(faceIndex)
      }
    }

    return faceIndices
  }, [activeFeatures])

  const featureFaces = selectedFaceIndices ?? originalFeatureFaces

  useEffect(() => {
    onFacePickRef.current = onFacePick
  }, [onFacePick])

  useEffect(() => {
    const host = hostRef.current

    if (!host) {
      return
    }

    const scene = new Scene()
    scene.background = new Color(0x091019)
    const camera = new PerspectiveCamera(38, 1, 0.01, 100000)
    camera.up.set(0, 0, 1)
    const renderer = new WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'high-performance' })
    renderer.outputColorSpace = SRGBColorSpace
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.domElement.style.cursor = 'grab'
    renderer.domElement.style.display = 'block'
    renderer.domElement.style.height = '100%'
    renderer.domElement.style.width = '100%'
    host.appendChild(renderer.domElement)

    const modelGroup = new Group()
    const merged = mergeRenderScene(document.primitives, topologyFaces)
    const focusIndex = createFocusIndex(merged)
    const geometry = new BufferGeometry()
    geometry.setAttribute('position', new Float32BufferAttribute(merged.positions, 3))
    geometry.setIndex(new Uint32BufferAttribute(merged.indices, 1))
    geometry.clearGroups()

    for (const faceGroup of merged.groups) {
      geometry.addGroup(faceGroup.start, faceGroup.count, 0)
    }

    geometry.computeVertexNormals()
    geometry.computeBoundingBox()
    geometry.computeBoundingSphere()

    const normalMaterial = new MeshStandardMaterial({
      color: 0x8da8b8,
      metalness: 0.16,
      roughness: 0.64,
      side: DoubleSide
    })

    const problemMaterial = new MeshStandardMaterial({
      color: 0xff4057,
      emissive: 0x7a0718,
      emissiveIntensity: 0.9,
      metalness: 0.02,
      roughness: 0.34,
      side: DoubleSide,
      polygonOffset: true,
      polygonOffsetFactor: -2,
      polygonOffsetUnits: -2
    })

    const pickedMaterial = new MeshStandardMaterial({
      color: 0x21d3c7,
      emissive: 0x087870,
      emissiveIntensity: 0.65,
      metalness: 0.1,
      roughness: 0.48,
      side: DoubleSide
    })

    const featureMaterial = new MeshStandardMaterial({
      color: 0xf6b84a,
      emissive: 0x7a4305,
      emissiveIntensity: 0.72,
      metalness: 0.06,
      roughness: 0.4,
      side: DoubleSide
    })

    const edgeMaterial = new LineBasicMaterial({ color: 0x17232e, opacity: 0.5, transparent: true })
    const modelMesh = new Mesh(geometry, [normalMaterial, problemMaterial, featureMaterial, pickedMaterial])
    modelGroup.add(modelMesh)

    const outline = new LineSegments(new EdgesGeometry(geometry, 24), edgeMaterial)
    outline.renderOrder = 2
    modelGroup.add(outline)
    scene.add(modelGroup)

    const bounds = new Box3().setFromObject(modelGroup)
    const sphere = bounds.getBoundingSphere(new Sphere())
    const radius = Math.max(sphere.radius, 0.5)

    scene.add(new HemisphereLight(0xcce8ff, 0x16222c, 2.2))
    const keyLight = new DirectionalLight(0xffffff, 2.5)
    keyLight.position.copy(sphere.center).add(new Vector3(1, -1, 2).multiplyScalar(radius))
    keyLight.target.position.copy(sphere.center)
    scene.add(keyLight)
    scene.add(keyLight.target)
    const rimLight = new DirectionalLight(0x50d4d0, 1.2)
    rimLight.position.copy(sphere.center).add(new Vector3(-2, 1, 0.5).multiplyScalar(radius))
    rimLight.target.position.copy(sphere.center)
    scene.add(rimLight)
    scene.add(rimLight.target)

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.08

    const axisController = createDirectionAxis(scene, camera, renderer.domElement, controls, focusIndex,
      (direction, origin) => directionChangeRef.current(direction, origin))

    axisController.update(directionRef.current)
    let focusAnimation: { start: number; fromOffset: Vector3; rotation: Quaternion; fromDistance: number; toDistance: number; fromTarget: Vector3; toTarget: Vector3 } | null = null

    const cancelFocus = () => { focusAnimation = null }
    controls.addEventListener('start', cancelFocus)

    const resize = () => {
      const width = Math.max(host.clientWidth, 1)
      const height = Math.max(host.clientHeight, 1)
      camera.aspect = width / height
      camera.updateProjectionMatrix()
      renderer.setSize(width, height, false)
    }

    const fit = () => {
      cancelFocus()
      resize()
      const verticalFov = MathUtils.degToRad(camera.fov)
      const horizontalFov = 2 * Math.atan(Math.tan(verticalFov / 2) * camera.aspect)
      const limitingFov = Math.max(Math.min(verticalFov, horizontalFov), MathUtils.degToRad(8))
      const distance = (radius / Math.sin(limitingFov / 2)) * 1.02
      const direction = new Vector3(1.25, -1.55, 1.05).normalize()

      camera.position.copy(sphere.center).add(direction.multiplyScalar(distance))
      camera.near = Math.max(radius / 1000, 0.01)
      camera.far = Math.max(radius * 100, distance + radius * 4)
      camera.updateProjectionMatrix()
      controls.target.copy(sphere.center)
      controls.minDistance = radius * 0.04
      controls.maxDistance = radius * 20
      controls.update()
    }

    const observer = new ResizeObserver(resize)
    observer.observe(host)
    fit()

    const faceGroups = new Map(merged.groups.map((faceGroup, index) => [faceGroup.faceIndex, index]))
    resourcesRef.current = {
      directionAxis: axisController,
      focus: faces => {
        const pose = selectionCameraPose(focusIndex, faces, camera.position, camera.fov, camera.aspect)

        if (!pose) {return}
        const fromOffset = camera.position.clone().sub(controls.target)
        const toOffset = pose.position.clone().sub(pose.target)
        focusAnimation = { start: performance.now(), fromOffset: fromOffset.clone().normalize(),
          rotation: new Quaternion().setFromUnitVectors(fromOffset.clone().normalize(), toOffset.clone().normalize()),
          fromDistance: fromOffset.length(), toDistance: toOffset.length(),
          fromTarget: controls.target.clone(), toTarget: pose.target }
        controls.minDistance = Math.max(pose.modelRadius * 1e-5, 1e-6)
        camera.near = Math.max(Math.min(pose.modelRadius / 10000, toOffset.length() / 100), 1e-6)
        camera.far = Math.max(pose.modelRadius * 100, pose.position.distanceTo(pose.target) + pose.modelRadius * 4)
        camera.updateProjectionMatrix()
      },
      faceGroups,
      featureMaterial,
      fit,
      geometry,
      issueOverlay: null,
      modelGroup,
      normalMaterial,
      pickedMaterial,
      problemMaterial
    }

    const raycaster = new Raycaster()
    const pointer = new Vector2()
    let pointerDown: { x: number; y: number } | null = null

    const pickFace = (event: PointerEvent): number | null => {
      const rect = renderer.domElement.getBoundingClientRect()

      if (rect.width <= 0 || rect.height <= 0) {
        return null
      }

      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1
      pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1
      raycaster.setFromCamera(pointer, camera)
      const intersection = raycaster.intersectObject(modelMesh, false)[0]
      const triangleIndex = intersection?.faceIndex

      return triangleIndex == null ? null : (merged.triangleFaceIndices[triangleIndex] ?? null)
    }

    const handlePointerDown = (event: PointerEvent) => {
      if (event.button !== 0 || directionRef.current?.editing) {
        return
      }

      pointerDown = { x: event.clientX, y: event.clientY }
      renderer.domElement.style.cursor = 'grabbing'
    }

    const handlePointerUp = (event: PointerEvent) => {
      renderer.domElement.style.cursor = 'grab'

      if (!pointerDown || Math.hypot(event.clientX - pointerDown.x, event.clientY - pointerDown.y) > 4) {
        pointerDown = null

        return
      }

      pointerDown = null

      if (directionRef.current?.editing) {return}

      if (axisController.hit(event)) { axisSelectedRef.current();

 return }

      const faceIndex = pickFace(event)

      if (faceIndex != null && faceIndex > 0) {
        onFacePickRef.current(faceIndex)
      }
    }

    const handlePointerCancel = () => {
      pointerDown = null
      renderer.domElement.style.cursor = 'grab'
    }

    renderer.domElement.addEventListener('pointerdown', handlePointerDown)
    renderer.domElement.addEventListener('pointerup', handlePointerUp)
    renderer.domElement.addEventListener('pointercancel', handlePointerCancel)
    renderer.domElement.addEventListener('pointerleave', handlePointerCancel)

    let frame = 0

    const render = () => {
      if (focusAnimation) {
        const t = Math.min((performance.now() - focusAnimation.start) / 380, 1)
        const blend = t * t * (3 - 2 * t)
        controls.target.lerpVectors(focusAnimation.fromTarget, focusAnimation.toTarget, blend)
        const rotation = new Quaternion().slerp(focusAnimation.rotation, blend)
        camera.position.copy(focusAnimation.fromOffset).applyQuaternion(rotation)
          .multiplyScalar(MathUtils.lerp(focusAnimation.fromDistance, focusAnimation.toDistance, blend)).add(controls.target)

        if (t === 1) {focusAnimation = null}
      }

      controls.update()
      renderer.render(scene, camera)
      frame = requestAnimationFrame(render)
    }

    render()

    return () => {
      cancelAnimationFrame(frame)
      observer.disconnect()
      renderer.domElement.removeEventListener('pointerdown', handlePointerDown)
      renderer.domElement.removeEventListener('pointerup', handlePointerUp)
      renderer.domElement.removeEventListener('pointercancel', handlePointerCancel)
      renderer.domElement.removeEventListener('pointerleave', handlePointerCancel)
      axisController.dispose()
      controls.dispose()
      controls.removeEventListener('start', cancelFocus)
      modelGroup.traverse(child => {
        if (child instanceof Mesh || child instanceof LineSegments) {
          child.geometry.dispose()
        }
      })
      normalMaterial.dispose()
      problemMaterial.dispose()
      featureMaterial.dispose()
      pickedMaterial.dispose()
      edgeMaterial.dispose()
      renderer.dispose()
      renderer.domElement.remove()
      resourcesRef.current = null
    }
  }, [document, topologyFaces])

  useEffect(() => {
    resourcesRef.current?.directionAxis.update(directionAxis)
  }, [directionAxis, document])

  useEffect(() => {
    const resources = resourcesRef.current

    if (!resources) {
      return
    }

    for (const [faceIndex, groupIndex] of resources.faceGroups) {
      const group = resources.geometry.groups[groupIndex]
      group.materialIndex = problemFaces.has(faceIndex)
        ? 1
        : featureFaces.has(faceIndex)
          ? 2
          : pickedFaceIndex === faceIndex && selectedFaceIndices == null
            ? 3
            : 0
    }
  }, [featureFaces, originalFeatureFaces, pickedFaceIndex, problemFaces, selectedFaceIndices])

  useEffect(() => {
    const resources = resourcesRef.current

    if (!resources) {
      return
    }

    if (resources.issueOverlay) {
      resources.modelGroup.remove(resources.issueOverlay)
      resources.issueOverlay.geometry.dispose()
      resources.issueOverlay = null
    }

    if (!issuePositions.length) {
      return
    }

    const geometry = new BufferGeometry()
    geometry.setAttribute('position', new Float32BufferAttribute(issuePositions, 3))
    geometry.computeVertexNormals()

    const overlay = new Mesh(geometry, resources.problemMaterial)
    overlay.renderOrder = 3
    resources.modelGroup.add(overlay)
    resources.issueOverlay = overlay
  }, [issuePositions])

  useEffect(() => {
    if (focusRequest.sequence > 0) {resourcesRef.current?.focus(new Set(focusRequest.faceIndices))}
  }, [focusRequest, document])

  useEffect(() => {
    if (fitRequest > 0) {
      resourcesRef.current?.fit()
    }
  }, [fitRequest])

  return <div aria-label="DFM 三维画布" className="h-full min-h-0 w-full" data-direction-origin={directionAxis?.origin ? JSON.stringify(directionAxis.origin) : undefined} data-direction-scope={directionAxis?.scopeId} data-direction-vector={directionAxis?.direction ? JSON.stringify(directionAxis.direction) : undefined} ref={hostRef} />
}

const FEATURE_LABELS: Record<string, string> = {
  main_wall: '主体壁',
  hollow_boss: '带孔 BOSS 柱',
  screw_boss: '带孔 BOSS 柱', // Display historical snapshots without changing their IDs.
  solid_boss: '实心 BOSS 柱',
  blend: '圆角链',
  canonical_surface: '规范曲面',
  cavity: '几何凹腔',
  chamfer: '倒角',
  convex_hull: '凸包面集',
  drilled_hole: '钻孔',
  isolated: '孤立特征',
  rib: '加强筋',
  shaft: '轴类特征',
  surface_probe: '曲面探针'
}

const FEATURE_SUBTYPE_LABELS: Record<string, string> = {
  hollow_boss: '空心柱',
  solid_boss: '实心柱',
  arbitrary_cavity: '任意型腔',
  blind_hole: '盲孔',
  cliff_blend_chain: '悬崖圆角链',
  coaxial_shaft: '同轴轴段',
  conical: '圆锥倒角',
  conical_hole: '锥孔',
  countersunk_hole: '沉头孔',
  edge_blend_chain: '边圆角链',
  face_probe_set: '面探针集',
  hull_face_set: '凸包面集',
  inner_contour_feature: '内轮廓孤立特征',
  planar_band: '平面倒角',
  straight_rib: '直加强筋',
  stepped_countersunk_hole: '阶梯沉头孔',
  stepped_hole: '阶梯孔',
  through_hole: '通孔',
  uncertain_blend_chain: '待确认圆角链',
  vertex_blend_chain: '顶点圆角链'
}

const PARAMETER_LABELS: Record<string, string> = {
  depth_mm: '深度',
  diameter_mm: '直径',
  face_count: '面数',
  height_mm: '高度',
  length_mm: '长度',
  radius_mm: '半径',
  semi_angle_deg: '半角',
  thickness_mm: '厚度',
  width_mm: '宽度'
}

function formatFeatureParameters(parameters: Record<string, unknown>): string {
  const preferred = Object.entries(parameters)
    .filter(([key, value]) => key in PARAMETER_LABELS && (typeof value === 'number' || typeof value === 'string'))
    .slice(0, 3)

  return (
    preferred
      .map(
        ([key, value]) =>
          PARAMETER_LABELS[key] +
          ' ' +
          formatViewerValue(value) +
          (key.endsWith('_mm') ? ' mm' : key.endsWith('_deg') ? '°' : '')
      )
      .join(' · ') || '无尺寸参数'
  )
}

export function DfmClarificationPanel({
  features,
  onFeaturesHighlight,
  priorityFeatureId,
  request
}: {
  features: ViewerFeature[]
  onFeaturesHighlight: (featureRefs: string[]) => void
  priorityFeatureId: string | null
  request: ClarifyRequest
}) {
  const gateway = useStore($gateway)
  const context = request.context!
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [inheritedFeatureIds, setInheritedFeatureIds] = useState<Set<string>>(new Set())
  const [openFeatureKinds, setOpenFeatureKinds] = useState<Set<string>>(new Set())
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const questionsRef = useRef<HTMLDivElement>(null)

  const featureById = useMemo(
    () => new Map(features.map((feature, index) => [feature.feature_id, { feature, index }] as const)),
    [features]
  )

  const wholeItems = useMemo(
    () =>
      context.items.filter(
        item => !item.featureId && item.impactScope !== '特征实例级' && !item.impactScope.startsWith('非前置')
      ),
    [context.items]
  )

  const inferredItems = useMemo(
    () => context.items.filter(item => item.impactScope.startsWith('非前置')),
    [context.items]
  )

  const featureTypeGroups = useMemo(() => {
    const byKind = new Map<
      string,
      { firstIndex: number; instances: Map<string, { firstIndex: number; items: DfmClarificationItem[] }> }
    >()

    for (const item of context.items) {
      if (item.impactScope !== '特征实例级' || !item.featureId) {
        continue
      }

      const featureEntry = featureById.get(item.featureId)
      const kind = item.featureKind || featureEntry?.feature.kind || 'unknown'
      const featureIndex = featureEntry?.index ?? Number.MAX_SAFE_INTEGER
      const typeGroup = byKind.get(kind) ?? { firstIndex: featureIndex, instances: new Map() }

      const instance = typeGroup.instances.get(item.featureId) ?? {
        firstIndex: featureIndex,
        items: []
      }

      instance.items.push(item)
      typeGroup.instances.set(item.featureId, instance)
      typeGroup.firstIndex = Math.min(typeGroup.firstIndex, featureIndex)
      byKind.set(kind, typeGroup)
    }

    const priorityKind = priorityFeatureId ? featureById.get(priorityFeatureId)?.feature.kind : undefined

    return [...byKind.entries()]
      .map(([kind, group]) => ({
        firstIndex: group.firstIndex,
        kind,
        instances: [...group.instances.entries()]
          .map(([featureId, instance]) => ({ featureId, ...instance }))
          .sort((left, right) => left.firstIndex - right.firstIndex)
      }))
      .sort((left, right) => {
        if (left.kind === priorityKind && right.kind !== priorityKind) {
          return -1
        }

        if (right.kind === priorityKind && left.kind !== priorityKind) {
          return 1
        }

        return left.firstIndex - right.firstIndex
      })
  }, [context.items, featureById, priorityFeatureId])

  useEffect(() => {
    setAnswers({})
    setInheritedFeatureIds(new Set())
    setError('')
    setSubmitting(false)
  }, [request.requestId])

  useEffect(() => {
    setOpenFeatureKinds(new Set(featureTypeGroups.map(typeGroup => typeGroup.kind)))
  }, [featureTypeGroups, priorityFeatureId, request.requestId])

  useEffect(() => {
    if (priorityFeatureId) {
      questionsRef.current?.scrollTo({ behavior: 'smooth', top: 0 })
    }
  }, [priorityFeatureId])

  const answeredCount = context.items.filter(item => Boolean(answers[item.clarificationId]?.trim())).length
  const allAnswered = answeredCount === context.items.length

  const pendingDefaults = context.items.filter(
    item => item.hasDefault && item.defaultValue !== null && !answers[item.clarificationId]?.trim()
  ).length

  const fillAllDefaults = useCallback(() => {
    setAnswers(current => {
      const next = { ...current }

      for (const item of context.items) {
        if (item.hasDefault && item.defaultValue !== null && !next[item.clarificationId]?.trim()) {
          next[item.clarificationId] = item.defaultValue
        }
      }

      return next
    })
  }, [context.items])

  const inheritFeatureType = useCallback(
    (instances: Array<{ featureId: string; items: DfmClarificationItem[] }>) => {
      const source = instances[0]

      if (!source) {
        return
      }

      const sourceValues = new Map(
        source.items.flatMap(item => {
          const value = answers[item.clarificationId]?.trim()

          return value ? [[`${item.runtimeKey}@${item.checkId ?? ''}`, value] as const] : []
        })
      )

      const inherited = new Set(
        instances
          .slice(1)
          .flatMap(instance =>
            instance.items.some(item => sourceValues.has(`${item.runtimeKey}@${item.checkId ?? ''}`))
              ? [instance.featureId]
              : []
          )
      )

      setAnswers(current => {
        const next = { ...current }

        for (const instance of instances.slice(1)) {
          for (const item of instance.items) {
            const value = sourceValues.get(`${item.runtimeKey}@${item.checkId ?? ''}`)

            if (value !== undefined) {
              next[item.clarificationId] = value
            }
          }
        }

        return next
      })
      setInheritedFeatureIds(current => new Set([...current, ...inherited]))
    },
    [answers]
  )

  const submit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault()

      if (!gateway || !allAnswered) {
        return
      }

      setSubmitting(true)
      setError('')

      try {
        await gateway.request<{ ok?: boolean }>('clarify.respond', {
          answer: JSON.stringify({
            answers: context.items.map(item => ({
              fact_name: item.factName,
              feature_id: item.featureId,
              value: answers[item.clarificationId].trim()
            }))
          }),
          request_id: request.requestId
        })
        clearClarifyRequest(request.requestId, request.sessionId)
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause))
        setSubmitting(false)
      }
    },
    [allAnswered, answers, context.items, gateway, request.requestId, request.sessionId]
  )

  const renderQuestion = (item: DfmClarificationItem) => {
    const answered = Boolean(answers[item.clarificationId]?.trim())
    const question = item.question.replace(/^(?:【[^】]+】)+/, '')

    return (
      <label
        className={`grid gap-1.5 rounded-md border px-2 py-2 transition ${
          answered
            ? 'border-cyan-400/25 bg-cyan-500/[0.055]'
            : 'border-white/8 bg-black/10 focus-within:border-cyan-400/50'
        }`}
        key={item.clarificationId}
      >
        <span className="flex items-start justify-between gap-2 text-[10px] leading-4 text-slate-200">
          <span>{question}</span>
          {item.hasDefault && item.defaultValue !== null && (
            <span className="shrink-0 text-[9px] text-slate-500">默认：{item.defaultValue}</span>
          )}
        </span>
        {item.choices.length > 0 ? (
          <select
            className="min-w-0 rounded-md border border-white/10 bg-[#10151d] px-2 py-1.5 text-[11px] text-slate-100 outline-none focus:border-cyan-400/60"
            disabled={submitting}
            onChange={event => setAnswers(current => ({ ...current, [item.clarificationId]: event.target.value }))}
            value={answers[item.clarificationId] ?? ''}
          >
            <option value="">请选择</option>
            {item.choices.map(choice => (
              <option key={choice} value={choice}>
                {choice}
              </option>
            ))}
          </select>
        ) : (
          <input
            className="min-w-0 rounded-md border border-white/10 bg-[#10151d] px-2 py-1.5 text-[11px] text-slate-100 outline-none placeholder:text-slate-600 focus:border-cyan-400/60"
            disabled={submitting}
            onChange={event => setAnswers(current => ({ ...current, [item.clarificationId]: event.target.value }))}
            placeholder="请输入"
            value={answers[item.clarificationId] ?? ''}
          />
        )}
      </label>
    )
  }

  return (
    <form className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)_auto] gap-2" onSubmit={submit}>
      <div className="rounded-lg border border-cyan-400/15 bg-cyan-500/[0.06] px-2.5 py-2">
        <div className="flex items-center justify-between gap-3 text-[10px]">
          <div className="grid gap-0.5">
            <span className="text-cyan-100">请补充分析条件</span>
            <span className="text-slate-500">默认值只填充空项</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-slate-400">
              {answeredCount}/{context.items.length}
            </span>
            <button
              className="rounded-md border border-cyan-300/20 bg-cyan-500/10 px-2 py-1 text-[10px] text-cyan-100 hover:bg-cyan-500/20 disabled:cursor-not-allowed disabled:opacity-40"
              disabled={pendingDefaults === 0 || submitting}
              onClick={fillAllDefaults}
              type="button"
            >
              一键填充默认值{pendingDefaults > 0 ? `（${pendingDefaults}）` : ''}
            </button>
          </div>
        </div>
        <div className="mt-2 h-1 overflow-hidden rounded-full bg-white/8">
          <div
            className="h-full rounded-full bg-cyan-400 transition-[width]"
            style={{ width: `${(answeredCount / context.items.length) * 100}%` }}
          />
        </div>
        <p className="mt-1.5 text-[10px] leading-4 text-slate-400">整件 → 特征类型 → 特征实例</p>
      </div>

      <div className="min-h-0 overflow-y-auto pr-1" ref={questionsRef}>
        <div className="overflow-hidden rounded-lg border border-cyan-300/20 bg-cyan-500/[0.025]">
          <div className="border-b border-white/10 bg-cyan-500/[0.07] px-2.5 py-2">
            <div className="text-[11px] font-medium text-cyan-50">整件：当前零件</div>
            <div className="mt-0.5 text-[9px] text-slate-400">所有补充信息的最外层作用域</div>
          </div>

          <div className="grid gap-2 p-2">
            {wholeItems.length > 0 && (
              <section className="overflow-hidden rounded-lg border border-white/10 bg-white/[0.025]">
                <div className="border-b border-white/10 px-2.5 py-2">
                  <div className="text-[11px] font-medium text-slate-100">整件级</div>
                  <div className="mt-0.5 text-[9px] text-slate-500">答案对当前零件整体生效</div>
                </div>
                <div className="grid gap-1.5 p-2">{wholeItems.map(renderQuestion)}</div>
              </section>
            )}

            {featureTypeGroups.length > 0 && (
              <section className="overflow-hidden rounded-lg border border-amber-300/15 bg-amber-500/[0.025]">
                <div className="border-b border-white/10 px-2.5 py-2">
                  <div className="text-[11px] font-medium text-amber-100">特征实例级</div>
                  <div className="mt-0.5 text-[9px] text-slate-500">先按特征类型，再按模型中的实例顺序</div>
                </div>

                <div className="grid gap-2 p-2">
                  {featureTypeGroups.map(typeGroup => {
                    const source = typeGroup.instances[0]

                    const sourceReady = Boolean(
                      source && source.items.every(item => answers[item.clarificationId]?.trim())
                    )

                    const inheritableCount = Math.max(0, typeGroup.instances.length - 1)

                    return (
                      <details
                        className="overflow-hidden rounded-lg border border-white/10 bg-[#171f2b]"
                        key={typeGroup.kind}
                        onToggle={event => {
                          const isOpen = event.currentTarget.open

                          setOpenFeatureKinds(current => {
                            if (current.has(typeGroup.kind) === isOpen) {
                              return current
                            }

                            const next = new Set(current)

                            if (isOpen) {
                              next.add(typeGroup.kind)
                            } else {
                              next.delete(typeGroup.kind)
                            }

                            return next
                          })
                        }}
                        open={openFeatureKinds.has(typeGroup.kind)}
                      >
                        <summary className="flex items-center justify-between gap-2 px-2.5 py-2 text-[11px] text-slate-100">
                          <span>
                            {FEATURE_LABELS[typeGroup.kind] ?? typeGroup.kind} · {typeGroup.instances.length} 个实例
                          </span>
                          <span className="text-[9px] text-slate-500">
                            {
                              typeGroup.instances.filter(instance =>
                                instance.items.every(item => answers[item.clarificationId]?.trim())
                              ).length
                            }
                            /{typeGroup.instances.length}
                          </span>
                        </summary>

                        {inheritableCount > 0 && (
                          <div className="flex items-center justify-between gap-2 border-t border-white/8 px-2 py-1.5">
                            <span className="text-[9px] leading-4 text-slate-500">
                              先填写实例 1，再应用到同类型后续实例
                            </span>
                            <button
                              className="shrink-0 rounded-md border border-cyan-300/20 bg-cyan-500/10 px-2 py-1 text-[9px] text-cyan-100 hover:bg-cyan-500/20 disabled:cursor-not-allowed disabled:opacity-40"
                              disabled={!sourceReady || submitting}
                              onClick={() => inheritFeatureType(typeGroup.instances)}
                              type="button"
                            >
                              将实例 1 应用到后续 {inheritableCount} 个
                            </button>
                          </div>
                        )}

                        <div className="grid gap-2 border-t border-white/8 p-2">
                          {typeGroup.instances.map((instance, instanceIndex) => {
                            const featureEntry = featureById.get(instance.featureId)
                            const instanceAnswered = instance.items.every(item => answers[item.clarificationId]?.trim())

                            return (
                              <article
                                className={`grid gap-1.5 rounded-lg border p-2 ${
                                  instanceAnswered
                                    ? 'border-cyan-400/25 bg-cyan-500/[0.045]'
                                    : 'border-white/8 bg-black/10'
                                }`}
                                key={instance.featureId}
                                onFocus={() => onFeaturesHighlight([instance.featureId])}
                                onMouseEnter={() => onFeaturesHighlight([instance.featureId])}
                              >
                                <div className="flex items-center justify-between gap-2">
                                  <div>
                                    <div className="text-[10px] font-medium text-slate-100">
                                      {FEATURE_LABELS[typeGroup.kind] ?? typeGroup.kind} {instanceIndex + 1}
                                    </div>
                                    <div className="mt-0.5 text-[9px] text-slate-500">
                                      {featureEntry ? `模型特征 ${featureEntry.index + 1}` : instance.featureId}
                                    </div>
                                  </div>
                                  <span
                                    className={`text-[9px] ${instanceAnswered ? 'text-cyan-300' : 'text-slate-500'}`}
                                  >
                                    {inheritedFeatureIds.has(instance.featureId)
                                      ? '已继承，可修改'
                                      : instanceAnswered
                                        ? '已填写'
                                        : '待填写'}
                                  </span>
                                </div>
                                <div className="grid gap-1.5">{instance.items.map(renderQuestion)}</div>
                              </article>
                            )
                          })}
                        </div>
                      </details>
                    )
                  })}
                </div>
              </section>
            )}

            {inferredItems.length > 0 && (
              <section className="overflow-hidden rounded-lg border border-violet-300/15 bg-violet-500/[0.025]">
                <div className="border-b border-white/10 px-2.5 py-2">
                  <div className="text-[11px] font-medium text-violet-100">系统识别待确认</div>
                  <div className="mt-0.5 text-[9px] text-slate-500">仅显示需要人工确认的非前置项</div>
                </div>
                <div className="grid gap-1.5 p-2">{inferredItems.map(renderQuestion)}</div>
              </section>
            )}
          </div>
        </div>
      </div>

      <div className="border-t border-white/10 pt-2">
        {error && <p className="mb-2 text-[10px] text-red-200">{error}</p>}
        <button
          className="w-full rounded-md bg-cyan-500/20 px-3 py-2 text-[11px] font-medium text-cyan-50 hover:bg-cyan-500/30 disabled:cursor-not-allowed disabled:opacity-40"
          disabled={!allAnswered || submitting}
          type="submit"
        >
          {submitting ? '提交中…' : `提交全部并继续分析（${answeredCount}/${context.items.length}）`}
        </button>
      </div>
    </form>
  )
}

export function DfmViewerPane({ embedded = false, target }: { embedded?: boolean; target: DfmViewerTarget }) {
  const activeFeatureElementRef = useRef<HTMLDivElement | null>(null)
  const clarifyRequest = useStore($clarifyRequest)
  const [manifest, setManifest] = useState<ViewerManifest | null>(null)
  const projectId = target.projectId ?? manifest?.project_id
  const [scene, setScene] = useState<RenderSceneDocument | null>(null)
  const [topologyFaces, setTopologyFaces] = useState<TopologyFace[]>([])
  const [topologyEdges, setTopologyEdges] = useState<TopologyEdge[]>([])
  const [activeIssueId, setActiveIssueId] = useState<string | null>(null)
  const [activeFeatureId, setActiveFeatureId] = useState<string | null>(null)
  const [reviewNavigation, setReviewNavigation] = useState<ReviewNavigation | null>(null)
  const [highlightedFeatureIds, setHighlightedFeatureIds] = useState<string[]>([])
  const [reviewGeometryRefs, setReviewGeometryRefs] = useState<GeometryReference[]>([])
  const currentProjectId = useRef(projectId)
  currentProjectId.current = projectId
  const [clarificationPriorityFeatureId, setClarificationPriorityFeatureId] = useState<string | null>(null)
  const [pickedFaceIndex, setPickedFaceIndex] = useState<number | null>(null)
  const [editingFeatureId, setEditingFeatureId] = useState<string | null>(null)
  const [editingScopeId, setEditingScopeId] = useState<string | null>(null)
  const [editingAnnotation, setEditingAnnotation] = useState<ReviewAnnotation | null>(null)
  const [editingSnapshotId, setEditingSnapshotId] = useState<string | undefined>()
  const [annotationValues, setAnnotationValues] = useState<string[]>([])
  const [draftFaceIndices, setDraftFaceIndices] = useState<Set<number> | null>(null)
  const [projectRevision, setProjectRevision] = useState<number | null>(null)
  const [latestReviewContext, setLatestReviewContext] = useState<ReviewContext | null>(null)
  const [savingFeature, setSavingFeature] = useState(false)
  const [activeDirectionId, setActiveDirectionId] = useState<string | null>(null)
  const [axisOrigins, setAxisOrigins] = useState<Record<string, [number, number, number]>>({})
  const [directionDraft, setDirectionDraft] = useState<DirectionDraft | null>(null)
  const [directionError, setDirectionError] = useState('')
  const [preparingReview, setPreparingReview] = useState(false)
  const [editConflict, setEditConflict] = useState(false)
  const editBase = useRef<ReviewEditBase | null>(null)
  const [reviewError, setReviewError] = useState('')
  const [reviewNotice, setReviewNotice] = useState('')
  const [error, setError] = useState('')
  const [reloadRequest, setReloadRequest] = useState(0)
  const [fitRequest, setFitRequest] = useState(0)
  const [focusRequest, setFocusRequest] = useState({ sequence: 0, faceIndices: [] as number[] })
  const editingGeometry = Boolean(editingFeatureId || editingScopeId || editingAnnotation || directionDraft || preparingReview)
  const loadedTargetSignature = useRef('')
  const workingViewer = useRef<{ externalPath: string; path: string; projectId: string } | null>(null)
  const [reviewPaneTab, setReviewPaneTab] = useState<'features' | 'issues' | 'clarifications'>('features')

  useEffect(() => {
    setEditingFeatureId(null)
    setEditingScopeId(null)
    setEditingAnnotation(null)
    setDirectionDraft(null)
    setPreparingReview(false)
    setEditConflict(false)
    editBase.current = null
    setActiveDirectionId(null)
    setAxisOrigins({})
    setDirectionError('')
    setSavingFeature(false)
    setDraftFaceIndices(null)
    setReviewGeometryRefs([])
  }, [projectId, target.manifestPath])

  useEffect(() => {
    if (editingGeometry) {return}

    if (workingViewer.current && (workingViewer.current.externalPath !== target.manifestPath ||
      (target.projectId && workingViewer.current.projectId !== target.projectId))) {workingViewer.current = null}

    const manifestPath = workingViewer.current?.path ?? target.manifestPath
    const signature = JSON.stringify([reloadRequest, manifestPath, target.projectId, target.revision, target.status])

    if (loadedTargetSignature.current === signature) {return}
    let cancelled = false

    async function load() {
      try {
        setError('')
        setManifest(null)
        setScene(null)
        setTopologyFaces([])
        setTopologyEdges([])
        setPickedFaceIndex(null)
        setActiveDirectionId(null)
        setLatestReviewContext(null)
        setFocusRequest({ sequence: 0, faceIndices: [] })
        setActiveIssueId(null)
        setActiveFeatureId(null)
        setReviewNavigation(null)
        setHighlightedFeatureIds([])
        setReviewGeometryRefs([])
        setClarificationPriorityFeatureId(null)
        setEditingFeatureId(null)
        setEditingScopeId(null)
        setEditingAnnotation(null)
        setDraftFaceIndices(null)
        setReviewError('')

        if (!workingViewer.current) {setReviewNotice('')}
        const nextManifest = await readJson<ViewerManifest>(manifestPath)

        if (nextManifest.contract_version !== 'hermes.dfm.viewer/v2') {
          throw new Error('不支持的 DFM 查看器协议')
        }

        if (
          nextManifest.project_id &&
          (!/^dfm_[a-f0-9]{12,32}$/.test(nextManifest.project_id) ||
            (target.projectId && target.projectId !== nextManifest.project_id))
        ) {
          throw new Error('DFM 项目身份不一致，请重新打开对应项目。')
        }

        const artifactDir = dirname(manifestPath)

        const [nextScene, topology] = await Promise.all([
          readJson<RenderSceneDocument>(joinPath(artifactDir, nextManifest.scene_path)),
          readJson<TopologyDocument>(joinPath(artifactDir, nextManifest.topology_path))
        ])

        if (nextScene.schema_version !== 2 || topology.schema_version !== 2) {
          throw new Error('不支持的共享几何工件协议')
        }

        if (cancelled) {
          return
        }

        setManifest(nextManifest)
        loadedTargetSignature.current = signature
        setProjectRevision(nextManifest.project_revision ?? null)
        setScene(nextScene)
        setTopologyFaces(topology.faces)
        setTopologyEdges(topology.edges ?? [])
        const initialIssueId = nextManifest.issues[0]?.evaluation_id ?? null
        const initialFeatureId = initialIssueId ? null : (nextManifest.features?.[0]?.feature_id ?? null)
        setActiveIssueId(initialIssueId)
        setActiveFeatureId(initialFeatureId)
        setHighlightedFeatureIds(initialFeatureId ? [initialFeatureId] : [])
      } catch (cause) {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : String(cause))
        }
      }
    }

    void load()

    return () => {
      cancelled = true
    }
  }, [reloadRequest, target.manifestPath, target.projectId, target.revision, target.status, editingGeometry])

  const activeIssue = manifest?.issues.find(issue => issue.evaluation_id === activeIssueId) ?? null

  const activeFeatures = useMemo(() => {
    const highlighted = new Set(highlightedFeatureIds)

    return (manifest?.features ?? []).filter(feature => highlighted.has(feature.feature_id))
  }, [highlightedFeatureIds, manifest])

  const clarificationRequest =
    clarifyRequest?.context?.mode === 'dfm_analysis_batch' && clarifyRequest.context.projectId === projectId
      ? clarifyRequest
      : null

  const clarificationRequestId = clarificationRequest?.requestId
  const analysisCompleted = manifest?.status === 'completed'

  useEffect(() => {
    if (clarificationRequestId) {
      setReviewPaneTab('clarifications')
    }
  }, [clarificationRequestId])

  useEffect(() => {
    if (analysisCompleted && !clarificationRequestId) {
      setReviewPaneTab('issues')
    }
  }, [analysisCompleted, clarificationRequestId])

  const issueFacesById = useMemo(
    () =>
      new Map(
        (manifest?.issues ?? []).map(
          issue => [issue.evaluation_id, resolveGeometryRefFaceIndices(issue.geometry_refs)] as const
        )
      ),
    [manifest]
  )

  const featureFacesById = useMemo(
    () =>
      new Map(
        (manifest?.features ?? []).map(
          feature => [feature.feature_id, resolveGeometryRefFaceIndices(feature.geometry_refs)] as const
        )
      ),
    [manifest]
  )

  const issueTypeCounts = useMemo(() => {
    if (manifest?.issue_type_counts?.length) {
      return manifest.issue_type_counts.map(item => {
        const [, label] = classifyViewerIssueType({
          issue_type_id: item.issue_type_id,
          issue_type_label: item.label
        })

        return { ...item, label }
      })
    }

    return summarizeViewerIssueTypes(manifest?.issues ?? [])
  }, [manifest])

  const faceNavigationIndex = useMemo(() => createFaceNavigationIndex(latestReviewContext ?? {
    features: (manifest?.features ?? []).map(feature => ({
      feature_id: feature.feature_id, kind: feature.kind, confidence: 0,
      properties: { ...feature.parameters, geometry_refs: feature.geometry_refs }
    })), annotations: [], forming_scopes: []
  }, topologyFaces, topologyEdges), [latestReviewContext, manifest, topologyFaces, topologyEdges])

  const handleFacePick = useCallback(
    (faceIndex: number) => {
      if (directionDraft || preparingReview) {return}
      setPickedFaceIndex(faceIndex)

      if (editingFeatureId || editingScopeId || editingAnnotation) {
        setDraftFaceIndices(current => {
          const next = new Set(current ?? [])

          if (next.has(faceIndex)) {
            next.delete(faceIndex)
          } else {
            next.add(faceIndex)
          }

          return next
        })

        return
      }

      const match = faceNavigationIndex.get(faceIndex)
      setActiveDirectionId(match?.category === 'forming' ? match.objectId : null)
      setReviewGeometryRefs([])
      setActiveIssueId(null)
      setActiveFeatureId(match?.category === 'structure' ? match.objectId : null)
      setHighlightedFeatureIds(match?.category === 'structure' ? [match.objectId] : [])
      setClarificationPriorityFeatureId(match?.category === 'structure' ? match.objectId : null)

      if (match) {
        setReviewPaneTab('features')
        setReviewNavigation(previous => ({ ...match, sequence: (previous?.sequence ?? 0) + 1 }))

        if (match.category !== 'structure' && latestReviewContext) {
          const refs = match.category === 'annotation'
            ? latestReviewContext.annotations.find(item => item.annotation_id === match.objectId)?.geometry_refs
            : latestReviewContext.forming_scopes.find(item => item.forming_scope_id === match.objectId)?.geometry_refs

          setReviewGeometryRefs(resolveReviewDisplayRefs(refs ?? [], topologyFaces, topologyEdges))
        }

        return
      }

      setReviewNavigation(null)
      const issue = manifest?.issues.find(item => issueFacesById.get(item.evaluation_id)?.has(faceIndex))

      if (issue) {
        setActiveIssueId(issue.evaluation_id)
        setReviewPaneTab('issues')
      }
    },
    [directionDraft, preparingReview, editingFeatureId, editingScopeId, editingAnnotation, faceNavigationIndex, issueFacesById, manifest,
      latestReviewContext, topologyFaces, topologyEdges]
  )

  const requestFocus = useCallback((refs: GeometryReference[]) => {
    setFocusRequest(current => ({ sequence: current.sequence + 1, faceIndices: [...resolveGeometryRefFaceIndices(refs)] }))
  }, [])

  const handleIssueSelect = useCallback(
    (issue: ViewerIssue) => {
      setActiveDirectionId(null)
      setReviewGeometryRefs([])
      setActiveIssueId(issue.evaluation_id)
      setActiveFeatureId(null)
      setHighlightedFeatureIds([])
      setClarificationPriorityFeatureId(null)
      const firstFace = Array.from(issueFacesById.get(issue.evaluation_id) ?? [])[0]
      setPickedFaceIndex(firstFace ?? null)
      requestFocus(issue.geometry_refs)
    },
    [issueFacesById, requestFocus]
  )

  const handleFeatureSelect = useCallback(
    (feature: ViewerFeature) => {
      setActiveDirectionId(null)
      setReviewGeometryRefs([])
      setActiveFeatureId(feature.feature_id)
      setHighlightedFeatureIds([feature.feature_id])
      setClarificationPriorityFeatureId(feature.feature_id)
      setActiveIssueId(null)
      const firstFace = Array.from(featureFacesById.get(feature.feature_id) ?? [])[0]
      setPickedFaceIndex(firstFace ?? null)
      requestFocus(feature.geometry_refs)
    },
    [featureFacesById, requestFocus]
  )

  const handleFeaturesHighlight = useCallback(
    (featureRefs: string[]) => {
      setReviewGeometryRefs([])
      setHighlightedFeatureIds([...new Set(featureRefs.filter(ref => featureFacesById.has(ref)))])
      setActiveIssueId(null)
    },
    [featureFacesById]
  )

  const startFeatureEdit = useCallback(
    (feature: ViewerFeature, context?: ReviewContext) => {
      const source = context ?? latestReviewContext
      editBase.current = source ? captureReviewEdit(source, 'feature', feature.feature_id) : null
      setEditConflict(false)
      handleFeatureSelect(feature)
      setEditingFeatureId(feature.feature_id)
      setEditingSnapshotId(context?.snapshot_id ?? manifest?.snapshot_id)

      if (context) {setProjectRevision(context.revision)}
      setEditingScopeId(null)
      setEditingAnnotation(null)
      setDraftFaceIndices(resolveGeometryRefFaceIndices(feature.geometry_refs))
      setReviewError('')
    },
    [handleFeatureSelect, manifest?.snapshot_id, latestReviewContext]
  )

  const cancelFeatureEdit = useCallback(() => {
    editBase.current = null
    setEditConflict(false)
    setEditingFeatureId(null)
    setEditingScopeId(null)
    setEditingAnnotation(null)
    setDraftFaceIndices(null)
    setReviewError('')
  }, [])

  const applyReviewContext = useCallback((context: ReviewContext) => {
    if (currentProjectId.current !== context.project_id) {return}
    setLatestReviewContext(previous => previous?.project_id === context.project_id && previous.revision > context.revision ? previous : context)
    setProjectRevision(previous => (previous != null && previous > context.revision ? previous : context.revision))
    setReviewGeometryRefs([])
    // Only semantic state changed. Keep the already loaded scene and mesh.
    setManifest(current =>
      current && (current.project_revision ?? 0) > context.revision
        ? current
        : current
          ? {
              ...current,
              project_revision: context.revision,
              snapshot_id: context.snapshot_id,
              review_groups: context.review_groups,
              review_status: context.review_groups.some(group => group.status === 'pending_review')
                ? 'pending'
                : 'confirmed',
              features: context.features.map(feature => ({
                feature_id: feature.feature_id,
                kind: feature.kind,
                confidence: feature.confidence,
                subtype: String(feature.properties.subtype ?? ''),
                geometry_refs: feature.properties.geometry_refs ?? [],
                parameters: feature.properties,
                method: String(feature.properties.method ?? ''),
                diagnostics: feature.properties.diagnostics ?? {}
              }))
            }
          : current
    )
  }, [])

  const highlightReview = useCallback(
    (refs: GeometryReference[]) => {
      setActiveDirectionId(null)
      setActiveIssueId(null)
      setActiveFeatureId(null)
      setHighlightedFeatureIds([])
      const displayRefs = resolveReviewDisplayRefs(refs, topologyFaces, topologyEdges)
      setReviewError(displayRefs.length ? '' : '该项没有可用于定位的面引用，请刷新或检查识别结果。')
      setReviewGeometryRefs(displayRefs)
      requestFocus(displayRefs)
    },
    [topologyFaces, topologyEdges, requestFocus]
  )

  const prepareReviewForEdit = useCallback(async (pins: Pick<ReviewContext, 'revision' | 'snapshot_id' | 'edit_timeout_ms'>) => {
    if (!projectId) {throw new Error('请先打开当前项目。')}

    const context = await window.hermesDesktop.api<ReviewContext & { session_restored?: boolean; review_refreshed?: boolean; viewer_manifest?: string | null }>({
      path: `/api/dfm/projects/${encodeURIComponent(projectId)}/discovery/review/prepare`, method: 'POST',
      timeoutMs: reviewEditTimeout(pins),
      body: { expected_revision: pins.revision, expected_snapshot_id: pins.snapshot_id }
    })

    if (currentProjectId.current !== projectId) {return null}
    applyReviewContext(context)

    if (context.session_restored) {
      if (context.viewer_manifest) {workingViewer.current = { externalPath: target.manifestPath, path: context.viewer_manifest, projectId }}
      setManifest(current => current ? { ...current, status: 'discovery', issues: [], issue_count: 0, issue_type_counts: [] } : current)
      setReviewNotice('几何会话已恢复，请按当前识别结果继续编辑和确认。')
    } else if (context.review_refreshed) {
      setReviewNotice('审核状态已更新，已使用当前区域打开编辑。')
    }

    return context
  }, [projectId, target.manifestPath, applyReviewContext])

  const withCurrentReview = useCallback(async <T,>(send: (pins: { expected_revision: number; expected_snapshot_id?: string }, timeoutMs: number) => Promise<T>): Promise<T> => {
    const base = editBase.current

    if (!base || !projectId || editConflict) {throw new Error('请重新载入最新区域后编辑。')}

    const refresh = async () => {
      const current = await window.hermesDesktop.api<ReviewContext>({ path: `/api/dfm/projects/${encodeURIComponent(projectId)}/discovery/review` })

      if (currentProjectId.current !== projectId) {throw new Error('项目已切换。')}
      applyReviewContext(current)

      if (!reviewEditUnchanged(base, current)) {
        setEditConflict(true)
        throw new Error('当前区域已被修改或移除，草稿已保留。请重新载入最新区域后再编辑。')
      }

      editBase.current = captureReviewEdit(current, base.kind, base.id)
      setEditingSnapshotId(current.snapshot_id)
      setProjectRevision(current.revision)
      setDirectionDraft(draft => draft ? { ...draft, revision: current.revision, snapshotId: current.snapshot_id } : null)

      return { pins: { expected_revision: current.revision, expected_snapshot_id: current.snapshot_id }, timeoutMs: reviewEditTimeout(current) }
    }

    const current = await refresh()

    try { return await send(current.pins, current.timeoutMs) }
    catch (cause) {
      if (!isReviewConflict(cause)) {throw cause}
      // One retry is safe only after verifying that this target did not change.
      const next = await refresh()

      try { return await send(next.pins, next.timeoutMs) }
      catch (last) {
        if (isReviewConflict(last)) {throw new Error('审核状态仍在更新，草稿已保留，请稍后重试保存。')}
        throw last
      }
    }
  }, [projectId, applyReviewContext, editConflict])

  const reloadEdit = async () => {
    const base = editBase.current

    if (!base || savingFeature || preparingReview) {return}
    setPreparingReview(true)

    try {
      const current = await window.hermesDesktop.api<ReviewContext>({ path: `/api/dfm/projects/${encodeURIComponent(base.projectId)}/discovery/review` })
      const fresh = await prepareReviewForEdit(current)

      if (!fresh) {return}
      const latestBase = captureReviewEdit(fresh, base.kind, base.id)

      if (latestBase.model !== base.model || latestBase.content === 'null' && base.content !== 'null') {
        if (fresh.viewer_manifest && projectId) {workingViewer.current = { externalPath: target.manifestPath, path: fresh.viewer_manifest, projectId }}
        cancelFeatureEdit()
        setDirectionDraft(null)
        setReloadRequest(value => value + 1)
        setReviewNotice('模型或对象已更新，请从当前列表重新选择。')

        return
      }

      if (directionDraft) {
        const scope = fresh.forming_scopes.find(item => item.forming_scope_id === base.id)!
        setDirectionDraft({ ...directionDraft, scope, text: scope.draw_direction ? JSON.stringify(scope.draw_direction) : '',
          revision: fresh.revision, snapshotId: fresh.snapshot_id, origin: undefined,
          canConfirm: scope.mode === 'main' || ['undercut_scope', 'local_structure'].every(kind => fresh.review_groups.some(group => group.kind === kind && group.status === 'confirmed' && group.target_refs.includes(scope.forming_scope_id))) })
      } else if (base.kind === 'annotation') {
        const annotation = fresh.annotations.find(item => item.annotation_id === base.id)

        if (annotation) { setEditingAnnotation(annotation); setDraftFaceIndices(resolveGeometryRefFaceIndices(annotation.geometry_refs)) }
      } else {
        const refs = base.kind === 'scope' ? fresh.forming_scopes.find(item => item.forming_scope_id === base.id)?.geometry_refs : fresh.features.find(item => item.feature_id === base.id)?.properties.geometry_refs
        setDraftFaceIndices(resolveGeometryRefFaceIndices(refs))
      }

      editBase.current = latestBase
      setEditingSnapshotId(fresh.snapshot_id)
      setProjectRevision(fresh.revision)
      setEditConflict(false)
      setReviewError('')
      setDirectionError('')
    } catch (cause) { if (currentProjectId.current === base.projectId) {setReviewError(cause instanceof Error ? cause.message : String(cause))} }
    finally { if (currentProjectId.current === base.projectId) {setPreparingReview(false)} }
  }

  const currentEditedScope = latestReviewContext?.forming_scopes.find(scope => scope.forming_scope_id === editingScopeId)
  const scopeFaces = resolveGeometryRefFaceIndices(currentEditedScope?.geometry_refs)
  const scopeFacesChanged = Boolean(editingScopeId && (scopeFaces.size !== draftFaceIndices?.size || [...scopeFaces].some(face => !draftFaceIndices?.has(face))))

  const startDirectionEdit = useCallback(async (scope: ReviewScope, context: ReviewContext) => {
    if (savingFeature || preparingReview || (editingGeometry && !editingScopeId)) {return}

    if (editingScopeId && scopeFacesChanged) { setReviewError('请先保存范围修改，再输入退出方向。');

 return }

    setPreparingReview(true)
    setReviewError('')

    try {
      const fresh = await prepareReviewForEdit(context)

      if (!fresh) {return}
      const current = fresh.forming_scopes.find(item => item.forming_scope_id === scope.forming_scope_id && item.status !== 'stale')

      if (!current) {throw new Error('倒扣范围已重新识别，请从当前列表重新选择。')}
      editBase.current = captureReviewEdit(fresh, 'scope', current.forming_scope_id)
      setEditConflict(false)
      highlightReview(current.geometry_refs)
      setEditingScopeId(null)
      setDraftFaceIndices(null)
      setActiveDirectionId(current.forming_scope_id)
      setDirectionError('')
      setDirectionDraft({ scope: current, revision: fresh.revision, snapshotId: fresh.snapshot_id,
        text: current.draw_direction ? JSON.stringify(current.draw_direction) : '', mode: 'rotate', origin: axisOrigins[current.forming_scope_id],
        canConfirm: current.mode === 'main' || ['undercut_scope', 'local_structure'].every(kind => fresh.review_groups.some(group =>
          group.kind === kind && group.status === 'confirmed' && group.target_refs.includes(current.forming_scope_id))) })
    } catch (cause) {
      if (currentProjectId.current === projectId) {setReviewError(cause instanceof Error ? cause.message : String(cause))}
    } finally {
      if (currentProjectId.current === projectId) {setPreparingReview(false)}
    }
  }, [savingFeature, preparingReview, editingGeometry, editingScopeId, scopeFacesChanged, prepareReviewForEdit, highlightReview, axisOrigins, projectId])

  const saveDirectionEdit = async () => {
    if (!directionDraft || !projectId || savingFeature || editConflict) {return}
    const vector = parseDirectionVector(directionDraft.text)

    if (!vector) {return}
    setSavingFeature(true)
    setDirectionError('')

    try {
      const context = await withCurrentReview((pins, timeoutMs) => window.hermesDesktop.api<ReviewContext & { viewer_manifest?: string | null; direction_status?: string }>({
        path: `/api/dfm/projects/${encodeURIComponent(projectId)}/discovery/review/direction`, method: 'POST',
        timeoutMs,
        body: { forming_scope_id: directionDraft.scope.forming_scope_id, draw_direction: vector,
          ...pins }
      }))

      if (currentProjectId.current !== projectId) {return}

      if (context.viewer_manifest) {workingViewer.current = { externalPath: target.manifestPath, path: context.viewer_manifest, projectId }}
      applyReviewContext(context)
      setAxisOrigins(previous => directionDraft.origin ? { ...previous, [directionDraft.scope.forming_scope_id]: directionDraft.origin } :
        Object.fromEntries(Object.entries(previous).filter(([id]) => id !== directionDraft.scope.forming_scope_id)))
      setManifest(current => current ? { ...current, status: 'discovery', issues: [], issue_count: 0, issue_type_counts: [] } : current)
      setDirectionDraft(null)
      editBase.current = null
      setEditConflict(false)
      setReviewNotice(context.direction_status === 'forming_direction_staged' ? '退出方向已暂存。请确认倒扣范围和局部结构，再正式确认方向。' : '出模方向已更新。相关计算需重新分析，原报告保留为历史结果。')
    } catch (cause) {
      if (currentProjectId.current === projectId) {setDirectionError(cause instanceof Error ? cause.message : String(cause))}
    } finally {
      if (currentProjectId.current === projectId) {setSavingFeature(false)}
    }
  }

  const activeDirectionScope = latestReviewContext?.forming_scopes.find(scope => scope.forming_scope_id === activeDirectionId && scope.status !== 'stale')

  const axisState: DirectionAxisState | null = directionDraft ? {
    scopeId: directionDraft.scope.forming_scope_id, direction: parseDirectionVector(directionDraft.text),
    geometryRefs: directionDraft.scope.geometry_refs, editing: true, mode: directionDraft.mode, origin: directionDraft.origin
  } : activeDirectionScope ? { scopeId: activeDirectionScope.forming_scope_id, direction: activeDirectionScope.draw_direction,
    geometryRefs: activeDirectionScope.geometry_refs, editing: false, mode: 'rotate', origin: axisOrigins[activeDirectionScope.forming_scope_id] } : null

  const saveFeatureEdit = useCallback(async () => {
    if (
      editConflict || (!editingFeatureId && !editingScopeId && !editingAnnotation) ||
      !draftFaceIndices?.size ||
      !projectId ||
      projectRevision == null
    ) {
      setReviewError('区域至少需要保留一个面。')

      return
    }

    setSavingFeature(true)
    setReviewError('')

    try {
      const faceIndices = Array.from(draftFaceIndices).sort((left, right) => left - right)

      const refsByIndex = new Map(topologyFaces.map(face => [face.geometry_ref.index, face.geometry_ref] as const))
      const geometryRefs = faceIndices.map(index => refsByIndex.get(index))

      if (geometryRefs.some(ref => !ref?.input_sha256 || !ref.topology_snapshot_id || !ref.entity_id))
        {throw new Error('选择中包含无法验证拓扑身份的面，请刷新后重试。')}

      const result = await withCurrentReview((pins, timeoutMs) => window.hermesDesktop.api<{ revision: number; geometry_refs?: GeometryReference[]; unchanged?: boolean; viewer_manifest?: string | null }>({
        path: `/api/dfm/projects/${encodeURIComponent(projectId)}/discovery/${editingAnnotation ? 'annotations' : `${editingScopeId ? 'scopes' : 'features'}/${encodeURIComponent(editingScopeId ?? editingFeatureId!)}`}`,
        method: 'PATCH',
        timeoutMs,
        body: editingAnnotation
          ? {
              ...pins,
              annotations: [
                {
                  annotation_id: editingAnnotation.annotation_id,
                  kind: editingAnnotation.kind,
                  value: editingAnnotation.value,
                  geometry_refs: geometryRefs
                }
              ]
            }
          : { ...pins, geometry_refs: geometryRefs }
      }))

      if (currentProjectId.current !== projectId) {return}

      if (result.viewer_manifest && !result.unchanged) {
        workingViewer.current = { externalPath: target.manifestPath, path: result.viewer_manifest, projectId }
      }

      setManifest(current =>
        current
          ? {
              ...current,
              project_revision: result.revision,
              ...(result.unchanged ? {} : { status: 'discovery' as const, review_status: 'pending' as const,
                issues: [], issue_count: 0, issue_type_counts: [] }),
              features: current.features?.map(feature =>
                feature.feature_id === editingFeatureId && result.geometry_refs
                  ? {
                      ...feature,
                      geometry_refs: result.geometry_refs
                    }
                  : feature
              )
            }
          : current
      )
      setProjectRevision(result.revision)

      if (!result.unchanged) {setReviewNotice('识别结果已更新。请重新确认受影响的条目，再更新分析；原报告保留为历史结果。')}
      setEditingFeatureId(null)
      setEditingScopeId(null)
      setEditingAnnotation(null)
      setDraftFaceIndices(null)
      editBase.current = null
      setEditConflict(false)

      const context = await window.hermesDesktop.api<ReviewContext>({
        path: `/api/dfm/projects/${encodeURIComponent(projectId)}/discovery/review`
      })

      applyReviewContext(context)
    } catch (cause) {
      if (currentProjectId.current === projectId) {
        const message = cause instanceof Error ? cause.message : String(cause)
        setReviewError(/geometry_edit_conflict|snapshot_conflict|manifest_conflict/.test(message)
          ? '审核状态已更新，草稿已保留，请重试。'
          : message)
      }
    } finally {
      if (currentProjectId.current === projectId) {setSavingFeature(false)}
    }
  }, [
    applyReviewContext,
    withCurrentReview,
    editConflict,
    draftFaceIndices,
    editingFeatureId,
    editingScopeId,
    editingAnnotation,
    projectRevision,
    projectId,
    topologyFaces,
    target.manifestPath
  ])

  const startScopeEdit = useCallback(async (scope: ReviewScope, reviewed: ReviewContext) => {
    if (preparingReview || editingGeometry || savingFeature) {return}
    setPreparingReview(true)
    setReviewError('')

    try {
      const context = await prepareReviewForEdit(reviewed)

      if (!context) {return}
      const current = context.forming_scopes.find(item => item.forming_scope_id === scope.forming_scope_id && item.status !== 'stale')

      if (!current) {throw new Error('倒扣范围已重新识别，请从当前列表重新选择。')}
      editBase.current = captureReviewEdit(context, 'scope', current.forming_scope_id)
      setEditConflict(false)
      highlightReview(current.geometry_refs)
      setEditingFeatureId(null)
      setEditingScopeId(current.forming_scope_id)
      setEditingSnapshotId(context.snapshot_id)
      setEditingAnnotation(null)
      setDraftFaceIndices(resolveGeometryRefFaceIndices(current.geometry_refs))
      setProjectRevision(context.revision)
    } catch (cause) {
      if (currentProjectId.current === projectId) {setReviewError(cause instanceof Error ? cause.message : String(cause))}
    } finally {
      if (currentProjectId.current === projectId) {setPreparingReview(false)}
    }
  }, [preparingReview, editingGeometry, savingFeature, prepareReviewForEdit, highlightReview, projectId])

  const startAnnotationEdit = useCallback(
    (annotation: ReviewAnnotation, context: ReviewContext, values: string[]) => {
      if (savingFeature) {return}
      editBase.current = captureReviewEdit(context, 'annotation', annotation.annotation_id)
      setEditConflict(false)
      highlightReview(annotation.geometry_refs)
      setEditingFeatureId(null)
      setEditingScopeId(null)
      setEditingAnnotation(annotation)
      setEditingSnapshotId(context.snapshot_id)
      setAnnotationValues(values)
      setDraftFaceIndices(new Set(annotation.geometry_refs.filter(ref => ref.kind === 'face').map(ref => ref.index)))
      setProjectRevision(context.revision)
      setReviewError('')
    },
    [highlightReview, savingFeature]
  )

  const createAnnotation = useCallback(
    (kind: AnnotationKind, context: ReviewContext) => {
      const values = kind.value_schema.enum ?? []

      if (!values.length) {return}
      startAnnotationEdit(
        {
          annotation_id: `annotation_${crypto.randomUUID()}`,
          kind: kind.kind,
          value: values[0],
          status: 'active',
          geometry_refs: []
        },
        context,
        values
      )
    },
    [startAnnotationEdit]
  )

  const status = manifest?.status ?? target.status
  const canEditDiscovery = status !== 'preview' && Boolean(projectId && manifest?.snapshot_id)
  const confirmedIds = confirmedFeatureIds(manifest?.review_groups ?? [])

  const confirmedFeatures = (manifest?.features ?? []).filter(feature =>
    manifest?.review_groups ? confirmedIds.has(feature.feature_id) : status !== 'discovery'
  )

  const editedFeature = manifest?.features?.find(feature => feature.feature_id === editingFeatureId)
  const originalEditedFaces = resolveGeometryRefFaceIndices(editedFeature?.geometry_refs)

  const featureResults = ((manifest?.features?.length ?? 0) === 0 ? (
              <div className="rounded-lg border border-amber-400/20 bg-amber-400/10 p-3 text-xs leading-5 text-amber-100">
                {status === 'preview'
                  ? '分析完成后，识别到的孔、圆角、倒角等特征会显示在这里。'
                  : '当前没有可显示的结构化特征。'}
              </div>
            ) : (
              <DfmFeatureGroups
                activeFeatureElementRef={activeFeatureElementRef}
                activeFeatureId={activeFeatureId}
                editingFeatureId={editingFeatureId}
                features={confirmedFeatures}
                key={projectId ?? target.manifestPath}
                labels={FEATURE_LABELS}
                navigationSequence={reviewNavigation?.category === 'structure' ? reviewNavigation.sequence : undefined}
                renderFeature={(feature, index) => {
                  const selected = feature.feature_id === activeFeatureId
                  const editing = feature.feature_id === editingFeatureId

                  const visibleRefs = editing
                    ? Array.from(draftFaceIndices ?? []).sort((left, right) => left - right)
                    : feature.geometry_refs.map(ref => ref.index)

                  const refs = visibleRefs.map(indexValue => 'face #' + indexValue).join('、') || '无拓扑引用'

                  const confidence =
                    typeof feature.confidence === 'number' ? Math.round(feature.confidence * 100) + '%' : '—'

                  return (
                    <div
                      className={
                        'flex w-full items-start gap-2 rounded-lg border p-2.5 text-left transition ' +
                        (selected
                          ? 'border-amber-400/70 bg-amber-500/18 shadow-[0_0_0_1px_rgba(251,191,36,0.12)]'
                          : 'border-white/10 bg-white/[0.035] hover:border-white/20 hover:bg-white/[0.07]')
                      }
                      data-review-object={feature.feature_id}
                      key={feature.feature_id}
                      ref={selected ? activeFeatureElementRef : undefined}
                    >
                      <button
                        aria-pressed={selected}
                        className="flex min-w-0 flex-1 items-start gap-2 text-left"
                        disabled={editingGeometry || savingFeature}
                        onClick={() => handleFeatureSelect(feature)}
                        type="button"
                      >
                        <span className="mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-amber-500/20 text-[10px] text-amber-100">
                          {index + 1}
                        </span>
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <h2 className="text-xs font-medium text-slate-100">
                              {FEATURE_LABELS[feature.kind] ?? feature.kind}
                            </h2>
                            <span className="rounded bg-white/5 px-1.5 py-0.5 text-[9px] text-slate-400">
                              {FEATURE_SUBTYPE_LABELS[feature.subtype] ?? feature.subtype}
                            </span>
                            {Array.isArray(feature.diagnostics.unassigned_geometry_refs) && feature.diagnostics.unassigned_geometry_refs.length > 0 && (
                              <span className="text-[9px] text-amber-200">新增面待分区</span>
                            )}
                          </div>
                          <p className="mt-1.5 text-[11px] text-slate-300">
                            {formatFeatureParameters(feature.parameters)}
                          </p>
                          <p className="mt-1 text-[10px] text-slate-500">
                            置信度 {confidence} · {refs}
                          </p>
                        </div>
                      </button>
                      {canEditDiscovery && (
                        <div className="flex shrink-0 gap-1">
                          {editing ? (
                            <span className="text-[10px] text-amber-100">上方编辑中</span>
                          ) : (
                            <button
                              className="rounded bg-white/5 px-1.5 py-1 text-[10px] text-slate-300 hover:bg-white/10 disabled:opacity-50"
                              disabled={
                                Boolean(editingFeatureId || editingScopeId || editingAnnotation) || savingFeature
                              }
                              onClick={() => startFeatureEdit(feature)}
                              type="button"
                            >
                              编辑
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  )
                }}
              />
            ))

  if (error) {
    return (
      <main className="grid h-full place-items-center bg-[#10151d] p-5 text-slate-100">
        <div className="max-w-md rounded-xl border border-red-500/30 bg-red-500/10 p-5">
          <h1 className="mb-2 text-sm font-semibold">DFM 三维结果加载失败</h1>
          <p className="break-words text-xs text-red-100/80">{error}</p>
          <button
            className="mt-4 rounded-md bg-white/10 px-3 py-1.5 text-xs hover:bg-white/15"
            onClick={() => setReloadRequest(value => value + 1)}
            type="button"
          >
            重试
          </button>
        </div>
      </main>
    )
  }

  if (!manifest || !scene) {
    return (
      <main className="grid h-full place-items-center bg-[#10151d] text-xs text-slate-300">正在生成 STEP 预览…</main>
    )
  }

  return (
    <main className="grid h-full min-h-0 grid-rows-[auto_1fr] overflow-hidden bg-[#10151d] text-slate-100">
      <header className="flex min-w-0 items-center justify-between gap-3 border-b border-white/10 px-3 py-2.5">
        <div className="min-w-0">
          <h1 className="truncate text-sm font-semibold">DFM 三维模型</h1>
          <p className="mt-0.5 truncate text-[11px] text-slate-400">
            {status === 'preview'
              ? 'STEP 预览 · 等待风险分析'
              : `${manifest.scope_id} · ${manifest.verification_level}`}{' '}
            · {scene.render_mesh_snapshot.triangle_count.toLocaleString()} 个三角形
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <button
            className="rounded-md border border-white/10 bg-white/5 px-2 py-1 text-[11px] text-slate-200 hover:bg-white/10"
            onClick={() => setFitRequest(value => value + 1)}
            type="button"
          >
            适合窗口
          </button>
          {embedded && (
            <button
              className="rounded-md border border-white/10 bg-white/5 px-2 py-1 text-[11px] text-slate-200 hover:bg-white/10"
              onClick={() => void openDfmViewerWindow(target.manifestPath)}
              type="button"
            >
              弹出
            </button>
          )}
          <span
            className={`rounded-full px-2 py-1 text-[11px] ${
              status === 'preview'
                ? 'bg-sky-500/15 text-sky-200'
                : status === 'discovery'
                  ? 'bg-amber-500/15 text-amber-200'
                  : 'bg-red-500/15 text-red-200'
            }`}
          >
            {status === 'preview'
              ? '预览'
              : status === 'discovery'
                ? `${manifest.features?.length ?? 0} 个候选 · ${confirmedFeatures.length} 已确认`
                : manifest.issue_count + ' 问题 · ' + (manifest.features?.length ?? 0) + ' 特征'}
          </span>
        </div>
      </header>
      <section
        className="grid min-h-0"
        style={
          embedded
            ? { gridTemplateRows: 'minmax(14rem, 1fr) minmax(14rem, 1fr)' }
            : { gridTemplateColumns: 'minmax(0, 1fr) 20rem' }
        }
      >
        <div className="relative min-h-0 overflow-hidden">
          <ModelCanvas
            activeFeatures={
              reviewGeometryRefs.length
                ? [
                    {
                      feature_id: 'review-selection',
                      kind: 'review_scope',
                      subtype: '',
                      confidence: 1,
                      geometry_refs: reviewGeometryRefs,
                      parameters: {},
                      diagnostics: {},
                      method: 'user_review'
                    }
                  ]
                : activeFeatures
            }
            activeIssue={activeIssue}
            directionAxis={axisState}
            document={scene}
            fitRequest={fitRequest}
            focusRequest={focusRequest}
            onAxisSelected={() => {
              if (!activeDirectionScope || !latestReviewContext) {return}

              void startDirectionEdit(activeDirectionScope, latestReviewContext)
            }}
            onDirectionChange={(direction, origin) => setDirectionDraft(current => current ? { ...current, text: JSON.stringify(direction), origin } : current)}
            onFacePick={handleFacePick}
            pickedFaceIndex={pickedFaceIndex}
            selectedFaceIndices={editingFeatureId || editingScopeId || editingAnnotation ? draftFaceIndices : null}
            topologyFaces={topologyFaces}
          />
          <div className="pointer-events-none absolute bottom-2 left-2 rounded-md bg-black/55 px-2 py-1 text-[10px] text-slate-300 backdrop-blur">
            {editingFeatureId || editingScopeId || editingAnnotation
              ? '单击面切换选中 · 右键平移 · 滚轮缩放'
              : '左键旋转 · 滚轮缩放 · 右键平移 · 单击选择面'}
          </div>
          {pickedFaceIndex != null && (
            <div className="pointer-events-none absolute right-2 top-2 rounded-md border border-cyan-300/20 bg-black/60 px-2 py-1 text-[10px] text-cyan-100 backdrop-blur">
              当前面 #{pickedFaceIndex}
            </div>
          )}
        </div>
        <aside
          className={`grid min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-2 overflow-hidden bg-[#151b24] p-2.5 ${
            embedded ? 'border-t border-white/10' : 'border-l border-white/10'
          }`}
        >
          <div className="grid grid-cols-3 rounded-lg border border-white/10 bg-black/15 p-1" role="tablist">
            <button
              aria-selected={reviewPaneTab === 'features'}
              className={`flex items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-[11px] transition ${
                reviewPaneTab === 'features'
                  ? 'bg-amber-500/15 text-amber-100 shadow-sm'
                  : 'text-slate-400 hover:bg-white/5 hover:text-slate-200'
              }`}
              onClick={() => setReviewPaneTab('features')}
              role="tab"
              type="button"
            >
              特征点
              <span className="rounded-full bg-black/20 px-1.5 py-0.5 text-[9px]">
                {manifest.features?.length ?? 0}
              </span>
            </button>
            <button
              aria-selected={reviewPaneTab === 'clarifications'}
              className={`flex items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-[11px] transition ${
                reviewPaneTab === 'clarifications'
                  ? 'bg-cyan-500/15 text-cyan-100 shadow-sm'
                  : 'text-slate-400 hover:bg-white/5 hover:text-slate-200'
              }`}
              onClick={() => setReviewPaneTab('clarifications')}
              role="tab"
              type="button"
            >
              补充信息
              <span className="rounded-full bg-cyan-500/15 px-1.5 py-0.5 text-[9px] text-cyan-100">
                {clarificationRequest?.context?.items.length ?? 0}
              </span>
            </button>
            <button
              aria-selected={reviewPaneTab === 'issues'}
              className={`flex items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-[11px] transition ${
                reviewPaneTab === 'issues'
                  ? 'bg-red-500/15 text-red-100 shadow-sm'
                  : 'text-slate-400 hover:bg-white/5 hover:text-slate-200'
              }`}
              onClick={() => setReviewPaneTab('issues')}
              role="tab"
              type="button"
            >
              问题明细
              <span className="rounded-full bg-red-500/15 px-1.5 py-0.5 text-[9px] text-red-100">
                {manifest.issue_count}
              </span>
            </button>
          </div>
          <section
            className={
              reviewPaneTab === 'clarifications'
                ? 'order-1 min-h-0 overflow-hidden'
                : reviewPaneTab === 'issues'
                  ? 'order-1 min-h-0 overflow-y-auto pr-1'
                  : 'hidden'
            }
          >
            {reviewPaneTab === 'clarifications' ? (
              clarificationRequest ? (
                <DfmClarificationPanel
                  features={manifest.features ?? []}
                  onFeaturesHighlight={handleFeaturesHighlight}
                  priorityFeatureId={clarificationPriorityFeatureId}
                  request={clarificationRequest}
                />
              ) : (
                <div className="rounded-lg border border-cyan-400/15 bg-cyan-500/[0.06] p-3 text-xs leading-5 text-cyan-100">
                  当前没有待补充的信息。
                </div>
              )
            ) : (
              <>
                <div className="mb-2 flex items-center justify-between">
                  <h2 className="text-xs font-semibold text-red-100">问题分类与明细</h2>
                  <span className="rounded-full bg-red-500/15 px-2 py-0.5 text-[10px] text-red-200">
                    {manifest.issue_count}
                  </span>
                </div>
                {issueTypeCounts.length > 0 && (
                  <div className="mb-2 flex flex-wrap gap-1.5">
                    {issueTypeCounts.map(item => (
                      <span
                        className="rounded-full border border-red-300/15 bg-red-500/10 px-2 py-1 text-[10px] text-red-100"
                        key={item.issue_type_id}
                      >
                        {item.label} <strong className="ml-1 font-semibold">{item.count}</strong>
                      </span>
                    ))}
                  </div>
                )}
                {manifest.issues.length === 0 ? (
                  <div className="rounded-lg border border-sky-400/20 bg-sky-400/10 p-3 text-xs leading-5 text-sky-100">
                    {status === 'preview'
                      ? '模型已加载。DFM 分析完成后，风险项会自动更新到这里。'
                      : '当前规则未发现需要高亮的问题。'}
                  </div>
                ) : (
                  <div className="grid gap-2">
                    {manifest.issues.map((issue, index) => {
                      const selected = issue.evaluation_id === activeIssueId

                      const refs =
                        issue.geometry_refs.map(ref => `${ref.kind} #${ref.index}`).join('、') || '无拓扑引用'

                      const [, issueTypeLabel] = classifyViewerIssueType(issue)

                      return (
                        <button
                          aria-pressed={selected}
                          className={`w-full rounded-lg border p-2.5 text-left transition ${
                            selected
                              ? 'border-red-400/70 bg-red-500/18 shadow-[0_0_0_1px_rgba(248,113,113,0.12)]'
                              : 'border-white/10 bg-white/[0.035] hover:border-white/20 hover:bg-white/[0.07]'
                          }`}
                          key={issue.evaluation_id}
                          onClick={() => handleIssueSelect(issue)}
                          type="button"
                        >
                          <div className="flex items-start gap-2">
                            <span className="mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-red-500/20 text-[10px] text-red-100">
                              {index + 1}
                            </span>
                            <div className="min-w-0">
                              <h2 className="text-xs font-medium text-slate-100">{issue.title}</h2>
                              <p className="mt-1 text-[11px] text-red-200">{issueTypeLabel}</p>
                              <p className="mt-0.5 text-[10px] text-slate-500">{issue.check_id || issue.metric_id}</p>
                              <p className="mt-1.5 text-[11px] text-slate-300">
                                未满足：实际 {formatViewerValue(issue.actual)}{' '}
                                {issue.operator === 'between' ? '∈' : issue.operator} 目标{' '}
                                {formatViewerValue(issue.expected)}
                              </p>
                              <p className="mt-1 break-words text-[10px] text-slate-500">{refs}</p>
                            </div>
                          </div>
                        </button>
                      )
                    })}
                  </div>
                )}
              </>
            )}
          </section>
          <section className={reviewPaneTab === 'features' ? 'order-1 min-h-0 overflow-y-auto pr-1' : 'hidden'}>
            {preparingReview && <p className="mb-2 text-[11px] text-cyan-100" role="status">正在准备编辑…</p>}
            {reviewNotice && <p className="mb-2 rounded border border-cyan-300/20 bg-cyan-500/10 p-2 text-[10px] text-cyan-100">{reviewNotice}</p>}
            {editConflict && <div className="mb-2 rounded border border-amber-400/40 p-2 text-[11px] text-amber-100" role="alert">
              <p>当前区域已更新，草稿仍保留。重新载入会以最新内容替换草稿。</p>
              <button className="mt-1 text-cyan-100" disabled={savingFeature || preparingReview} onClick={() => void reloadEdit()} type="button">重新载入最新区域</button>
            </div>}
            {editedFeature && (
              <DfmFeatureEditor
                addedCount={[...(draftFaceIndices ?? [])].filter(index => !originalEditedFaces.has(index)).length}
                busy={savingFeature}
                faceCount={draftFaceIndices?.size ?? 0}
                identity={editedFeature.feature_id}
                label={FEATURE_LABELS[editedFeature.kind] ?? editedFeature.kind}
                onCancel={cancelFeatureEdit}
                onRestore={() => setDraftFaceIndices(new Set(originalEditedFaces))}
                onSave={() => void saveFeatureEdit()}
                removedCount={[...originalEditedFaces].filter(index => !draftFaceIndices?.has(index)).length}
                saveBlocked={editConflict}
              />
            )}
            {editingAnnotation && (
              <DfmAnnotationEditor
                busy={savingFeature}
                faceCount={draftFaceIndices?.size ?? 0}
                identity={editingAnnotation.annotation_id}
                onCancel={cancelFeatureEdit}
                onSave={() => void saveFeatureEdit()}
                onValue={value => setEditingAnnotation(current => (current ? { ...current, value } : null))}
                saveBlocked={editConflict}
                value={editingAnnotation.value}
                values={annotationValues}
              />
            )}
            {editingScopeId && (
              <div aria-label="编辑倒扣范围" className="mb-2 rounded border border-amber-300/30 p-2 text-[11px] text-amber-100" role="region">
                <p>
                  编辑范围 {editingScopeId} · 已选 {draftFaceIndices?.size ?? 0} 个面
                </p>
                <p>保存后需重新确认范围、局部结构和退出方向。</p>
                <button className="mt-2 mr-3 text-cyan-200 disabled:opacity-40" disabled={savingFeature || preparingReview || scopeFacesChanged}
                  onClick={() => { if (currentEditedScope && latestReviewContext) {void startDirectionEdit(currentEditedScope, latestReviewContext)} }} type="button">输入退出方向</button>
                {scopeFacesChanged && <p className="mt-1 text-[10px] text-slate-300">请先保存当前选面，再编辑方向。</p>}
                <button
                  className="mr-3 mt-2"
                  disabled={savingFeature || editConflict || !draftFaceIndices?.size}
                  onClick={() => void saveFeatureEdit()}
                  type="button"
                >
                  {savingFeature ? '保存中…' : '保存范围'}
                </button>
                {savingFeature && <p className="mt-1 text-slate-300" role="status">正在保存范围并更新审核状态，请稍候。</p>}
                <button disabled={savingFeature} onClick={cancelFeatureEdit} type="button">
                  取消
                </button>
                {reviewError && (
                  <p className="mt-2 text-red-200" role="alert">
                    {reviewError}
                  </p>
                )}
              </div>
            )}
            {directionDraft && <DfmDirectionEditor busy={savingFeature} draft={directionDraft} error={directionError} onCancel={() => { setDirectionDraft(null); setDirectionError(''); setEditConflict(false); editBase.current = null }}
              onChange={setDirectionDraft} onSave={() => void saveDirectionEdit()} saveBlocked={editConflict} />}
            {projectId && status !== 'preview' && (
              <DfmReviewPanel
                confirmedFeaturesContent={featureResults}
                editing={Boolean(editingGeometry || savingFeature)}
                latestContext={latestReviewContext}
                navigation={reviewNavigation}
                onChanged={applyReviewContext}
                onCreateAnnotation={createAnnotation}
                onEditAnnotation={startAnnotationEdit}
                onEditDirection={startDirectionEdit}
                onEditFeature={canEditDiscovery ? (feature, context) => {
                  applyReviewContext(context)
                  startFeatureEdit({ feature_id: feature.feature_id, kind: feature.kind, confidence: feature.confidence,
                    geometry_refs: feature.properties.geometry_refs ?? [], subtype: String(feature.properties.subtype ?? ''),
                    parameters: feature.properties, diagnostics: feature.properties.diagnostics ?? {},
                    method: String(feature.properties.method ?? '') }, context)
                } : undefined}
                onEditScope={startScopeEdit}
                onHighlight={highlightReview}
                onRefreshed={applyReviewContext}
                onSelectDirection={scope => setActiveDirectionId(scope.forming_scope_id)}
                projectId={projectId}
                revision={projectRevision}
              />
            )}
            {reviewError && (
              <div className="mb-2 rounded-md border border-red-400/25 bg-red-500/10 px-2 py-1.5 text-[10px] text-red-100">
                {reviewError}
              </div>
            )}
            {(!projectId || status === 'preview') && featureResults}
          </section>
        </aside>
      </section>
    </main>
  )
}
