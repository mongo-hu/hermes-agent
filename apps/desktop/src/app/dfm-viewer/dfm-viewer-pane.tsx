import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
import type { DfmViewerTarget } from '@/store/dfm-viewer'

import {
  type GeometryReference,
  mergeRenderScene,
  type RenderPrimitive,
  resolveGeometryRefFaceIndices,
  type TopologyFace
} from './dfm-viewer-geometry'

interface ViewerIssue {
  actual: unknown
  evaluation_id: string
  expected: unknown
  geometry_refs: GeometryReference[]
  metric_id: string
  operator: string
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
  feature_count?: number
  features?: ViewerFeature[]
  issue_count: number
  issues: ViewerIssue[]
  scene_path: string
  scope_id: string
  status?: 'completed' | 'preview'
  topology_path: string
  verification_level: string
}

interface RenderSceneDocument {
  primitives: RenderPrimitive[]
  render_mesh_snapshot: { triangle_count: number }
  schema_version: 2
}

interface TopologyDocument {
  faces: TopologyFace[]
  schema_version: 2
}

interface SceneResources {
  faceGroups: Map<number, number>
  fit: () => void
  geometry: BufferGeometry
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
  activeFeature,
  activeIssue,
  document,
  fitRequest,
  onFacePick,
  pickedFaceIndex,
  topologyFaces
}: {
  activeFeature: ViewerFeature | null
  activeIssue: ViewerIssue | null
  document: RenderSceneDocument
  fitRequest: number
  onFacePick: (faceIndex: number) => void
  pickedFaceIndex: number | null
  topologyFaces: TopologyFace[]
}) {
  const hostRef = useRef<HTMLDivElement>(null)
  const resourcesRef = useRef<SceneResources | null>(null)
  const onFacePickRef = useRef(onFacePick)

  const problemFaces = useMemo(() => resolveGeometryRefFaceIndices(activeIssue?.geometry_refs), [activeIssue])

  const featureFaces = useMemo(() => resolveGeometryRefFaceIndices(activeFeature?.geometry_refs), [activeFeature])

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
      side: DoubleSide
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

    const resize = () => {
      const width = Math.max(host.clientWidth, 1)
      const height = Math.max(host.clientHeight, 1)
      camera.aspect = width / height
      camera.updateProjectionMatrix()
      renderer.setSize(width, height, false)
    }

    const fit = () => {
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
      faceGroups,
      featureMaterial,
      fit,
      geometry,
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
      if (event.button !== 0) {
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
      controls.dispose()
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
          : pickedFaceIndex === faceIndex
            ? 3
            : 0
    }
  }, [featureFaces, pickedFaceIndex, problemFaces])

  useEffect(() => {
    if (fitRequest > 0) {
      resourcesRef.current?.fit()
    }
  }, [fitRequest])

  return <div className="h-full min-h-0 w-full" ref={hostRef} />
}

function formatValue(value: unknown): string {
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : value.toFixed(3)
  }

  return String(value ?? '—')
}

const FEATURE_LABELS: Record<string, string> = {
  blend: '圆角链',
  canonical_surface: '规范曲面',
  cavity: '型腔',
  chamfer: '倒角',
  convex_hull: '凸包面集',
  drilled_hole: '钻孔',
  isolated: '孤立特征',
  rib: '加强筋',
  shaft: '轴类特征',
  surface_probe: '曲面探针'
}

const FEATURE_SUBTYPE_LABELS: Record<string, string> = {
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
          formatValue(value) +
          (key.endsWith('_mm') ? ' mm' : key.endsWith('_deg') ? '°' : '')
      )
      .join(' · ') || '无尺寸参数'
  )
}

export function DfmViewerPane({ embedded = false, target }: { embedded?: boolean; target: DfmViewerTarget }) {
  const [manifest, setManifest] = useState<ViewerManifest | null>(null)
  const [scene, setScene] = useState<RenderSceneDocument | null>(null)
  const [topologyFaces, setTopologyFaces] = useState<TopologyFace[]>([])
  const [activeIssueId, setActiveIssueId] = useState<string | null>(null)
  const [activeFeatureId, setActiveFeatureId] = useState<string | null>(null)
  const [pickedFaceIndex, setPickedFaceIndex] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [reloadRequest, setReloadRequest] = useState(0)
  const [fitRequest, setFitRequest] = useState(0)

  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        setError('')
        setManifest(null)
        setScene(null)
        setTopologyFaces([])
        setPickedFaceIndex(null)
        setActiveIssueId(null)
        setActiveFeatureId(null)
        const nextManifest = await readJson<ViewerManifest>(target.manifestPath)

        if (nextManifest.contract_version !== 'hermes.dfm.viewer/v2') {
          throw new Error('不支持的 DFM 查看器协议')
        }

        const artifactDir = dirname(target.manifestPath)

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
        setScene(nextScene)
        setTopologyFaces(topology.faces)
        const initialIssueId = nextManifest.issues[0]?.evaluation_id ?? null
        setActiveIssueId(initialIssueId)
        setActiveFeatureId(initialIssueId ? null : (nextManifest.features?.[0]?.feature_id ?? null))
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
  }, [reloadRequest, target.manifestPath])

  const activeIssue = manifest?.issues.find(issue => issue.evaluation_id === activeIssueId) ?? null
  const activeFeature = manifest?.features?.find(feature => feature.feature_id === activeFeatureId) ?? null

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

  const handleFacePick = useCallback(
    (faceIndex: number) => {
      setPickedFaceIndex(faceIndex)
      const matchingIssue = manifest?.issues.find(issue => issueFacesById.get(issue.evaluation_id)?.has(faceIndex))

      if (matchingIssue) {
        setActiveIssueId(matchingIssue.evaluation_id)
        setActiveFeatureId(null)

        return
      }

      const matchingFeature = manifest?.features?.find(feature =>
        featureFacesById.get(feature.feature_id)?.has(faceIndex)
      )

      setActiveIssueId(null)
      setActiveFeatureId(matchingFeature?.feature_id ?? null)
    },
    [featureFacesById, issueFacesById, manifest]
  )

  const handleIssueSelect = useCallback(
    (issue: ViewerIssue) => {
      setActiveIssueId(issue.evaluation_id)
      setActiveFeatureId(null)
      const firstFace = Array.from(issueFacesById.get(issue.evaluation_id) ?? [])[0]
      setPickedFaceIndex(firstFace ?? null)
    },
    [issueFacesById]
  )

  const handleFeatureSelect = useCallback(
    (feature: ViewerFeature) => {
      setActiveFeatureId(feature.feature_id)
      setActiveIssueId(null)
      const firstFace = Array.from(featureFacesById.get(feature.feature_id) ?? [])[0]
      setPickedFaceIndex(firstFace ?? null)
    },
    [featureFacesById]
  )

  const status = manifest?.status ?? target.status

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
              status === 'preview' ? 'bg-sky-500/15 text-sky-200' : 'bg-red-500/15 text-red-200'
            }`}
          >
            {status === 'preview'
              ? '预览'
              : manifest.issue_count + ' 问题 · ' + (manifest.features?.length ?? 0) + ' 特征'}
          </span>
        </div>
      </header>
      <section
        className="grid min-h-0"
        style={
          embedded
            ? { gridTemplateRows: 'minmax(14rem, 3fr) minmax(9rem, 2fr)' }
            : { gridTemplateColumns: 'minmax(0, 1fr) 20rem' }
        }
      >
        <div className="relative min-h-0 overflow-hidden">
          <ModelCanvas
            activeFeature={activeFeature}
            activeIssue={activeIssue}
            document={scene}
            fitRequest={fitRequest}
            onFacePick={handleFacePick}
            pickedFaceIndex={pickedFaceIndex}
            topologyFaces={topologyFaces}
          />
          <div className="pointer-events-none absolute bottom-2 left-2 rounded-md bg-black/55 px-2 py-1 text-[10px] text-slate-300 backdrop-blur">
            左键旋转 · 滚轮缩放 · 右键平移 · 单击选择面
          </div>
          {pickedFaceIndex != null && (
            <div className="pointer-events-none absolute right-2 top-2 rounded-md border border-cyan-300/20 bg-black/60 px-2 py-1 text-[10px] text-cyan-100 backdrop-blur">
              当前面 #{pickedFaceIndex}
            </div>
          )}
        </div>
        <aside
          className={`grid min-h-0 grid-rows-2 gap-2 overflow-hidden bg-[#151b24] p-2.5 ${
            embedded ? 'border-t border-white/10' : 'border-l border-white/10'
          }`}
        >
          <section className="min-h-0 overflow-y-auto pr-1">
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-xs font-semibold text-red-100">问题点</h2>
              <span className="rounded-full bg-red-500/15 px-2 py-0.5 text-[10px] text-red-200">
                {manifest.issue_count}
              </span>
            </div>
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
                  const refs = issue.geometry_refs.map(ref => `${ref.kind} #${ref.index}`).join('、') || '无拓扑引用'

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
                          <p className="mt-1 text-[11px] text-slate-400">{issue.metric_id}</p>
                          <p className="mt-1.5 text-[11px] text-slate-300">
                            实际 {formatValue(issue.actual)} {issue.operator} 目标 {formatValue(issue.expected)}
                          </p>
                          <p className="mt-1 break-words text-[10px] text-slate-500">{refs}</p>
                        </div>
                      </div>
                    </button>
                  )
                })}
              </div>
            )}
          </section>
          <section className="min-h-0 overflow-y-auto border-t border-white/10 pr-1 pt-2">
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-xs font-semibold text-amber-100">特征点</h2>
              <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] text-amber-200">
                {manifest.features?.length ?? 0}
              </span>
            </div>
            {(manifest.features?.length ?? 0) === 0 ? (
              <div className="rounded-lg border border-amber-400/20 bg-amber-400/10 p-3 text-xs leading-5 text-amber-100">
                {status === 'preview'
                  ? '分析完成后，识别到的孔、圆角、倒角等特征会显示在这里。'
                  : '当前没有可显示的结构化特征。'}
              </div>
            ) : (
              <div className="grid gap-2">
                {manifest.features?.map((feature, index) => {
                  const selected = feature.feature_id === activeFeatureId
                  const refs = feature.geometry_refs.map(ref => ref.kind + ' #' + ref.index).join('、') || '无拓扑引用'

                  const confidence =
                    typeof feature.confidence === 'number' ? Math.round(feature.confidence * 100) + '%' : '—'

                  return (
                    <button
                      aria-pressed={selected}
                      className={
                        'w-full rounded-lg border p-2.5 text-left transition ' +
                        (selected
                          ? 'border-amber-400/70 bg-amber-500/18 shadow-[0_0_0_1px_rgba(251,191,36,0.12)]'
                          : 'border-white/10 bg-white/[0.035] hover:border-white/20 hover:bg-white/[0.07]')
                      }
                      key={feature.feature_id}
                      onClick={() => handleFeatureSelect(feature)}
                      type="button"
                    >
                      <div className="flex items-start gap-2">
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
                          </div>
                          <p className="mt-1.5 text-[11px] text-slate-300">
                            {formatFeatureParameters(feature.parameters)}
                          </p>
                          <p className="mt-1 text-[10px] text-slate-500">
                            置信度 {confidence} · {refs}
                          </p>
                        </div>
                      </div>
                    </button>
                  )
                })}
              </div>
            )}
          </section>
        </aside>
      </section>
    </main>
  )
}
