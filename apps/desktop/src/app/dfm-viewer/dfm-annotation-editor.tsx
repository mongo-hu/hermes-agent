interface AnnotationEditorProps {
  identity: string
  value: string
  values: string[]
  faceCount: number
  busy: boolean
  saveBlocked?: boolean
  onValue: (value: string) => void
  onSave: () => void
  onCancel: () => void
}

export function DfmAnnotationEditor({
  identity,
  value,
  values,
  faceCount,
  busy,
  saveBlocked = false,
  onValue,
  onSave,
  onCancel
}: AnnotationEditorProps) {
  return (
    <div className="mb-2 rounded border border-cyan-300/30 p-2 text-[11px] text-cyan-100">
      <p>
        编辑面属性 {identity} · 已选 {faceCount} 个面
      </p>
      <p className="mt-1 text-[10px] text-slate-400">单击模型调整选面；保存后只需重新确认这项属性。</p>
      <label className="mt-2 block">
        属性值
        <select
          aria-label="面属性值"
          className="ml-2 rounded bg-slate-800 p-1"
          disabled={busy}
          onChange={event => onValue(event.target.value)}
          value={value}
        >
          {values.map(item => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
      </label>
      <button
        className="mr-3 mt-2"
        disabled={busy || saveBlocked || faceCount === 0 || !values.includes(value)}
        onClick={onSave}
        type="button"
      >
        保存属性
      </button>
      <button disabled={busy} onClick={onCancel} type="button">
        取消
      </button>
    </div>
  )
}
