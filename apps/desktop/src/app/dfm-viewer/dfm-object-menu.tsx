import { useEffect, useRef } from 'react'

import type { CanvasMenuPoint } from './dfm-canvas-menus'
import type { FaceCandidate } from './dfm-face-navigation'

export interface ObjectMenuItem {
  target: FaceCandidate
  label: string
  status: string
  removable: boolean
}

export function DfmObjectMenu({ point, items, selected, busy, onSelect, onClear, onRemove, onClose }: {
  point: CanvasMenuPoint
  items: ObjectMenuItem[]
  selected: ObjectMenuItem | null
  busy: boolean
  onSelect(item: ObjectMenuItem): void
  onClear(): void
  onRemove(item: ObjectMenuItem): void
  onClose(): void
}) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const menu = ref.current

    if (!menu) {return}
    const parent = menu.parentElement!.getBoundingClientRect()
    menu.style.left = `${Math.max(8, Math.min(point.x, parent.width - menu.offsetWidth - 8))}px`
    menu.style.top = `${Math.max(8, Math.min(point.y, parent.height - menu.offsetHeight - 8))}px`
    menu.querySelector<HTMLButtonElement>('button')?.focus()

    const outside = (event: PointerEvent) => { if (!menu.contains(event.target as Node)) {onClose()} }

    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') {event.stopPropagation(); onClose()} }
    window.addEventListener('pointerdown', outside)
    window.addEventListener('keydown', escape, true)
    window.addEventListener('blur', onClose)

    return () => {
      window.removeEventListener('pointerdown', outside)
      window.removeEventListener('keydown', escape, true)
      window.removeEventListener('blur', onClose)
    }
  }, [point, onClose, selected])

  return <div aria-label="审核对象菜单" className="absolute z-30 w-[228px] max-h-[80%] overflow-auto rounded-[10px] border border-slate-500/60 bg-[#202b37] p-1.5 text-xs text-slate-100 shadow-2xl" onContextMenu={event => event.preventDefault()}
    ref={ref}
    role="menu" style={{ left: point.x, top: point.y }}>
    {selected ? <>
      <div className="border-b border-white/10 px-2 py-2">
        <p className="break-words font-medium">{selected.label}</p>
        <p className="mt-1 text-[10px] text-slate-400">{selected.status}</p>
      </div>
      <button className="w-full rounded px-2 py-2 text-left hover:bg-white/10 disabled:opacity-40" disabled={busy}
        onClick={onClear} role="menuitem" type="button">取消高亮</button>
      {selected.removable && <button className="w-full rounded px-2 py-2 text-left text-red-300 hover:bg-red-400/10 disabled:opacity-40" disabled={busy}
        onClick={() => onRemove(selected)} role="menuitem" type="button">删除识别项<span className="mt-0.5 block text-[10px] text-slate-400">模型形状保留</span></button>}
    </> : <>
      <p className="border-b border-white/10 px-2 py-2 text-slate-400">该面属于多个审核对象</p>
      {items.map(item => <button className="w-full rounded px-2 py-2 text-left hover:bg-white/10 disabled:opacity-40" disabled={busy}
        key={`${item.target.category}:${item.target.objectId}`} onClick={() => onSelect(item)}
        role="menuitem" type="button">{item.label}<span className="block text-[10px] text-slate-400">{item.status}</span></button>)}
    </>}
  </div>
}
