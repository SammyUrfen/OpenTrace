# OpenTrace bash integration.
#
# Bash's readline cannot rewrite the accepted line from a `bind -x` handler (the
# accept key ignores READLINE_LINE edits), so bash does NOT get the transparent
# auto-interception that zsh does. Instead it gets an explicit, reliable helper:
#
#     ot <command> [args...]      # run a command under OpenTrace tracing
#
# This is the honest Phase-1 scope for bash; zsh is the fully-wrapped path.

# Resolve the otrace launcher path from this file's own location unless preset.
if [ -z "${OPENTRACE_OTRACE:-}" ]; then
  __ot_self="${BASH_SOURCE[0]}"
  OPENTRACE_OTRACE="$(cd "$(dirname "$__ot_self")" >/dev/null 2>&1 && pwd)/otrace"
  unset __ot_self
fi
: "${OPENTRACE_API:=http://localhost:8000}"
export OPENTRACE_OTRACE OPENTRACE_API

# One-time terminal registration so runs attach to a session + terminal.
if [ -z "${OPENTRACE_TERMINAL:-}" ] && [ -n "${OPENTRACE_API:-}" ] && \
   command -v curl >/dev/null 2>&1; then
  __ot_payload="{\"shell\":\"${SHELL:-/bin/bash}\",\"cwd\":\"$PWD\"}"
  [ -n "${OPENTRACE_SESSION:-}" ] && \
    __ot_payload="{\"shell\":\"${SHELL:-/bin/bash}\",\"cwd\":\"$PWD\",\"session_id\":\"$OPENTRACE_SESSION\"}"
  __ot_resp="$(curl -sf --max-time 2 -X POST "$OPENTRACE_API/terminals/attach" \
      -H 'content-type: application/json' -d "$__ot_payload" 2>/dev/null)"
  if [ -n "$__ot_resp" ]; then
    export OPENTRACE_SESSION="$(printf '%s' "$__ot_resp" | grep -o '"session_id":"[^"]*"' | cut -d'"' -f4)"
    export OPENTRACE_TERMINAL="$(printf '%s' "$__ot_resp" | grep -o '"terminal_id":"[^"]*"' | cut -d'"' -f4)"
    __ot_hf="$(printf '%s' "$__ot_resp" | grep -o '"histfile_path":"[^"]*"' | cut -d'"' -f4)"
    [ -n "$__ot_hf" ] && export HISTFILE="$__ot_hf"
  fi
  unset __ot_payload __ot_resp __ot_hf
fi

# Explicit tracing helper. Respects the master toggle: if tracing is OFF, runs
# the command plainly so `ot` is always safe to leave in muscle memory.
ot() {
  if [ "${OPENTRACE_ENABLE_STRACE:-0}" = "1" ]; then
    "${OPENTRACE_OTRACE}" -- "$@"
  else
    "$@"
  fi
}
