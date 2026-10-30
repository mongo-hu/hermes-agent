import { useEffect, useRef } from 'react'

interface FeatureEditorProps {
  identity: string
  label: string
  faceCount: number
  addedCount: number
  removedCount: number
  busy: boolean
  saveBlocked?: boolean
  onSave: () => void
  onCancel: () => void
  onRestore: () => void
}

export function DfmFeatureEditor({
  identity,
  label,
  faceCount,
  addedCount,
  removedCount,
  busy,
  saveBlocked = false,
  onSave,
  onCancel,
  onRestore
}: FeatureEditorProps) {
  const editorRef = useRef<HTMLElement>(null)
  useEffect(() => {
    editorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [identity])

  return (
    <section
      aria-label="编辑特征选面"
      className="mb-2 rounded-lg border border-amber-300/40 bg-amber-500/10 p-2.5 text-[11px] text-amber-100"
      ref={editorRef}
    >
      <h2 className="font-medium">
        编辑{label} · {identity}
      </h2>
      <p className="mt-1">
        已选 {faceCount} 个面 · 新增 {addedCount} · 移除 {removedCount}
      </p>
      <ol className="mt-2 list-inside list-decimal space-y-1 text-[10px] text-slate-300">
        <li>黄色区域是当前选面；单击未选面添加，单击黄色面移除。</li>
        <li>按住左键拖动可旋转模型，右键拖动平移，滚轮缩放。</li>
        <li>选面有变化时，保存后返回待确认；确认该特征后进入已确认结果。</li>
      </ol>
      <p className="mt-1 text-[10px] text-slate-400">这里修改特征包含的面，不改变模型形状。</p>
      {faceCount === 0 && (
        <p className="mt-1 text-amber-200" role="alert">
          至少保留一个面后才能保存。
        </p>
      )}
      <div className="mt-2 flex flex-wrap gap-3">
        <button
          className="text-amber-50 disabled:opacity-40"
          disabled={busy || saveBlocked || faceCount === 0}
          onClick={onSave}
          type="button"
        >
          {busy ? '保存中…' : '保存选面'}
        </button>
        <button className="text-slate-300" disabled={busy} onClick={onRestore} type="button">
          恢复原选面
        </button>
        <button className="text-slate-300" disabled={busy} onClick={onCancel} type="button">
          取消编辑
        </button>
      </div>
    </section>
  )
}
