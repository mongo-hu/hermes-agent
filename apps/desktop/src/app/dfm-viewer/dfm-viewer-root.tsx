import { createRoot } from 'react-dom/client'

import { DfmViewerPane } from './dfm-viewer-pane'

export { DfmViewerPane } from './dfm-viewer-pane'

export function mountDfmViewer() {
  const manifestPath = new URLSearchParams(window.location.search).get('manifest') || ''

  createRoot(document.getElementById('root')!).render(<DfmViewerPane target={{ manifestPath, status: 'preview' }} />)
}
