import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { Run } from '../state/useOpenTrace'
import { RunNameBar } from './RunNameBar'
import { TabGuide } from './TabGuide'
import { MainTabs, type TabInfo } from './MainTabs'
import { MenuBar, type MenuDef } from './MenuBar'
import { RunSidebar } from './RunSidebar'

function mkRun(over: Partial<Run> = {}): Run {
  return {
    id: 'r1', session_id: 's1', terminal_id: 't1',
    display_name: 'python_20260703_120000', command: 'python3 train.py',
    command_basename: 'python3', cwd: '/home/u/demo', started_at: 1_700_000_000_000,
    ended_at: 1_700_000_002_500, duration_ms: 2500, exit_code: 0, exit_signal: null,
    status: 'completed', label: null, max_severity: null,
    collector_config: null, created_at: 1_700_000_000_000,
    ...over,
  }
}

describe('RunNameBar', () => {
  it('saves a changed name (trimmed) then dismisses', () => {
    const onRename = vi.fn()
    const onDismiss = vi.fn()
    render(<RunNameBar run={mkRun()} onRename={onRename} onDismiss={onDismiss} />)
    const input = screen.getByLabelText('run name') as HTMLInputElement
    fireEvent.change(input, { target: { value: '  nightly-bench  ' } })
    fireEvent.click(screen.getByText('Save'))
    expect(onRename).toHaveBeenCalledWith('nightly-bench')
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })

  it('does not rename when the name is unchanged', () => {
    const onRename = vi.fn()
    const onDismiss = vi.fn()
    render(<RunNameBar run={mkRun()} onRename={onRename} onDismiss={onDismiss} />)
    fireEvent.click(screen.getByText('Save'))
    expect(onRename).not.toHaveBeenCalled()
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })

  it('"Keep default" dismisses without renaming', () => {
    const onRename = vi.fn()
    const onDismiss = vi.fn()
    render(<RunNameBar run={mkRun()} onRename={onRename} onDismiss={onDismiss} />)
    fireEvent.click(screen.getByText('Keep default'))
    expect(onRename).not.toHaveBeenCalled()
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })

  it('Escape dismisses without renaming; Enter saves', () => {
    const onRename = vi.fn()
    const onDismiss = vi.fn()
    render(<RunNameBar run={mkRun()} onRename={onRename} onDismiss={onDismiss} />)
    const input = screen.getByLabelText('run name')
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(onDismiss).toHaveBeenCalledTimes(1)
    expect(onRename).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: 'renamed' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onRename).toHaveBeenCalledWith('renamed')
  })
})

describe('TabGuide', () => {
  it('renders a view-specific guide for a known view', () => {
    render(<TabGuide view="flamegraph" />)
    expect(screen.getByText('How to read this')).toBeTruthy()
    expect(screen.getByText(/Reading the Flamegraph/)).toBeTruthy()
  })

  it('renders nothing for an unknown view', () => {
    const { container } = render(<TabGuide view="does-not-exist" />)
    expect(container.firstChild).toBeNull()
  })
})

describe('MainTabs rename affordance', () => {
  const tabs: TabInfo[] = [
    { key: 'run:r1', label: 'python_1', title: 'python a.py' },
    { key: 'diff:r1:r2', label: 'a ↔ b', diff: true },
  ]

  it('double-clicking a run tab requests rename with its key', () => {
    const onRename = vi.fn()
    render(
      <MainTabs tabs={tabs} activeKey="run:r1" onSelect={() => {}} onClose={() => {}} onRename={onRename} />,
    )
    fireEvent.doubleClick(screen.getByText('python_1'))
    expect(onRename).toHaveBeenCalledWith('run:r1')
  })

  it('double-clicking a diff tab does not request rename', () => {
    const onRename = vi.fn()
    render(
      <MainTabs tabs={tabs} activeKey="run:r1" onSelect={() => {}} onClose={() => {}} onRename={onRename} />,
    )
    fireEvent.doubleClick(screen.getByText('a ↔ b'))
    expect(onRename).not.toHaveBeenCalled()
  })
})

describe('MenuBar', () => {
  const menus: MenuDef[] = [
    { label: 'File', items: [{ label: 'New Session', action: 'new-session', accel: 'Ctrl+N' }] },
    { label: 'View', items: [
      { label: 'Command Palette…', action: 'command-palette', accel: 'Ctrl+K' },
      { separator: true },
      { label: 'Toggle Theme', action: 'toggle-theme' },
    ] },
  ]

  it('opens a dropdown on click and fires the picked action', () => {
    const onAction = vi.fn()
    render(<MenuBar menus={menus} onAction={onAction} />)
    expect(screen.queryByText('Toggle Theme')).toBeNull() // closed initially
    fireEvent.click(screen.getByRole('button', { name: 'View' }))
    expect(screen.getByText('Toggle Theme')).toBeTruthy()
    expect(screen.getByText('Ctrl+K')).toBeTruthy()
    fireEvent.click(screen.getByText('Toggle Theme'))
    expect(onAction).toHaveBeenCalledWith('toggle-theme')
    expect(screen.queryByText('Toggle Theme')).toBeNull() // closes after pick
  })

  it('clicking the open top item toggles it closed; Escape also closes', () => {
    render(<MenuBar menus={menus} onAction={vi.fn()} />)
    const file = screen.getByRole('button', { name: 'File' })
    fireEvent.click(file)
    expect(screen.getByText('New Session')).toBeTruthy()
    fireEvent.click(file)
    expect(screen.queryByText('New Session')).toBeNull()
    fireEvent.click(file)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByText('New Session')).toBeNull()
  })
})

describe('RunSidebar run naming (label ?? command)', () => {
  const project = {
    id: 's1', display_name: 'Default', slug: 'default',
    created_at: 0, updated_at: 0, last_opened_at: null, notes: null,
  }

  it('shows the command when unlabeled, the label when renamed', () => {
    const runs = [
      mkRun({ id: 'r1', command: 'python a.py', label: null }),
      mkRun({ id: 'r2', command: 'python b.py', label: 'nightly-bench' }),
    ]
    render(<RunSidebar projects={[project]} runs={runs} connected />)
    expect(screen.getByText('python a.py')).toBeTruthy()   // unlabeled → command
    expect(screen.getByText('nightly-bench')).toBeTruthy() // labeled → label
    expect(screen.queryByText('python b.py')).toBeNull()   // command hidden behind label
  })
})
