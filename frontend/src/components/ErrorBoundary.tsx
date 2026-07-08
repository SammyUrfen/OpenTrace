import { Component, type ReactNode } from 'react'

interface Props {
  /** Heading shown over the error detail. */
  label?: string
  /** Reset the boundary when this changes (e.g. the active tab key). */
  resetKey?: string | null
  children: ReactNode
}

interface State {
  error: Error | null
}

/**
 * Catches render-time throws so one bad run/tab shows an inline error instead
 * of unmounting the whole app (main.tsx renders <App/> bare — without this, a
 * single chart crash blanks the window, and tab restore from localStorage can
 * re-crash on every launch).
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidUpdate(prev: Props) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null })
    }
  }

  render() {
    if (this.state.error) {
      return (
        <div className="error-boundary" role="alert">
          <div className="error-boundary__title">
            {this.props.label ?? 'Something went wrong rendering this view.'}
          </div>
          <div className="error-boundary__detail">{String(this.state.error)}</div>
          <button type="button" className="ai-btn" onClick={() => this.setState({ error: null })}>
            Retry
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
