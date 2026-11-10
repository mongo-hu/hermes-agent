export interface ManualReviewResult {
  result_id: string
  feature_id: string
  geometric_id: string
  reason: string
}

export function DfmManualReviewNotice({ results, onSelectFeature }: {
  results: ManualReviewResult[]
  onSelectFeature: (featureId: string) => void
}) {
  if (!results.length) return null
  return (
    <section aria-label="需人工复核" role="alert" className="mb-3 rounded-lg border border-orange-400/50 bg-orange-500/15 p-3 text-xs text-orange-100">
      <h3 className="mb-2 font-semibold">无法判定 · 需人工复核 {results.length} 项</h3>
      <p className="mb-2">以下测量未计为合格，也未作为不适用项跳过。</p>
      {results.map(item => (
        <button className="mb-1 block w-full text-left" key={item.result_id}
          onClick={() => onSelectFeature(item.feature_id)} type="button">
          {item.geometric_id}：{item.reason}
        </button>
      ))}
    </section>
  )
}
