#!/usr/bin/env bash

CMD="$1"

# extract first token
FIRST=$(echo "$CMD" | awk '{print $1}')

TYPE=$(type -t "$FIRST")

# builtins / functions / aliases → run normally
if [[ "$TYPE" == "builtin" || "$TYPE" == "function" || "$TYPE" == "alias" ]]; then
    eval "$CMD"
    exit $?
fi
if [[ "$CMD" == conda* ]]; then
    eval "$CMD"
    exit $?
fi

# common commands you don't want traced
SKIP_CMDS=(
  "cd" "pwd" "ls" "ll" "la" "cat" "less" "more" "head"
  "clear" "history" "export" "unset" "alias" "source"
  "conda" "activate" "deactivate" "nano" "vim" "vi" 
  "git" "grep" "find" "which" "whereis" "man"
  "echo" "touch" "mkdir" "rm" "mv" "cp" "tail"
)

for c in "${SKIP_CMDS[@]}"; do
  if [[ "$FIRST" == "$c" ]]; then
    eval "$CMD"
    exit $?
  fi
done

# tracing path
TRACE_DIR="$HOME/.opentrace/traces"
mkdir -p "$TRACE_DIR"

TRACE_FILE="$TRACE_DIR/trace-$(date +%s).log"

TRACE_CMD=()

if [[ "$OPENTRACE_ENABLE_STRACE" == "1" ]]; then
   TRACE_CMD+=(strace -f -tt -T -o "$TRACE_FILE")
fi

if [[ "$OPENTRACE_ENABLE_PERF" == "1" ]]; then
   TRACE_CMD+=(perf stat)
fi

if [[ ${#TRACE_CMD[@]} -eq 0 ]]; then
    exec bash -lc "$CMD"
fi

echo "[opentrace] tracing → $TRACE_FILE"

exec "${TRACE_CMD[@]}" bash -lc "$CMD"