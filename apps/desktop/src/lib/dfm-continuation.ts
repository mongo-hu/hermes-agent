import type { ReviewContext } from '@/app/dfm-viewer/dfm-review'
import { type ClarifyRequest, clearClarifyRequest } from '@/store/clarify'

export interface DfmContinuationInput {
  project_id: string
  snapshot_id: string
  expected_revision: number
  action_key: string
  expected_session_key: string
  manual_resume?: boolean
}

export const DFM_CONTINUATION_TEXT = '特征与出模审核已完成，请打开尚未填写的补充信息，并继续本次 DFM 分析。已有运行时继续跟踪原运行，已完成时仅展示结果。'

export function reviewReadyForContinuation(context: ReviewContext): boolean {
  return Boolean(context.project_id && context.snapshot_id && context.review_groups.length &&
    context.review_groups.every(group => group.status === 'confirmed'))
}

/** Called only after a successful user save, or an explicit Continue click. */
export async function continueDfmReview({ context, sessionKey, runtimeSessionId, request, respond, submit, manual = false }: {
  context: ReviewContext
  sessionKey: string
  runtimeSessionId: string | null
  request: ClarifyRequest | null
  respond: (params: Record<string, unknown>) => Promise<{ ok?: boolean }>
  submit: (input: DfmContinuationInput) => Promise<boolean>
  manual?: boolean
}): Promise<void> {
  if (!reviewReadyForContinuation(context)) {return}
  const waiting = request?.context
  if (request && request.sessionId === runtimeSessionId && waiting?.mode === 'dfm_discovery_review' &&
    waiting.projectId === context.project_id &&
    context.revision >= waiting.revision) {
    const result = await respond({
      request_id: request.requestId, session_id: request.sessionId,
      answer: '确认最终特征区域并继续',
      dfm_context: { project_id: context.project_id, snapshot_id: context.snapshot_id, expected_revision: context.revision }
    })
    if (result?.ok === false) {throw new Error('审核已保存，继续分析未成功，请重试。')}
    clearClarifyRequest(request.requestId, request.sessionId)
    return
  }
  // A different pending question must never be answered or replaced by this save.
  if (request) {throw new Error('当前会话已有待处理的问题，请先完成它。')}
  if (!sessionKey) {throw new Error('原会话尚未连接，请重试继续。')}
  const accepted = await submit({
    project_id: context.project_id, snapshot_id: context.snapshot_id!, expected_revision: context.revision,
    action_key: `dfm-review:${context.project_id}:${context.snapshot_id}:${context.revision}`,
    expected_session_key: sessionKey, manual_resume: manual
  })
  if (!accepted) {throw new Error('审核已保存，当前尚不能继续。请稍后重试继续。')}
}
