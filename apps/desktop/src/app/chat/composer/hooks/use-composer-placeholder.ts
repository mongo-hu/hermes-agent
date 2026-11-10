import { useI18n } from '@/i18n'

interface UseComposerPlaceholderOptions {
  disabled: boolean
  reconnecting: boolean
  sessionId: null | string | undefined
}

/** Product-specific guidance for the focused DFM analysis composer. */
export function useComposerPlaceholder({ disabled, reconnecting, sessionId }: UseComposerPlaceholderOptions): string {
  const { t } = useI18n()

  if (disabled) {
    return reconnecting ? t.composer.placeholderReconnecting : t.composer.placeholderStarting
  }

  return sessionId ? '补充参数、提出修改意见或继续审阅 DFM 结果…' : '描述分析要求，或点击“+”添加 STP 模型…'
}
