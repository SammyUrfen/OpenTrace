import { useState } from 'react'
import { useRunResource } from '../state/useRunResource'
import { formatBytes } from '../state/format'
import { apiFetch } from '../state/api'

interface FileRow {
  name: string
  size: number
  text: boolean
}
interface FileContent {
  name: string
  size: number
  truncated: boolean
  content: string | null
}

interface Props {
  backendUrl: string
  runId: string
}

/** Browse the raw files captured on disk for a run; preview the text ones. */
export function FilesTab({ backendUrl, runId }: Props) {
  const { rows } = useRunResource<FileRow>(backendUrl, runId, 'files')
  const [sel, setSel] = useState<string | null>(null)
  const [doc, setDoc] = useState<FileContent | null>(null)
  const [loading, setLoading] = useState(false)

  const open = (f: FileRow) => {
    setSel(f.name)
    setDoc(null)
    if (!f.text) return
    setLoading(true)
    apiFetch(`${backendUrl}/runs/${runId}/file?name=${encodeURIComponent(f.name)}`)
      .then((r) => r.json())
      .then((d: FileContent) => setDoc(d))
      .catch(() => setDoc(null))
      .finally(() => setLoading(false))
  }

  return (
    <div className="overview" data-testid="files-tab">
      <h3 className="overview__h">Captured files — {rows.length}</h3>
      <div className="files-layout">
        <ul className="files-list">
          {rows.map((f) => (
            <li
              key={f.name}
              className={`files-item ${sel === f.name ? 'files-item--active' : ''} ${f.text ? '' : 'files-item--binary'}`}
              onClick={() => open(f)}
              title={f.text ? 'Click to preview' : 'Binary file'}
            >
              <span className="files-item__name">{f.text ? '📄' : '📦'} {f.name}</span>
              <span className="files-item__size">{formatBytes(f.size)}</span>
            </li>
          ))}
          {rows.length === 0 && <li className="overview__muted">No files for this run.</li>}
        </ul>
        <div className="files-preview">
          {!sel && <div className="overview__muted">Select a file to preview.</div>}
          {sel && loading && <div className="overview__muted">Loading…</div>}
          {sel && !loading && doc?.content != null && (
            <>
              {doc.truncated && (
                <div className="files-preview__note">Showing the first 256 KB of {formatBytes(doc.size)}.</div>
              )}
              <pre className="files-preview__body">{doc.content}</pre>
            </>
          )}
          {sel && !loading && doc?.content == null && (
            <div className="overview__muted">Binary file — open it from {sel} on disk.</div>
          )}
        </div>
      </div>
    </div>
  )
}
