import { notifyError } from '@/store/notifications'

export async function openDfmViewerWindow(manifestPath: string): Promise<void> {
  const target = manifestPath.trim()

  if (!target || typeof window === 'undefined') {
    return
  }

  try {
    const result = await window.hermesDesktop?.openDfmViewerWindow?.(target)

    if (!result?.ok) {
      throw new Error(result?.error || 'DFM viewer window is unavailable')
    }
  } catch (error) {
    notifyError(error, '无法打开 DFM 三维结果窗口')
  }
}
