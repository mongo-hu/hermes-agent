import type { ReactNode } from 'react'

import { Codicon } from '@/components/ui/codicon'
import type { DfmViewerTarget } from '@/store/dfm-viewer'

import { titlebarHeaderBaseClass } from '../shell/titlebar'

import { DfmViewerPane } from './dfm-viewer-pane'

const STATUS_COPY: Record<DfmViewerTarget['status'], string> = {
  completed: '分析完成',
  discovery: '特征审核',
  preview: '模型预览'
}

export function DfmWorkbench({ analysisView, target }: { analysisView: ReactNode; target: DfmViewerTarget }) {
  return (
    <section className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)] overflow-hidden bg-(--ui-chat-surface-background)">
      <header className={titlebarHeaderBaseClass}>
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <div className="flex min-w-0 items-center gap-2">
            <span className="grid size-5 shrink-0 place-items-center rounded bg-primary/12 text-primary">
              <Codicon name="symbol-structure" size="0.75rem" />
            </span>
            <strong className="truncate text-[0.75rem] font-semibold text-foreground">DFM 分析工作台</strong>
            {(target.projectId || target.runId) && (
              <span className="hidden max-w-52 truncate text-[0.6875rem] text-(--ui-text-tertiary) xl:inline">
                {target.projectId || target.runId}
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
            title={target.failure?.message}
          >
            <span
              className={`size-1.5 rounded-full ${
                target.failure
                  ? 'bg-red-500'
                  : target.status === 'completed'
                    ? 'bg-emerald-500'
                    : target.status === 'discovery'
                      ? 'bg-amber-500'
                      : 'bg-sky-500'
              }`}
            />
            {target.failure ? '分析未完成' : STATUS_COPY[target.status]}
          </span>
        </div>
      </header>

      <div className="grid min-h-0 grid-rows-[minmax(14rem,0.8fr)_minmax(20rem,1.2fr)] overflow-hidden lg:grid-cols-[minmax(18rem,0.72fr)_minmax(34rem,1.45fr)] lg:grid-rows-1">
        <section
          aria-label="分析过程"
          className="min-h-0 overflow-hidden border-b border-(--ui-stroke-tertiary) lg:border-b-0 lg:border-r"
        >
          {analysisView}
        </section>
        <section aria-label="三维模型与问题" className="min-h-0 overflow-hidden">
          <DfmViewerPane target={target} />
        </section>
      </div>
    </section>
  )
}
