/** Program name from a command line: `python3 app.py` -> `python3`,
 *  `./build/my_bin --x` -> `my_bin`. Mirrors `app.paths.command_basename`. */
export function commandBasename(command: string): string {
  const first = command.trim().split(/\s+/)[0] || 'command'
  const slash = first.lastIndexOf('/')
  return (slash >= 0 ? first.slice(slash + 1) : first) || 'command'
}
