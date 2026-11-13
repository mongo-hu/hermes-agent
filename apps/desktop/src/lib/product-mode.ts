/**
 * This branch ships the dedicated DFM desktop client rather than the general
 * agent workspace. Keep the product boundary in one place so generic
 * capabilities can remain implemented without leaking back into the DFM UI.
 */
export const DFM_PRODUCT_MODE = true

/** The dedicated client accepts one native STEP/STP model format at intake. */
export function isDfmStpModelPath(value: string): boolean {
  return /\.stp$/i.test(value.trim())
}

/** Shared by the welcome CTA and the composer "+" control. */
export function dfmStpPickerOptions() {
  return {
    filters: [{ extensions: ['stp'], name: 'STP 三维模型' }],
    multiple: false,
    title: '选择 STP 三维模型'
  }
}

const LEGACY_PRODUCT_NAME_RE = /hermes/gi

interface SessionSummary {
  preview?: null | string
  title?: null | string
}

const DFM_SESSION_MARKER =
  /(?:\bdfm\b|\.stp\b|\.step\b|\.igs\b|\.iges\b|\.x_t\b|\.x_b\b|可制造性|制造分析|注塑|冲压|成形|模具|零件|三维模型|工艺参数)/iu

/**
 * Historical installs may share the same session database with the general
 * agent client. Keep those conversations out of the dedicated DFM product
 * unless their title or preview clearly belongs to a manufacturing analysis.
 */
export function isDfmSessionSummary(session: SessionSummary): boolean {
  return DFM_SESSION_MARKER.test(`${session.title ?? ''}\n${session.preview ?? ''}`)
}

/**
 * Keep a session created inside the dedicated DFM client routable while the
 * backend has not persisted its attachment-aware preview yet. The server's
 * real title/preview replaces this optimistic value after the first refresh.
 */
export function optimisticSessionPreviewForProduct(preview: null | string): null | string {
  if (!DFM_PRODUCT_MODE) {
    return preview
  }

  const trimmed = preview?.trim()

  return trimmed ? `DFM · ${trimmed}` : 'DFM analysis'
}

/** Replace legacy branding only in rendered copy; runtime identifiers stay unchanged. */
export function sanitizeDfmDisplayText(text: string): string {
  return DFM_PRODUCT_MODE ? text.replace(LEGACY_PRODUCT_NAME_RE, 'DFM') : text
}
