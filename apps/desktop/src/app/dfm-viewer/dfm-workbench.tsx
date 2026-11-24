import { useStore } from '@nanostores/react'
import { type ReactNode, useEffect, useState } from 'react'

import { Codicon } from '@/components/ui/codicon'
import type { DfmContinuationInput } from '@/lib/dfm-continuation'
import type { DfmViewerTarget } from '@/store/dfm-viewer'
import { notifyError } from '@/store/notifications'
import { $dfmReportRecord, $previewTarget, setPreviewTarget } from '@/store/preview'

import { PreviewPane } from '../chat/right-rail/preview-pane'
import { titlebarHeaderBaseClass } from '../shell/titlebar'

import { DfmViewerPane } from './dfm-viewer-pane'

const STATUS_COPY: Record<DfmViewerTarget['status'], string> = {
  completed: '分析完成',
  discovery: '特征审核',
  preview: '模型预览'
}

interface DfmWorkbenchProps {
  analysisView: ReactNode
  onContinueAnalysis?: (options: DfmContinuationInput) => Promise<boolean>
  target: DfmViewerTarget | null
}

export function DfmWorkbench({ analysisView, onContinueAnalysis, target }: DfmWorkbenchProps) {
  const record = useStore($dfmReportRecord)
  const preview = useStore($previewTarget)
  const identity = record?.normalized.dfmReport
  const report =
    identity &&
    (!target || (target.projectId === identity.projectId && (!target.runId || target.runId === identity.runId)))
      ? record.normalized
      : null
  const reportKey = report ? `${identity?.sessionId}:${identity?.projectId}:${identity?.runId}:${report.url}` : ''
  const showingReport = Boolean(
    report &&
    preview &&
    preview.dfmReport &&
    preview.dfmReport.sessionId === identity?.sessionId &&
    preview.dfmReport.projectId === identity?.projectId &&
    preview.dfmReport.runId === identity?.runId &&
    preview.url === report.url
  )
  const [openedReportKey, setOpenedReportKey] = useState('')
  const [reloadRequest, setReloadRequest] = useState(0)
  const [reportSideBySide, setReportSideBySide] = useState(false)
  const expandedReading = showingReport && !reportSideBySide

  useEffect(() => setReportSideBySide(false), [reportKey])

  useEffect(() => {
    if (showingReport) setOpenedReportKey(reportKey)
  }, [reportKey, showingReport])

  async function openReportExternally() {
    if (!report) return

    try {
      if (!window.hermesDesktop?.openPreviewInBrowser) throw new Error('桌面浏览器入口不可用。')
      await window.hermesDesktop.openPreviewInBrowser(report.url)
    } catch (error) {
      notifyError(error, '无法打开报告')
    }
  }

  return (
    <section className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)] overflow-hidden bg-(--ui-chat-surface-background)">
      <header className={titlebarHeaderBaseClass}>
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <div className="flex min-w-0 items-center gap-2">
            <span className="grid size-5 shrink-0 place-items-center rounded bg-primary/12 text-primary">
              <Codicon name="symbol-structure" size="0.75rem" />
            </span>
            <strong className="truncate text-[0.75rem] font-semibold text-foreground">DFM 分析工作台</strong>
            {(target?.projectId || target?.runId || identity?.projectId) && (
              <span className="hidden max-w-52 truncate text-[0.6875rem] text-(--ui-text-tertiary) xl:inline">
                {target?.projectId || target?.runId || identity?.projectId}
              </span>
            )}
          </div>

          <div className="ml-auto hidden items-center gap-3 text-[0.6875rem] text-(--ui-text-tertiary) lg:flex">
            <span className="inline-flex items-center gap-1.5">
              <Codicon name="comment-discussion" size="0.75rem" />
              分析过程
            </span>
            <span className="h-3 w-px bg-(--ui-stroke-tertiary)" />
            <span className="inline-flex items-center gap-1.5">
              <Codicon name="symbol-structure" size="0.75rem" />
              三维模型与问题
            </span>
          </div>

          <span
            aria-label="DFM 分析状态"
            className="inline-flex shrink-0 items-center gap-1.5 text-[0.6875rem] text-(--ui-text-secondary)"
            role="status"
            title={target?.failure?.message}
          >
            <span
              className={`size-1.5 rounded-full ${
                target?.failure
                  ? 'bg-red-500'
                  : !target || target.status === 'completed'
                    ? 'bg-emerald-500'
                    : target.status === 'discovery'
                      ? 'bg-amber-500'
                      : 'bg-sky-500'
              }`}
            />
            {target?.failure ? '分析未完成' : target ? STATUS_COPY[target.status] : '报告已生成'}
          </span>
        </div>
      </header>

      <div className={`grid min-h-0 overflow-hidden ${expandedReading
        ? 'grid-cols-1 grid-rows-1'
        : 'grid-rows-[minmax(14rem,0.8fr)_minmax(20rem,1.2fr)] lg:grid-cols-[minmax(18rem,0.72fr)_minmax(34rem,1.45fr)] lg:grid-rows-1'}`}>
        <section
          aria-label="分析过程"
          className="min-h-0 overflow-hidden border-b border-(--ui-stroke-tertiary) lg:border-b-0 lg:border-r"
          hidden={expandedReading}
        >
          {analysisView}
        </section>
        <section aria-label="三维模型与问题" className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)] overflow-hidden">
          <div className="flex flex-wrap items-center gap-2 border-b border-(--ui-stroke-tertiary) px-3 py-2">
            <div aria-label="分析结果视图" className="flex gap-2" role="tablist">
              <button
                className="rounded px-3 py-1 text-sm aria-selected:bg-primary/12 aria-selected:text-primary disabled:opacity-50"
                aria-selected={!showingReport}
                disabled={!target}
                onClick={() => setPreviewTarget(null)}
                role="tab"
                type="button"
              >
                模型
              </button>
              <button
                className="rounded px-3 py-1 text-sm aria-selected:bg-primary/12 aria-selected:text-primary disabled:opacity-50"
                aria-selected={showingReport}
                disabled={!report}
                onClick={() => report && setPreviewTarget(report)}
                role="tab"
                type="button"
              >
                报告
              </button>
            </div>
            {report && !showingReport && (
              <button className="ml-auto text-sm text-primary" onClick={() => setPreviewTarget(report)} type="button">
                查看报告
              </button>
            )}
            {showingReport && (
              <>
                <button
                  aria-pressed={expandedReading}
                  className="ml-auto text-sm text-primary"
                  onClick={() => setReportSideBySide(value => !value)}
                  type="button"
                >
                  {expandedReading ? '并排查看' : '展开阅读'}
                </button>
                <button className="text-sm" onClick={() => setReloadRequest(value => value + 1)} type="button">
                  重新加载报告
                </button>
                <button className="text-sm" onClick={() => void openReportExternally()} type="button">
                  在浏览器打开
                </button>
                <span className="basis-full text-xs text-muted-foreground">
                  文字较小时，可用报告右下角的 ＋／－ 或 Ctrl＋滚轮缩放。放映请先在浏览器打开。
                </span>
              </>
            )}
          </div>
          <div className="relative min-h-0 overflow-hidden">
            <div className="h-full min-h-0" hidden={showingReport}>
              {target && <DfmViewerPane onContinueAnalysis={onContinueAnalysis} showPopout target={target} />}
              {!target && <p className="p-4 text-sm text-muted-foreground">报告已生成，点击“查看报告”打开。</p>}
            </div>
            {report && (showingReport || openedReportKey === reportKey) && (
              <div className="absolute inset-0" hidden={!showingReport}>
                <PreviewPane embedded reloadRequest={reloadRequest} target={report} />
              </div>
            )}
          </div>
        </section>
      </div>
    </section>
  )
}
