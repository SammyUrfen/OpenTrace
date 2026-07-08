/** Program name from a command line: `python3 app.py` -> `python3`,
 *  `./build/my_bin --x` -> `my_bin`. Mirrors `app.paths.command_basename`. */
export function commandBasename(command: string): string {
  const first = command.trim().split(/\s+/)[0] || 'command'
  const slash = first.lastIndexOf('/')
  return (slash >= 0 ? first.slice(slash + 1) : first) || 'command'
}

/** The single user-facing name for a run: a custom label if renamed, else the
 *  command as typed. Used identically by the tab bar and the sidebar row so the
 *  two surfaces never disagree (the backend `display_name` slug is separate). */
export function runLabel(run: { label: string | null; command: string }): string {
  return run.label ?? run.command
}
