import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Markdown } from './Markdown'

describe('Markdown', () => {
  it('renders headers, bold, code, bullets, and paragraphs', () => {
    const md = [
      '## What\'s Wrong',
      'There is a **memory leak** in `train.py`.',
      '',
      '## What to Investigate',
      '* check the data loader',
      '* look for unbounded lists',
    ].join('\n')
    const { container } = render(<Markdown text={md} />)
    expect(screen.getByText("What's Wrong")).toBeInTheDocument()
    expect(container.querySelector('strong')?.textContent).toBe('memory leak')
    expect(container.querySelector('code')?.textContent).toBe('train.py')
    expect(container.querySelectorAll('.md-ul li').length).toBe(2)
    expect(screen.getByText('check the data loader')).toBeInTheDocument()
  })

  it('does not inject raw HTML (XSS-safe)', () => {
    const { container } = render(<Markdown text={'## <img src=x onerror=alert(1)>'} />)
    expect(container.querySelector('img')).toBeNull()
    // the angle-bracket text is rendered as inert text
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>')
  })
})
