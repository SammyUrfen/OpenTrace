#!/usr/bin/env bash
# Run every scenario file in parallel waves (each file = one isolated app), then
# tally. Usage: ./run-all.sh [wave_size]   (default 4 concurrent instances)
set -u
cd "$(dirname "$0")"
WAVE="${1:-4}"
FILES=(00-core 10-sessions 11-attach 12-monitor 13-ebpf 14-run-tabs 15-analytics \
       16-settings 17-palette-menu 18-diff 19-edge-cases 20-terminal)
mkdir -p out
i=0
for f in "${FILES[@]}"; do
  ( timeout 400 node run.js "file:$f.js" > "out/all-$f.log" 2>&1 ) &
  i=$((i+1)); [ $((i % WAVE)) -eq 0 ] && wait
done
wait

echo "======================= RESULTS ======================="
pass=0; total=0; fails=""
for f in "${FILES[@]}"; do
  line=$(grep -h "passed →" "out/all-$f.log" 2>/dev/null | sed 's| →.*||')
  printf "  %-18s %s\n" "$f" "${line:-NO RESULT}"
  p=$(echo "$line" | grep -oE '^[0-9]+'); t=$(echo "$line" | grep -oE '/[0-9]+' | tr -d /)
  pass=$((pass + ${p:-0})); total=$((total + ${t:-0}))
  fl=$(grep -hE "^  [✗‼]" "out/all-$f.log" 2>/dev/null)
  [ -n "$fl" ] && fails="$fails\n$fl"
done
echo "-------------------------------------------------------"
echo "  TOTAL: $pass/$total passed"
[ -n "$fails" ] && { echo "  FAILURES:"; echo -e "$fails"; }
