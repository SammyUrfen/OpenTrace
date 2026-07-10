# OpenTrace zsh integration.
#
# Transparently wraps a single simple foreground command with `otrace` when the
# master toggle (OPENTRACE_ENABLE_STRACE=1) is on. Interception is done at the
# line editor: the accept-line widget rewrites $BUFFER to `otrace -- <line>`
# BEFORE zsh parses it, so the shell forks otrace as a native foreground job —
# quoting, globbing, job control, $?, and Ctrl-C all behave exactly as normal.
#
# Anything that isn't a single simple external command (pipelines, &&/||, &,
# subshells, builtins, functions, TUIs, bare REPLs) runs natively and untraced.

# Resolve the otrace launcher path from this file's own location unless preset.
() {
  emulate -L zsh
  local self="${${(%):-%x}:A}"
  : ${OPENTRACE_OTRACE:="${self:h}/otrace"}
  : ${OPENTRACE_API:="http://localhost:8000"}
  export OPENTRACE_OTRACE OPENTRACE_API OPENTRACE_API_TOKEN
}

# One-time terminal registration so runs attach to a session + terminal.
if [[ -z "${OPENTRACE_TERMINAL:-}" && -n "${OPENTRACE_API:-}" ]] && \
   command -v curl >/dev/null 2>&1; then
  local __ot_resp __ot_payload
  __ot_payload="{\"shell\":\"${SHELL:-/usr/bin/zsh}\",\"cwd\":\"$PWD\"}"
  [[ -n "${OPENTRACE_SESSION:-}" ]] && \
    __ot_payload="{\"shell\":\"${SHELL:-/usr/bin/zsh}\",\"cwd\":\"$PWD\",\"session_id\":\"$OPENTRACE_SESSION\"}"
  __ot_resp="$(curl -sf --max-time 2 -X POST "$OPENTRACE_API/terminals/attach" \
      -H 'content-type: application/json' -H "Authorization: Bearer ${OPENTRACE_API_TOKEN:-}" -d "$__ot_payload" 2>/dev/null)"
  if [[ -n "$__ot_resp" ]]; then
    export OPENTRACE_SESSION="$(print -r -- "$__ot_resp" | grep -o '"session_id":"[^"]*"' | cut -d'"' -f4)"
    export OPENTRACE_TERMINAL="$(print -r -- "$__ot_resp" | grep -o '"terminal_id":"[^"]*"' | cut -d'"' -f4)"
    local __hf="$(print -r -- "$__ot_resp" | grep -o '"histfile_path":"[^"]*"' | cut -d'"' -f4)"
    [[ -n "$__hf" ]] && export HISTFILE="$__hf"
  fi
  unset __ot_resp __ot_payload __hf
fi

# Classifier: return 0 (trace) only for a single simple external command.
opentrace_should_trace() {
  emulate -L zsh
  local line="$1"
  [[ -z "${line//[[:space:]]/}" ]] && return 1
  [[ "$line" == \#* ]] && return 1

  local -a toks
  toks=( ${(z)line} )              # quote-aware tokenization
  (( ${#toks} )) || return 1

  # Structural gate: any shell control operator -> run native, never wrap.
  # ${(z)} keeps process substitutions / fd redirections as single tokens
  # (e.g. '<(sort)', '2>file'), so match by prefix as well as exact operators.
  local t
  for t in "${toks[@]}"; do
    case "$t" in
      '|'|'|&'|'||'|'&'|'&&'|';'|';;'|'<'|'>'|'>>'|'>&'|'&>'|'<<'|'<<<'|'<>'|'('|')'|'{'|'}') return 1 ;;
      '<('*|'>('*|'&>'*|[0-9]'>'*|[0-9]'<'*) return 1 ;;
    esac
  done

  # Strip leading assignments and transparent prefixes to find the real command.
  while (( ${#toks} )); do
    case "${toks[1]}" in
      *=*) [[ "${toks[1]}" == */* ]] && break; shift toks ;;
      env|sudo|nice|nohup|time|command|exec|stdbuf) shift toks ;;
      *) break ;;
    esac
  done
  (( ${#toks} )) || return 1

  local first="${toks[1]}"

  # Never re-wrap an already-wrapped command. Recalling an old `otrace -- <cmd>`
  # line from history (or typing `ot`/`otrace` directly) would otherwise nest as
  # `otrace -- otrace -- <cmd>`, polluting both the terminal and the run record.
  [[ "${first:t}" == otrace ]] && return 1

  local wtype="$(whence -w -- "$first" 2>/dev/null)"; wtype="${wtype##*: }"
  case "$wtype" in
    builtin|reserved|function|alias|none|'') return 1 ;;   # only real commands
  esac

  case "${first:t}" in
    vim|vi|nvim|nano|emacs|less|more|top|htop|btop|man|ssh|tmux|screen|fzf|watch|lazygit|gdb|info|tig)
      return 1 ;;
  esac

  # Bare REPL with no args is interactive -> skip; `python app.py` traces.
  if (( ${#toks} == 1 )); then
    case "${first:t}" in
      python|python3|node|irb|ruby|R|julia|bash|sh|zsh|fish|psql|mysql|sqlite3) return 1 ;;
    esac
  fi
  return 0
}

# Pull live tracing/session state from the runtime file the app maintains (set
# without typing into the shell, so nothing echoes into the terminal).
opentrace_sync() { [[ -r "${OPENTRACE_RT:-}" ]] && source "$OPENTRACE_RT"; }
autoload -Uz add-zsh-hook 2>/dev/null && add-zsh-hook precmd opentrace_sync
opentrace_sync

# Keep the `otrace --` wrapper out of shell history: when a wrapped line is about
# to be committed, record the clean command the user actually typed and drop the
# wrapper. This runs at history-commit time (via the zshaddhistory hook), so it
# works regardless of the user's HIST_IGNORE_SPACE / SHARE_HISTORY settings —
# unlike a per-widget `setopt localoptions`, whose effect is restored before the
# commit happens.
opentrace_addhistory() {
  emulate -L zsh
  local line=${1%$'\n'}
  if [[ "$line" == "otrace -- "* ]]; then
    print -sr -- "${line#otrace -- }"
    return 1   # don't save the wrapped form (clean line already recorded above)
  fi
  return 0
}
autoload -Uz add-zsh-hook 2>/dev/null && add-zsh-hook zshaddhistory opentrace_addhistory

opentrace-accept-line() {
  opentrace_sync
  if [[ "${OPENTRACE_ENABLE_STRACE:-0}" == "1" ]] && \
     opentrace_should_trace "$BUFFER"; then
    # `otrace` resolves via the hooks dir on PATH, so the rewritten line reads
    # `otrace -- <cmd>` rather than an absolute path. The wrapper is stripped
    # back out before the line is saved (see opentrace_addhistory), so history
    # and up-arrow show the command the user actually typed.
    BUFFER="otrace -- ${BUFFER}"
  fi
  zle .accept-line
}
zle -N opentrace-accept-line
bindkey '^M' opentrace-accept-line
bindkey '^J' opentrace-accept-line

# Manual escape hatch: force-trace a command the classifier would skip.
ot() { "${OPENTRACE_OTRACE}" -- "$@" }
