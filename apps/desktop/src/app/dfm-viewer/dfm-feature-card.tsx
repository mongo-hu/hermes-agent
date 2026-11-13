import type { ReactNode, Ref } from 'react'

import { featurePresentation, type FeaturePresentationInput } from './dfm-feature-presentation'

export interface DfmFeatureCardProps {
  feature: FeaturePresentationInput
  featureNumber: number | string
  confirmed: boolean
  selected?: boolean
  disabled?: boolean
  selection?: ReactNode
  actions?: ReactNode
  notice?: ReactNode
  editLabel?: string
  onSelect?: () => void
  onLocate?: () => void
  onEdit?: () => void
  onConfirm?: () => void
  onDelete?: () => void
  elementRef?: Ref<HTMLDivElement>
}

const actionClass = 'rounded bg-white/5 px-2 py-1 text-[10px] text-slate-200 hover:bg-white/10 disabled:opacity-40'

export function DfmFeatureCard({
  feature, featureNumber, confirmed, selected = false, disabled = false,
  selection, actions, notice, editLabel = '编辑选面',
  onSelect, onLocate, onEdit, onConfirm, onDelete, elementRef
}: DfmFeatureCardProps) {
  const presentation = featurePresentation(feature)
  const title = `${presentation.label} #${featureNumber}`
  return (
    <div
      className={'min-w-0 w-full rounded-lg border p-2 text-left ' + (confirmed ? '' : 'mt-2 ') + (selected
        ? 'border-amber-400/70 bg-amber-500/10'
        : 'border-white/10 bg-white/[0.035]')}
      data-review-object={feature.feature_id}
      ref={elementRef}
    >
      <div className="flex min-w-0 items-center gap-1.5">
        {selection && <span className="flex shrink-0 items-center">{selection}</span>}
        <button
          aria-label={title}
          aria-pressed={selected}
          className="min-w-0 flex-1 truncate text-left text-xs font-medium text-slate-100 disabled:opacity-50"
          disabled={disabled || !onSelect}
          onClick={onSelect}
          title={feature.feature_id}
          type="button"
        >
          {title}
        </button>
        <span className={'shrink-0 rounded px-1.5 py-0.5 text-[9px] ' + (confirmed
          ? 'bg-emerald-500/10 text-emerald-200' : 'bg-amber-500/10 text-amber-200')}>
          {confirmed ? '已确认' : '待确认'}
        </span>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-slate-400">
        <span title="识别置信度仅供特征识别参考，不代表 DFM 合格率；人工确认不改变原始评分。">
          识别置信度 <span className="text-slate-200">{presentation.confidence.label}</span>
        </span>
        <span>关联面 {presentation.faceRefs.length} 个</span>
      </div>
      {notice && <div className="mt-1 break-words text-[10px] text-amber-200">{notice}</div>}
      {(onLocate || onEdit || (onConfirm && !confirmed) || actions) && (
        <div className="mt-2 flex flex-wrap items-center gap-1">
          {onLocate && <button className={actionClass} disabled={disabled} onClick={onLocate} type="button">定位</button>}
          {onEdit && <button className={actionClass} disabled={disabled} onClick={onEdit} type="button">{editLabel}</button>}
          {onConfirm && !confirmed && <button className={actionClass} disabled={disabled} onClick={onConfirm} type="button">确认</button>}
          {actions}
        </div>
      )}
      <details className="mt-1.5 min-w-0 text-[10px] text-slate-400">
        <summary className="cursor-pointer py-0.5">详情</summary>
        <dl className="mt-1 grid min-w-0 gap-1 break-words border-t border-white/5 pt-1.5">
          <div><dt className="inline text-slate-500">子类型：</dt><dd className="inline">{presentation.subtype}</dd></div>
          <div><dt className="inline text-slate-500">参数：</dt><dd className="inline">{presentation.parameters}</dd></div>
          <div><dt className="inline text-slate-500">完整 ID：</dt><dd className="inline break-all">{feature.feature_id}</dd></div>
          <div><dt className="inline text-slate-500">关联面：</dt><dd className="inline break-all">{
            presentation.faceRefs.map(ref => `face #${ref.index}`).join('、') || '无拓扑引用'
          }</dd></div>
        </dl>
        {onDelete && <div className="mt-2 flex flex-wrap gap-1">
          <button className={actionClass} disabled={disabled} onClick={onDelete} type="button">删除特征</button>
        </div>}
      </details>
    </div>
  )
}
