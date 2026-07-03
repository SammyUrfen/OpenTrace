/** "How to use OpenTrace effectively" — shared by the first-run wizard and the
 *  Settings ▸ Guide section. Pure presentational content. */
export function UsageGuide() {
  return (
    <div className="guide">
      <p className="guide__lead">
        OpenTrace traces the commands you run <b>transparently</b>. Flip tracing{' '}
        <b>ON</b> (top-right, or ⌘/Ctrl+Shift+T), then use your terminal normally —
        each command is measured, analyzed, and saved as a <i>run</i> you can open,
        compare, and get an AI summary for.
      </p>

      <h4 className="guide__h">The collectors (what gets captured)</h4>
      <ul className="guide__list">
        <li>
          <b>Resource metrics</b> — CPU, memory, FDs, threads (psutil). Always
          useful; leave it on.
        </li>
        <li>
          <b>Syscall trace</b> (strace) — syscalls, I/O, network, processes, and
          program logs. The default backend.
        </li>
        <li>
          <b>Library calls</b> (ltrace) — a malloc/free ledger + library-call
          hotspots (the <i>Profiling</i> tab). Uses ptrace, so it{' '}
          <b>replaces Syscall trace</b> — pick one. Best on native programs
          (C/C++/Rust); it can't see inside an interpreter like Python.
        </li>
        <li>
          <b>Hardware perf</b> — a CPU <i>Flamegraph</i> + function hotspots. It
          samples (doesn't use ptrace), so it can run <b>alongside</b> either
          tracer — but a flamegraph is cleanest with the tracer off. Needs{' '}
          <code>perf_event_paranoid ≤ 2</code>.
        </li>
      </ul>

      <h4 className="guide__h">A good workflow</h4>
      <ol className="guide__list">
        <li>Turn tracing ON and run your command (e.g. <code>python app.py</code>).</li>
        <li>The finished run opens automatically — read the Overview + AI summary.</li>
        <li>Dig into Timeline / Memory / CPU / I/O / Syscalls (and Profiling or Flamegraph if enabled).</li>
        <li>Right-click two runs → <b>Compare</b> for a side-by-side diff ("better or worse?").</li>
        <li>Group related runs under a <b>Session</b> (project) from the menu or palette.</li>
      </ol>

      <p className="guide__tip">
        Tip: press <kbd>⌘/Ctrl</kbd>+<kbd>K</kbd> for the command palette to jump
        to any run or action.
      </p>
    </div>
  )
}
