import { useStore } from '@nanostores/react'
import { type MouseEvent as ReactMouseEvent, useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { MonitorPlay } from '@/lib/icons'
import { normalizeOrLocalPreviewTarget } from '@/lib/local-preview'
import { previewName } from '@/lib/preview-targets'
import { notifyError } from '@/store/notifications'
import {
  $previewTarget,
  dismissPreviewTarget,
  type PreviewRecordSource,
  setCurrentSessionPreviewTarget
} from '@/store/preview'
import { $currentCwd } from '@/store/session'

interface PreviewAttachmentProps {
  label?: string
  source?: PreviewRecordSource
  target: string
  variant?: 'card' | 'link'
}

export function PreviewAttachment({
  label,
  source = 'manual',
  target,
  variant = 'card'
}: PreviewAttachmentProps) {
  const { t } = useI18n()
  const cwd = useStore($currentCwd)
  const activePreview = useStore($previewTarget)
  const [opening, setOpening] = useState(false)
  const activePreviewRef = useRef(activePreview)
  const cwdRef = useRef(cwd)
  const mountedRef = useRef(false)
  const requestTokenRef = useRef(0)
  const targetRef = useRef(target)
  const name = previewName(target)
  const isActive = activePreview?.source === target

  activePreviewRef.current = activePreview
  cwdRef.current = cwd
  targetRef.current = target

  useEffect(() => {
    mountedRef.current = true

    return () => {
      mountedRef.current = false
      requestTokenRef.current += 1
    }
  }, [])

  useEffect(() => {
    requestTokenRef.current += 1
    setOpening(false)
  }, [cwd, target])

  async function openPreview(external = false) {
    if (opening) {
      return
    }

    const requestToken = ++requestTokenRef.current
    const requestTarget = target
    const requestCwd = cwd

    setOpening(true)

    try {
      const preview = await normalizeOrLocalPreviewTarget(requestTarget, requestCwd || undefined)

      if (
        !mountedRef.current ||
        requestTokenRef.current !== requestToken ||
        targetRef.current !== requestTarget ||
        cwdRef.current !== requestCwd
      ) {
        return
      }

      if (!preview) {
        throw new Error(`Could not open preview target: ${requestTarget}`)
      }

      if (external) {
        const bridge = window.hermesDesktop?.openPreviewInBrowser

        if (!bridge) {
          throw new Error('Desktop preview browser bridge is unavailable')
        }

        await bridge(preview.url)

        return
      }

      const currentPreview = activePreviewRef.current

      if (currentPreview?.source === preview.source && currentPreview.url === preview.url) {
        // Re-select the Preview tab when the same report is already loaded but
        // another right-rail tab (for example DFM) is currently in front.
        setCurrentSessionPreviewTarget(preview, source, requestTarget)

        return
      }

      setCurrentSessionPreviewTarget(preview, source, requestTarget)
    } catch (error) {
      if (
        !mountedRef.current ||
        requestTokenRef.current !== requestToken ||
        targetRef.current !== requestTarget ||
        cwdRef.current !== requestCwd
      ) {
        return
      }

      notifyError(error, t.preview.unavailable)
    } finally {
      if (mountedRef.current && requestTokenRef.current === requestToken) {
        setOpening(false)
      }
    }
  }

  function togglePreview() {
    if (isActive) {
      dismissPreviewTarget()

      return
    }

    void openPreview()
  }

  function openLink(event: ReactMouseEvent<HTMLAnchorElement>) {
    event.preventDefault()
    event.stopPropagation()
    void openPreview(event.ctrlKey || event.metaKey)
  }

  if (variant === 'link') {
    return (
      <a
        aria-disabled={opening || undefined}
        className="font-semibold text-foreground underline underline-offset-4 decoration-current/20 wrap-anywhere"
        href="#"
        onClick={openLink}
        title={target}
      >
        {label || `Open ${name}`}
      </a>
    )
  }

  return (
    <div className="flex w-full max-w-160 items-center gap-2 rounded-lg border border-border/55 bg-card/55 px-2.5 py-1.5 text-sm">
      <span className="grid size-6 shrink-0 place-items-center rounded-md bg-muted/55 text-muted-foreground/85">
        <MonitorPlay className="size-3.5" />
      </span>
      <span className="min-w-0 flex-1 truncate text-[0.78rem] font-medium text-foreground/90" title={target}>
        {name}
      </span>
      <button
        className="shrink-0 rounded-md border border-border/55 bg-background/40 px-2 py-1 text-[0.7rem] font-medium text-muted-foreground transition-colors hover:bg-accent/55 hover:text-foreground disabled:opacity-50"
        disabled={opening}
        onClick={togglePreview}
        type="button"
      >
        {opening ? t.preview.opening : isActive ? t.preview.hide : t.preview.openPreview}
      </button>
    </div>
  )
}
