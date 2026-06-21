import { type ReactNode } from 'react'

/**
 * Minimal, dependency-free, XSS-safe markdown renderer for LLM summaries.
 * Supports the subset the prompt asks for: #/##/### headers, * or - bullet
 * lists, **bold**, `code`, and paragraphs. Text is rendered as React nodes
 * (never raw HTML), so model output can't inject markup.
 */

function inline(text: string, keyBase: string): ReactNode[] {
  // Split on **bold** and `code`, keeping the delimiters.
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
  return parts.map((p, i) => {
    if (p.startsWith('**') && p.endsWith('**')) {
      return <strong key={`${keyBase}-${i}`}>{p.slice(2, -2)}</strong>
    }
    if (p.startsWith('`') && p.endsWith('`')) {
      return <code key={`${keyBase}-${i}`}>{p.slice(1, -1)}</code>
    }
    return <span key={`${keyBase}-${i}`}>{p}</span>
  })
}

export function Markdown({ text }: { text: string }) {
  const lines = text.split('\n')
  const blocks: ReactNode[] = []
  let list: string[] = []
  let para: string[] = []

  const flushList = () => {
    if (list.length) {
      const items = [...list]
      blocks.push(
        <ul key={`ul-${blocks.length}`} className="md-ul">
          {items.map((it, i) => (
            <li key={i}>{inline(it, `li-${blocks.length}-${i}`)}</li>
          ))}
        </ul>,
      )
      list = []
    }
  }
  const flushPara = () => {
    if (para.length) {
      const txt = para.join(' ')
      blocks.push(
        <p key={`p-${blocks.length}`} className="md-p">
          {inline(txt, `p-${blocks.length}`)}
        </p>,
      )
      para = []
    }
  }

  for (const raw of lines) {
    const line = raw.trimEnd()
    const h = /^(#{1,3})\s+(.*)$/.exec(line)
    const li = /^\s*[*-]\s+(.*)$/.exec(line)
    if (h) {
      flushList()
      flushPara()
      const level = h[1].length
      const cls = `md-h md-h${level}`
      blocks.push(
        <div key={`h-${blocks.length}`} className={cls}>
          {inline(h[2], `h-${blocks.length}`)}
        </div>,
      )
    } else if (li) {
      flushPara()
      list.push(li[1])
    } else if (line.trim() === '') {
      flushList()
      flushPara()
    } else {
      flushList()
      para.push(line)
    }
  }
  flushList()
  flushPara()
  return <div className="md">{blocks}</div>
}
