/**
 * A collapsible "How to read this" footer shown at the bottom of every analytics
 * view, explaining what the numbers mean and what to look for. Content is keyed
 * by the view key from `runViews()` (RunView.tsx) and DIFF_VIEWS (DiffView.tsx).
 *
 * Native <details> gives us zero-JS collapse; it stays closed by default so it
 * never gets in the way, and remembers per-session whether the user opened it.
 */
interface GuideEntry {
  title: string
  body: React.ReactNode
}

const GUIDES: Record<string, GuideEntry> = {
  overview: {
    title: 'Reading the Overview',
    body: (
      <>
        <p>
          A one-glance snapshot: peak memory, peak CPU, wall-clock duration, exit
          code, and the syscall/library totals. The <b>anomaly cards</b> are the
          rule engine's findings — each names the evidence (the metric or syscall
          that tripped it) and a severity. Green means nothing crossed a threshold.
        </p>
        <p>
          Start here, then jump to the tab a card points at (a memory-leak card →
          Memory, a slow-I/O card → I/O). The AI summary, if configured, narrates
          the same evidence in plain English.
        </p>
      </>
    ),
  },
  timeline: {
    title: 'Reading the Timeline',
    body: (
      <>
        <p>
          Everything the run did, on one shared time axis (seconds from launch).
          The lanes — memory, CPU, syscall rate — are aligned so you can see cause
          and effect: a syscall spike that lines up with a memory jump, a CPU
          plateau that matches a stall. Each lane marks its <b>peak</b>.
        </p>
        <p>
          Look for the <i>shape</i>: steady growth (leak), sawtooth (alloc/free
          churn), a flat CPU line at 100% (a busy loop), or a long gap with no
          activity (blocked on I/O or a lock).
        </p>
      </>
    ),
  },
  memory: {
    title: 'Reading Memory',
    body: (
      <>
        <p>
          <b>RSS</b> (resident set) is physical RAM the process actually holds;
          <b> VMS</b> is the larger virtual reservation. RSS is the number that
          matters for "is this leaking." Sampled ~4×/second by psutil over the
          whole process subtree.
        </p>
        <p>
          A line that only ever climbs and never drops is the classic leak
          signature. A steep single spike is a large transient allocation. Compare
          two runs (right-click a run → Compare) to see if a change moved the curve.
        </p>
      </>
    ),
  },
  cpu: {
    title: 'Reading CPU',
    body: (
      <>
        <p>
          CPU% is whole-subtree utilization; <b>100% = one core saturated</b>, and
          values above 100% mean multiple cores are busy (threads/children). Also
          sampled ~4×/second.
        </p>
        <p>
          A sustained flat line near a core-multiple with little I/O suggests a
          compute-bound or spinning loop — switch on <b>perf</b> and open the
          Flamegraph to see <i>which function</i> is burning it.
        </p>
      </>
    ),
  },
  io: {
    title: 'Reading I/O',
    body: (
      <>
        <p>
          One row per file the program touched (resolved from fds back to paths),
          with opens, reads, writes, bytes moved, and whether the fd was ever
          closed. <b>Leaked</b> means opened but never closed — the fd-leak signal.
        </p>
        <p>
          Watch for the same small file read thousands of times (missing cache), a
          file written far more than expected, or a growing count of never-closed
          descriptors across a long run.
        </p>
      </>
    ),
  },
  network: {
    title: 'Reading Network',
    body: (
      <>
        <p>
          Outbound connections parsed from <code>connect()</code> calls — the
          destination address/port, how many attempts, and their result. A burst
          of connects to the same endpoint often means ret/reconnect churn; a
          string of <code>ECONNREFUSED</code>/<code>ETIMEDOUT</code> points at an
          unreachable or slow dependency.
        </p>
        <p>Populated from strace or ltrace's <code>@SYS</code> lines.</p>
      </>
    ),
  },
  processes: {
    title: 'Reading Processes',
    body: (
      <>
        <p>
          The process tree the run spawned: each command, its parent, syscall
          count, and lifespan. Useful when a "single" command actually forks a lot
          (build systems, shell scripts) — a long tail of short-lived children can
          dominate the run's cost.
        </p>
      </>
    ),
  },
  syscalls: {
    title: 'Reading Syscalls',
    body: (
      <>
        <p>
          Every system call the program made, aggregated: count, total time, error
          count, and the errors themselves. Sort by <b>time</b> to find where the
          program actually waited, or by <b>count</b> to spot chatter (thousands of
          tiny <code>read</code>/<code>write</code>/<code>stat</code> calls).
        </p>
        <p>
          A syscall is the boundary between your program and the kernel — high time
          in <code>read</code>/<code>poll</code>/<code>futex</code> means waiting on
          I/O or locks, not computing. Errors here (like <code>ENOENT</code>,
          <code>EAGAIN</code>) are often the root cause a program hides.
        </p>
      </>
    ),
  },
  logs: {
    title: 'Reading Logs',
    body: (
      <>
        <p>
          The program's own stdout/stderr, reconstructed from strace's write-data
          dumps (so we capture it without changing how the program sees its
          terminal). Lines that fall inside an anomaly's time window are
          highlighted, so you can read the program's output next to what the tracer
          saw. Only available for strace runs.
        </p>
      </>
    ),
  },
  profiling: {
    title: 'Reading Profiling (allocations)',
    body: (
      <>
        <p>
          Built from ltrace's view of <code>malloc</code>/<code>free</code>/
          <code>realloc</code>/<code>calloc</code>. The ledger tracks bytes
          allocated vs freed, <b>peak live</b> bytes, and <b>outstanding blocks</b>
          — allocations with no matching free by the time the program exited (the
          leak count). The hotspot table ranks the busiest library calls.
        </p>
        <p>
          Outstanding blocks &gt; 0 with growing bytes is a leak; a huge
          alloc+free count with low peak-live is churn (lots of temporary objects)
          — often a performance problem even without a leak.
        </p>
      </>
    ),
  },
  flamegraph: {
    title: 'Reading the Flamegraph',
    body: (
      <>
        <p>
          Sampled call stacks from <b>perf</b>. Width = how much CPU time was spent
          in a function <i>and everything it called</i>; the y-axis is call depth
          (callers below, callees above). It is <b>not</b> a timeline — bars are not
          chronological, they're merged samples.
        </p>
        <p>
          Find the widest bar that isn't a framework/entry frame: that's your hot
          path. Click a frame to zoom into its subtree. A single wide leaf function
          is the thing actually eating the CPU you saw on the CPU tab.
        </p>
      </>
    ),
  },
  files: {
    title: 'Reading Files',
    body: (
      <>
        <p>
          The raw artifacts OpenTrace captured on disk for this run —
          <code>meta.json</code>, the strace/ltrace logs, the compressed
          event/metric streams, and any derived JSON (profile, flamegraph). Click a
          text file to preview it. This is the ground truth behind every other tab,
          handy for exporting or double-checking a finding.
        </p>
      </>
    ),
  },
}

export function TabGuide({ view }: { view: string }) {
  const g = GUIDES[view]
  if (!g) return null
  return (
    <details className="tab-guide">
      <summary className="tab-guide__summary">
        <span className="tab-guide__icon" aria-hidden>?</span>
        How to read this
      </summary>
      <div className="tab-guide__body">
        <h4 className="tab-guide__title">{g.title}</h4>
        {g.body}
      </div>
    </details>
  )
}
