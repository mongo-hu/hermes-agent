import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'

export interface IntroProps {
  onStartAnalysis?: () => void
  /** Kept in the public shape because the session store still supplies it. */
  personality?: string
  /** Kept in the public shape because the session store still supplies it. */
  seed?: number
}

const STEPS = [
  { icon: 'file-binary', label: '导入 3D 模型', detail: '仅支持 STP 三维模型文件' },
  { icon: 'settings', label: '补充工艺参数', detail: '确认材料、工艺与关键约束' },
  { icon: 'checklist', label: '审阅并交付', detail: '定位问题、确认建议、导出报告' }
] as const

export function Intro({ onStartAnalysis }: IntroProps) {
  return (
    <div
      className="flex w-full min-w-0 flex-col items-center justify-center px-5 py-8 text-center sm:px-8"
      data-slot="aui_intro"
    >
      <div className="w-full max-w-3xl rounded-2xl border border-(--ui-stroke-secondary) bg-(--ui-sidebar-surface-background)/72 px-6 py-7 text-left shadow-sm backdrop-blur-sm sm:px-8">
        <div className="mb-6 flex items-start gap-4">
          <div className="grid size-11 shrink-0 place-items-center rounded-xl bg-primary text-primary-foreground shadow-sm">
            <Codicon name="symbol-structure" size="1.35rem" />
          </div>
          <div className="min-w-0">
            <p className="mb-1 text-xs font-semibold uppercase tracking-[0.18em] text-primary">DFM</p>
            <h1 className="text-2xl font-semibold tracking-tight text-foreground">新建 DFM 分析</h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-(--ui-text-secondary)">
              导入零件三维模型，自动完成可制造性分析，并在同一工作台中审阅特征、澄清项和风险问题。
            </p>
          </div>
        </div>

        <div className="mb-7 grid gap-3 sm:grid-cols-3">
          {STEPS.map((step, index) => (
            <div
              className="rounded-xl border border-(--ui-stroke-tertiary) bg-(--ui-editor-surface-background)/70 p-4"
              key={step.label}
            >
              <div className="mb-3 flex items-center gap-2 text-sm font-medium text-foreground">
                <span className="grid size-6 place-items-center rounded-full bg-primary/10 text-[0.6875rem] font-semibold text-primary">
                  {index + 1}
                </span>
                <Codicon className="text-primary" name={step.icon} size="0.95rem" />
                <span>{step.label}</span>
              </div>
              <p className="text-xs leading-5 text-(--ui-text-tertiary)">{step.detail}</p>
            </div>
          ))}
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-(--ui-stroke-tertiary) pt-5">
          <p className="text-xs text-(--ui-text-tertiary)">也可以将 .stp 模型直接拖入窗口，或在下方输入补充说明。</p>
          <Button className="gap-2" onClick={onStartAnalysis} type="button">
            <Codicon name="folder-opened" size="0.95rem" />
            选择 STP 模型并开始
          </Button>
        </div>
      </div>
    </div>
  )
}
