import { parseDirectionVector, type ReviewScope } from './dfm-review'

export interface DirectionDraft {
  scope: ReviewScope
  revision: number
  snapshotId?: string
  text: string
  mechanismKind: ReviewScope['mechanism_kind']
  origin?: [number, number, number]
  mode: 'rotate' | 'translate'
}

interface DfmDirectionEditorProps {
  draft: DirectionDraft
  busy: boolean
  saveBlocked?: boolean
  error: string
  onChange: (draft: DirectionDraft) => void
  onCancel: () => void
  onSave: () => void
}

export function DfmDirectionEditor({ draft, busy, error, onChange, onCancel, onSave, saveBlocked = false }: DfmDirectionEditorProps) {
  const direction = parseDirectionVector(draft.text)
  const unknownDirection = draft.scope.mode === 'local' && draft.text.trim() === ''

  return (
    <section
      aria-label="编辑出模方向"
      className="mb-3 rounded-lg border border-amber-300/35 bg-amber-500/10 p-2 text-[11px]"
    >
      <h2 className="mb-2 font-medium text-amber-100">
        {draft.scope.mode === 'main' ? '主出模方向' : `${draft.scope.forming_scope_id} 局部成型方案`}
      </h2>
      {draft.scope.mode === 'local' && (
        <div className="mb-2 grid gap-2">
          <p className="text-slate-300">倒扣面 {draft.scope.undercut_geometry_refs.length} 个 · 成型面 {draft.scope.geometry_refs.length} 个</p>
          <label className="grid gap-1">
            成型机构
            <select aria-label="成型机构" className="rounded border border-white/15 bg-slate-900 p-1.5" disabled={busy}
              onChange={event => onChange({ ...draft, mechanismKind: event.target.value as ReviewScope['mechanism_kind'] })}
              value={draft.mechanismKind}>
              <option value="undetermined">待确定</option>
              <option value="slider">滑块</option>
              <option value="lifter">斜顶</option>
            </select>
          </label>
          {draft.scope.candidate_solutions.length > 0 && (
            <label className="grid gap-1">
              备选方案
              <select aria-label="备选成型方案" className="rounded bg-slate-900 p-1.5" disabled={busy} onChange={event => {
                  if (!event.target.value) {return}
                  const candidate = draft.scope.candidate_solutions[Number(event.target.value)]
                  onChange({ ...draft, mechanismKind: candidate.mechanism_kind,
                    text: candidate.draw_direction ? JSON.stringify(candidate.draw_direction) : '',
                    scope: { ...draft.scope, geometry_refs: candidate.geometry_refs } })
                }}
                value="">
                <option value="">选择并载入候选机构、范围和方向</option>
                {draft.scope.candidate_solutions.map((candidate, index) =>
                  <option key={index} value={index}>{index + 1} · {candidate.mechanism_kind === 'slider' ? '滑块' : candidate.mechanism_kind === 'lifter' ? '斜顶' : '待确定'}</option>)}
              </select>
            </label>
          )}
        </div>
      )}
      <label className="grid gap-1">
        方向向量（模型坐标）
        <input
          aria-label="方向向量"
          className="rounded border border-white/15 bg-black/25 p-1.5 font-mono"
          disabled={busy}
          onChange={event => onChange({ ...draft, text: event.target.value })}
          placeholder="[0,0,1]"
          value={draft.text}
        />
      </label>
      <div className="mt-2 flex flex-wrap gap-2">
        {['X', 'Y', 'Z'].map((axis, index) => (
          <button
            className="rounded bg-white/10 px-2 py-1"
            disabled={busy}
            key={axis}
            onClick={() => onChange({ ...draft, text: JSON.stringify([0, 0, 0].map((_, i) => (i === index ? 1 : 0))) })}
            type="button"
          >
            沿 {axis}
          </button>
        ))}
        {(['rotate', 'translate'] as const).map(mode => (
          <button
            aria-pressed={draft.mode === mode}
            className="rounded border border-white/15 px-2 py-1 aria-pressed:bg-amber-400/25"
            disabled={busy || !direction}
            key={mode}
            onClick={() => onChange({ ...draft, mode })}
            type="button"
          >
            {mode === 'rotate' ? '旋转轴线' : '平移轴线'}
          </button>
        ))}
        <button
          className="text-slate-300"
          disabled={busy}
          onClick={() => onChange({ ...draft, origin: undefined })}
          type="button"
        >
          轴线回到区域中心
        </button>
      </div>
      <p className="mt-2 text-[10px] leading-4 text-slate-300">
        选中三维轴线后拖动旋转环或平移手柄。平移只改变显示位置；方向向量用于计算。右键平移视图，滚轮缩放。
      </p>
      {draft.scope.mode === 'local' && (
        <p className="mt-1 text-amber-200">
          保存草稿后，请核对倒扣面、成型面、机构和方向，在列表中确认完整方案。
        </p>
      )}
      {unknownDirection
        ? <p className="mt-1 text-amber-200">方向待补充，可先保存方案草稿；补全后才能确认方案。</p>
        : !direction && <p className="mt-1 text-amber-200">请输入三个有限数值，例如 [0,0,1]，方向不能为零。</p>}
      {error && (
        <p className="mt-1 text-red-200" role="alert">
          {error}
        </p>
      )}
      <div className="mt-2 flex gap-3">
        <button
          className="rounded bg-amber-500/25 px-2 py-1 disabled:opacity-40"
          disabled={busy || saveBlocked || (!direction && !unknownDirection)}
          onClick={onSave}
          type="button"
        >
          {busy ? '保存中…' : draft.scope.mode === 'local' ? '保存方案草稿' : '保存主方向'}
        </button>
        <button disabled={busy} onClick={onCancel} type="button">
          取消方向编辑
        </button>
      </div>
    </section>
  )
}
