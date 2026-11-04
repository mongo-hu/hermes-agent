import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Box3, BufferGeometry, Color, DirectionalLight, DoubleSide, EdgesGeometry,
  Float32BufferAttribute, Group, HemisphereLight, LineBasicMaterial, LineSegments,
  MathUtils, Matrix4, Mesh, MeshStandardMaterial, OrthographicCamera,
  Quaternion, Raycaster, Scene, Sphere, SRGBColorSpace, Uint32BufferAttribute,
  Vector2, Vector3, WebGLRenderer
} from 'three'

import { createDfmCameraControls, resizeDfmCameraControls } from './dfm-camera-controls'
import { attachCanvasMenus, type CanvasMenuPoint, type CanvasViewMenu } from './dfm-canvas-menus'
import { createDirectionAxis, type DirectionAxisState } from './dfm-direction-axis'
import { type DfmViewCommand, DfmViewCompass, type DfmViewCompassHandle, DfmViewMenu } from './dfm-view-menu'
import { type CameraOrientation, fitOrthographicCamera, nearestStandardPose, resizeOrthographicCamera,
  setOrthographicVisibleHeight, standardCameraPose, updateOrthographicClipping } from './dfm-viewer-camera'
import { createFocusIndex, selectionCameraPose } from './dfm-viewer-focus'
import { type GeometryReference, mergeRenderScene, type RenderPrimitive,
  type RenderTriangleReference, resolveGeometryRefFaceIndices, resolveTriangleRefPositions, type TopologyFace } from './dfm-viewer-geometry'

interface ViewerFeature { geometry_refs: GeometryReference[] }
interface ViewerIssue { geometry_refs: GeometryReference[]; triangle_refs?: RenderTriangleReference[] }
interface RenderSceneDocument {
  primitives: RenderPrimitive[]
  render_mesh_snapshot: { render_mesh_snapshot_id?: string; triangle_count: number }
  schema_version: 2
}

interface SceneResources {
  cancelMenu: () => void
  directionAxis: ReturnType<typeof createDirectionAxis>
  focus: (faces: ReadonlySet<number>) => void
  faceGroups: Map<number, number>
  command: (command: DfmViewCommand) => void
  fit: () => void
  geometry: BufferGeometry
  issueOverlay: Mesh | null
  formingOutline: LineSegments | null
  modelGroup: Group
  featureMaterial: MeshStandardMaterial
  normalMaterial: MeshStandardMaterial
  pickedMaterial: MeshStandardMaterial
  problemMaterial: MeshStandardMaterial
}

export function ModelCanvas({
  activeFeatures,
  activeIssue,
  document,
  fitRequest,
  focusRequest,
  onFacePick,
  onBackgroundClick,
  pickedFaceIndex,
  selectedFaceIndices,
  formingScope,
  topologyFaces,
  directionAxis,
  axisSelectable = true,
  onDirectionChange,
  onAxisSelected,
  onObjectMenu,
  onDismissMenu
}: {
  activeFeatures: ViewerFeature[]
  activeIssue: ViewerIssue | null
  document: RenderSceneDocument
  fitRequest: number
  focusRequest: { sequence: number; faceIndices: number[] }
  onFacePick: (faceIndex: number) => void
  onBackgroundClick: () => void
  pickedFaceIndex: number | null
  selectedFaceIndices?: Set<number> | null
  formingScope?: { geometry_refs: GeometryReference[]; undercut_geometry_refs: GeometryReference[] } | null
  directionAxis: DirectionAxisState | null
  axisSelectable?: boolean
  onDirectionChange: (direction: [number, number, number], origin: [number, number, number]) => void
  onAxisSelected: () => void
  onObjectMenu: (faceIndex: number, point: CanvasMenuPoint) => void
  onDismissMenu: () => void
  topologyFaces: TopologyFace[]
}) {
  const hostRef = useRef<HTMLDivElement>(null)
  const compassRef = useRef<DfmViewCompassHandle>(null)
  const [viewMenu, setViewMenu] = useState<CanvasViewMenu | null>(null)
  const objectMenuRef = useRef(onObjectMenu)
  const dismissMenuRef = useRef(onDismissMenu)
  objectMenuRef.current = onObjectMenu
  dismissMenuRef.current = onDismissMenu
  const resourcesRef = useRef<SceneResources | null>(null)
  const onFacePickRef = useRef(onFacePick)
  const onBackgroundClickRef = useRef(onBackgroundClick)
  const directionRef = useRef(directionAxis)
  const directionChangeRef = useRef(onDirectionChange)
  const axisSelectedRef = useRef(onAxisSelected)
  const axisSelectableRef = useRef(axisSelectable)
  directionRef.current = directionAxis
  directionChangeRef.current = onDirectionChange
  axisSelectedRef.current = onAxisSelected
  axisSelectableRef.current = axisSelectable

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
  const undercutFaces = useMemo(() => resolveGeometryRefFaceIndices(formingScope?.undercut_geometry_refs), [formingScope?.undercut_geometry_refs])
  const formingFaces = useMemo(() => resolveGeometryRefFaceIndices(formingScope?.geometry_refs), [formingScope?.geometry_refs])

  useEffect(() => {
    onFacePickRef.current = onFacePick
    onBackgroundClickRef.current = onBackgroundClick
  }, [onFacePick, onBackgroundClick])

  useEffect(() => {
    const host = hostRef.current

    if (!host) {
      return
    }

    const scene = new Scene()
    scene.background = new Color(0x091019)
    const camera = new OrthographicCamera(-1, 1, 1, -1, 0.01, 100000)
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

    camera.position.copy(sphere.center).add(new Vector3(1.25, -1.55, 1.05).normalize().multiplyScalar(radius * 4))
    camera.lookAt(sphere.center)

    const makeControls = (target: Vector3) => createDfmCameraControls(camera, renderer.domElement, target)

    let controls = makeControls(sphere.center)

    const axisController = createDirectionAxis(scene, camera, renderer.domElement, controls, focusIndex,
      (direction, origin) => directionChangeRef.current(direction, origin))

    axisController.update(directionRef.current)

    let focusAnimation: { start: number; fromRotation: Quaternion; toRotation: Quaternion; distance: number;
      fromHeight: number; toHeight: number; fromTarget: Vector3; toTarget: Vector3; up: Vector3 } | null = null

    const rebuildControls = () => {
      const target = controls.target.clone()
      controls.removeEventListener('start', cancelFocus)
      controls.dispose()
      controls = makeControls(target)
      controls.addEventListener('start', cancelFocus)
      axisController.setControls(controls)
    }

    const cancelFocus = () => {
      if (!focusAnimation) {return}
      focusAnimation = null
      camera.up.set(0, 1, 0).applyQuaternion(camera.quaternion)
      rebuildControls()
    }

    controls.addEventListener('start', cancelFocus)

    // Interrupt before navigation receives the same input, so its first drag is not lost.
    const interruptAnimation = () => { cancelFocus() }
    host.addEventListener('pointerdown', interruptAnimation, true)
    host.addEventListener('wheel', interruptAnimation, true)

    const resize = () => {
      const width = Math.max(host.clientWidth, 1)
      const height = Math.max(host.clientHeight, 1)
      resizeOrthographicCamera(camera, width / height)
      renderer.setSize(width, height, false)
      resizeDfmCameraControls(controls, camera, renderer.domElement)
    }

    const fit = () => {
      cancelFocus()
      resize()
      fitOrthographicCamera(camera, controls.target, bounds)
      rebuildControls()
    }

    const animatePose = (pose: CameraOrientation, target = controls.target.clone(), height = (camera.top - camera.bottom) / camera.zoom) => {
      cancelFocus()
      rebuildControls()
      focusAnimation = { start: performance.now(), fromRotation: camera.quaternion.clone(),
        toRotation: new Quaternion().setFromRotationMatrix(new Matrix4().lookAt(pose.direction, new Vector3(), pose.up)),
        distance: Math.max(camera.position.distanceTo(controls.target), radius * 4),
        fromHeight: (camera.top - camera.bottom) / camera.zoom, toHeight: height,
        fromTarget: controls.target.clone(), toTarget: target.clone(), up: pose.up.clone() }
    }

    const command = (action: DfmViewCommand) => {
      canvasMenus.cancel()

      if (action === 'fit') {fit();

 return}

      animatePose(action === 'align' ? nearestStandardPose(camera) : standardCameraPose(action))
    }

    const observer = new ResizeObserver(resize)
    observer.observe(host)
    fit()

    const faceGroups = new Map(merged.groups.map((faceGroup, index) => [faceGroup.faceIndex, index]))
    resourcesRef.current = {
      cancelMenu: () => canvasMenus.cancel(),
      directionAxis: axisController,
      command,
      focus: faces => {
        const pose = selectionCameraPose(focusIndex, faces, camera)

        if (!pose) {return}
        animatePose(pose, pose.target, pose.visibleHeight)
      },
      faceGroups,
      featureMaterial,
      fit,
      geometry,
      issueOverlay: null,
      formingOutline: null,
      modelGroup,
      normalMaterial,
      pickedMaterial,
      problemMaterial
    }

    const raycaster = new Raycaster()
    const pointer = new Vector2()
    let pointerDown: { x: number; y: number } | null = null

    const hitTest = (event: PointerEvent): { model: boolean; faceIndex: number | null } => {
      const rect = renderer.domElement.getBoundingClientRect()

      if (rect.width <= 0 || rect.height <= 0) {
        return { model: false, faceIndex: null }
      }

      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1
      pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1
      raycaster.setFromCamera(pointer, camera)
      const intersection = raycaster.intersectObject(modelMesh, false)[0]
      const triangleIndex = intersection?.faceIndex

      return { model: Boolean(intersection), faceIndex: triangleIndex == null ? null : (merged.triangleFaceIndices[triangleIndex] ?? null) }
    }

    const canvasMenus = attachCanvasMenus(renderer.domElement, {
      hitTest, onMenu: setViewMenu, onCommand: command,
      onObject: (faceIndex, point) => objectMenuRef.current(faceIndex, point),
      onStart: () => { cancelFocus(); dismissMenuRef.current() },
      blocked: () => Boolean(directionRef.current?.editing)
    })

    const handlePointerDown = (event: PointerEvent) => {
      if (event.button !== 0 || directionRef.current?.editing) {
        return
      }

      pointerDown = { x: event.clientX, y: event.clientY }
      dismissMenuRef.current()
      renderer.domElement.style.cursor = 'grabbing'
    }

    const handlePointerUp = (event: PointerEvent) => {
      renderer.domElement.style.cursor = 'grab'

      if (event.button !== 0 || !pointerDown || Math.hypot(event.clientX - pointerDown.x, event.clientY - pointerDown.y) > 4) {
        pointerDown = null

        return
      }

      pointerDown = null

      if (directionRef.current?.editing) {return}

      if (axisSelectableRef.current && axisController.hit(event)) { axisSelectedRef.current();

 return }

      const { faceIndex } = hitTest(event)

      if (faceIndex != null && faceIndex > 0) {
        onFacePickRef.current(faceIndex)
      } else {
        onBackgroundClickRef.current()
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
        const t = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 1 : Math.min((performance.now() - focusAnimation.start) / 280, 1)
        const blend = t * t * (3 - 2 * t)
        controls.target.lerpVectors(focusAnimation.fromTarget, focusAnimation.toTarget, blend)
        camera.quaternion.slerpQuaternions(focusAnimation.fromRotation, focusAnimation.toRotation, blend)
        camera.position.set(0, 0, 1).applyQuaternion(camera.quaternion)
          .multiplyScalar(focusAnimation.distance).add(controls.target)
        camera.up.set(0, 1, 0).applyQuaternion(camera.quaternion)
        setOrthographicVisibleHeight(camera, MathUtils.lerp(focusAnimation.fromHeight, focusAnimation.toHeight, blend))
        updateOrthographicClipping(camera, bounds)

        if (t === 1) { camera.up.copy(focusAnimation.up); focusAnimation = null; rebuildControls() }
      } else {
        controls.update()
        updateOrthographicClipping(camera, bounds)
      }

      compassRef.current?.setCameraQuaternion(camera.quaternion)
      host.dataset.cameraProjection = 'orthographic'
      host.dataset.cameraDirection = JSON.stringify(camera.getWorldDirection(new Vector3()).toArray())
      host.dataset.cameraTarget = JSON.stringify(controls.target.toArray())
      host.dataset.cameraZoom = String(camera.zoom)
      renderer.render(scene, camera)
      frame = requestAnimationFrame(render)
    }

    render()

    return () => {
      cancelAnimationFrame(frame)
      observer.disconnect()
      host.removeEventListener('pointerdown', interruptAnimation, true)
      host.removeEventListener('wheel', interruptAnimation, true)
      canvasMenus.dispose()
      renderer.domElement.removeEventListener('pointerdown', handlePointerDown)
      renderer.domElement.removeEventListener('pointerup', handlePointerUp)
      renderer.domElement.removeEventListener('pointercancel', handlePointerCancel)
      renderer.domElement.removeEventListener('pointerleave', handlePointerCancel)
      axisController.dispose()
      if (resourcesRef.current?.formingOutline) {
        const material = resourcesRef.current.formingOutline.material
        if (!Array.isArray(material)) {material.dispose()}
      }
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
        : (formingScope ? undercutFaces : featureFaces).has(faceIndex)
          ? 2
          : !formingScope && pickedFaceIndex === faceIndex && selectedFaceIndices == null
            ? 3
            : 0
    }
  }, [featureFaces, originalFeatureFaces, pickedFaceIndex, problemFaces, selectedFaceIndices, formingScope, undercutFaces, document])

  useEffect(() => {
    const resources = resourcesRef.current
    if (!resources) {return}
    if (resources.formingOutline) {
      resources.modelGroup.remove(resources.formingOutline)
      resources.formingOutline.geometry.dispose()
      const material = resources.formingOutline.material
      if (!Array.isArray(material)) {material.dispose()}
      resources.formingOutline = null
    }
    if (!formingFaces.size) {return}
    const positions = resources.geometry.getAttribute('position')
    const indices = resources.geometry.getIndex()!
    const points: number[] = []
    for (const face of formingFaces) {
      const groupIndex = resources.faceGroups.get(face)
      if (groupIndex === undefined) {continue}
      const group = resources.geometry.groups[groupIndex]
      for (let i = group.start; i < group.start + group.count; i++) {
        const vertex = indices.getX(i)
        points.push(positions.getX(vertex), positions.getY(vertex), positions.getZ(vertex))
      }
    }
    const surface = new BufferGeometry()
    surface.setAttribute('position', new Float32BufferAttribute(points, 3))
    const outline = new LineSegments(new EdgesGeometry(surface, 24), new LineBasicMaterial({ color: 0x47e4f0 }))
    surface.dispose()
    outline.name = 'forming-scope-outline'
    outline.renderOrder = 4
    resources.modelGroup.add(outline)
    resources.formingOutline = outline
  }, [formingFaces, document])

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

  return <div className="relative h-full min-h-0 w-full">
    <div aria-label="DFM 三维画布" className="h-full min-h-0 w-full" data-forming-faces={formingScope ? JSON.stringify([...formingFaces]) : undefined} data-undercut-faces={formingScope ? JSON.stringify([...undercutFaces]) : undefined} data-direction-origin={directionAxis?.origin ? JSON.stringify(directionAxis.origin) : undefined} data-direction-scope={directionAxis?.scopeId} data-direction-vector={directionAxis?.direction ? JSON.stringify(directionAxis.direction) : undefined} ref={hostRef} />
    <DfmViewCompass className="absolute bottom-12 left-4" ref={compassRef} />
    {viewMenu && <DfmViewMenu {...viewMenu} onCancel={() => resourcesRef.current?.cancelMenu()} onCommand={action => resourcesRef.current?.command(action)} />}
  </div>
}
