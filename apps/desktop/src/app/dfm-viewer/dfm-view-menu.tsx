import './dfm-view-menu.css'

import { IconAxisX, IconMaximize } from '@tabler/icons-react'
import { type CSSProperties, forwardRef, type Ref, useImperativeHandle, useRef } from 'react'

import {
  compassCubeTransform,
  type DfmViewCommand,
  VIEW_CANCEL_RADIUS,
  VIEW_COMMAND_SECTORS,
  VIEW_MENU_RADIUS,
  type ViewPoint,
  type ViewQuaternion
} from './dfm-view-gestures'

export type { DfmViewCommand } from './dfm-view-gestures'

const CUBE_FACES = [
  ['front', '−Y'], ['back', '+Y'], ['top', '+Z'],
  ['bottom', '−Z'], ['left', '−X'], ['right', '+X']
] as const

type CubeFace = typeof CUBE_FACES[number][0]

function ViewCube({ face, transform, labels = false, cubeRef }: {
  face?: CubeFace
  transform?: string
  labels?: boolean
  cubeRef?: Ref<HTMLSpanElement>
}) {
  return <span aria-hidden="true" className="dfm-view-cube-space" data-face={face}>
    <span className="dfm-view-cube" ref={cubeRef} style={transform ? { transform } : undefined}>
      {CUBE_FACES.map(([side, label]) =>
        <span className={`dfm-view-cube-face dfm-view-cube-${side}`} key={side}>
          {labels ? label : null}
        </span>
      )}
    </span>
  </span>
}

export interface DfmViewMenuProps {
  /** Center in the positioned viewer host's pixel coordinate space. */
  position: ViewPoint
  radius?: number
  activeCommand: DfmViewCommand | null
  needsCentering?: boolean
  onCommand: (command: DfmViewCommand) => void
  onCancel: () => void
}

function radialPoint(angle: number, radius: number) {
  const radians = angle * Math.PI / 180

  return { x: 50 + Math.sin(radians) * radius, y: 50 - Math.cos(radians) * radius }
}

/** Rendering only: the host owns pointer capture, drag threshold and cancellation. */
export function DfmViewMenu({ position, radius = VIEW_MENU_RADIUS, activeCommand, needsCentering = false, onCommand, onCancel }: DfmViewMenuProps) {
  const style = {
    left: position.x,
    top: position.y,
    width: radius * 2,
    height: radius * 2,
    '--dfm-menu-scale': radius / VIEW_MENU_RADIUS
  } as CSSProperties

  return <div aria-label="视图方向菜单" className="dfm-view-menu dfm-view-menu-theme"
    onContextMenu={event => event.preventDefault()} onKeyDown={event => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        onCancel()
      }
    }}
    role="group"
    style={style}>
    {VIEW_COMMAND_SECTORS.map(sector => {
      // Far endpoints make each clipped button cover the complete circular arc.
      const a = radialPoint(sector.start, 100)
      const b = radialPoint(sector.end, 100)
      const label = radialPoint((sector.start + sector.end) / 2, 33)

      return <button aria-label={sector.label} aria-pressed={activeCommand === sector.command} className="dfm-view-sector"
        data-view-command={sector.command} key={sector.command}
        onClick={event => { if (event.button === 0) {onCommand(sector.command)} }}
        style={{ clipPath: `polygon(50% 50%, ${a.x}% ${a.y}%, ${b.x}% ${b.y}%)` }}
        type="button">
        <span className="dfm-view-sector-label" style={{ left: `${label.x}%`, top: `${label.y}%` }}>
          {'face' in sector ? <ViewCube face={sector.face} /> :
            <span className="dfm-view-tool-symbol">
              {sector.command === 'align' ? <IconAxisX aria-hidden="true" size={23} /> : <IconMaximize aria-hidden="true" size={23} />}
            </span>}
          <span>{sector.label}</span>
        </span>
      </button>
    })}
    <button aria-label="取消视图手势" className="dfm-view-menu-cancel"
      onClick={event => { if (event.button === 0) {onCancel()} }}
      style={{ width: VIEW_CANCEL_RADIUS * 2, height: VIEW_CANCEL_RADIUS * 2 }} type="button">
      <span>{needsCentering ? '移入中心' : '松开取消'}</span><span className="dfm-view-menu-hint">Esc</span>
    </button>
  </div>
}

export interface DfmViewCompassProps {
  cameraQuaternion?: ViewQuaternion
  className?: string
}

export interface DfmViewCompassHandle {
  setCameraQuaternion: (quaternion: ViewQuaternion) => void
}

/** A passive orientation cue; it has no view-switching interaction. */
export const DfmViewCompass = forwardRef<DfmViewCompassHandle, DfmViewCompassProps>(function DfmViewCompass(
  { cameraQuaternion, className = '' }, ref
) {
  const cubeRef = useRef<HTMLSpanElement>(null)

  useImperativeHandle(ref, () => ({
    setCameraQuaternion: quaternion => {
      if (cubeRef.current) {
        cubeRef.current.style.transform = compassCubeTransform(quaternion)
      }
    }
  }), [])

  return <span aria-label="三维方位提示"
    className={`dfm-view-compass dfm-view-menu-theme ${className}`} role="img" title="当前模型方位">
    <ViewCube cubeRef={cubeRef} labels transform={cameraQuaternion ? compassCubeTransform(cameraQuaternion) : undefined} />
  </span>
})
