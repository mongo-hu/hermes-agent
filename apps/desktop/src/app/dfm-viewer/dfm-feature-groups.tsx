import { Fragment, type ReactNode, type RefObject, useEffect, useMemo, useState } from 'react'

interface FeatureIdentity {
  feature_id: string
  kind: string
}

interface DfmFeatureGroupsProps<T extends FeatureIdentity> {
  features: readonly T[]
  labels: Readonly<Record<string, string>>
  activeFeatureId: string | null
  editingFeatureId: string | null
  activeFeatureElementRef: RefObject<HTMLDivElement | null>
  renderFeature: (feature: T, index: number) => ReactNode
}

export function DfmFeatureGroups<T extends FeatureIdentity>({
  features,
  labels,
  activeFeatureId,
  editingFeatureId,
  activeFeatureElementRef,
  renderFeature
}: DfmFeatureGroupsProps<T>) {
  const [openKinds, setOpenKinds] = useState<Set<string>>(new Set())

  const groups = useMemo(() => {
    const byKind = new Map<string, Array<{ feature: T; index: number }>>()
    features.forEach((feature, index) => {
      const entries = byKind.get(feature.kind) ?? []
      entries.push({ feature, index })
      byKind.set(feature.kind, entries)
    })

    return [...byKind.entries()]
  }, [features])

  const activeKind = features.find(feature => feature.feature_id === activeFeatureId)?.kind
  const editingKind = features.find(feature => feature.feature_id === editingFeatureId)?.kind
  const activeKindOpen = activeKind !== undefined && openKinds.has(activeKind)

  useEffect(() => {
    setOpenKinds(previous => {
      const required = [activeKind, editingKind].filter((kind): kind is string => kind !== undefined)

      if (required.every(kind => previous.has(kind))) {return previous}

      return new Set([...previous, ...required])
    })
  }, [activeFeatureId, editingFeatureId, activeKind, editingKind])

  useEffect(() => {
    if (activeKindOpen && !editingFeatureId) {
      activeFeatureElementRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  }, [activeFeatureId, activeKindOpen, activeFeatureElementRef, editingFeatureId])

  return (
    <div className="grid gap-2">
      <h2 className="text-[11px] font-medium text-slate-300">已确认特征 · {features.length} 个实例</h2>
      {!features.length && <p className="text-[10px] text-slate-400">确认前结果为空。请在上方定位、编辑并确认候选特征。</p>}
      {groups.map(([kind, entries]) => (
        <details
          className="overflow-hidden rounded-lg border border-white/10 bg-white/[0.025]"
          key={kind}
          onToggle={event => {
            const open = event.currentTarget.open
            setOpenKinds(previous => {
              if (previous.has(kind) === open) {return previous}
              const next = new Set(previous)

              if (open) {next.add(kind)}
              else {next.delete(kind)}

              return next
            })
          }}
          open={openKinds.has(kind)}
        >
          <summary className="cursor-pointer px-2.5 py-2 text-[11px] font-medium text-slate-100">
            {labels[kind] ?? kind} · {entries.length} 个实例
          </summary>
          {openKinds.has(kind) && (
            <div className="grid gap-2 border-t border-white/10 p-2">
              {entries.map(({ feature, index }) => (
                <Fragment key={feature.feature_id}>{renderFeature(feature, index)}</Fragment>
              ))}
            </div>
          )}
        </details>
      ))}
    </div>
  )
}
