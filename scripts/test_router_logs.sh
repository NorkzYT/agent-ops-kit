#!/usr/bin/env bash
# test_router_logs.sh — exercise the real `make router-logs*` and
# `make cliproxyapi-logs*` targets offline: fixture agent.log (including a
# path with spaces), a stub compose binary, no-match / missing-file cases,
# LINES validation, and a time-bounded follow that crosses a log rotation.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAKE=(make -C "$ROOT" --no-print-directory)

pass=0; fail=0
ok() { printf '  ok   %s\n' "$1"; pass=$((pass+1)); }
no() { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

tmp="$(mktemp -d)"; trap 'pkill -f "$tmp" 2>/dev/null; rm -rf "$tmp"' EXIT

# Fixture Hermes home — directory name contains spaces on purpose.
hh="$tmp/hermes home"; mkdir -p "$hh/logs"
log="$hh/logs/agent.log"
for i in $(seq 1 25); do
  echo "2026-09-16 INFO model_router decision i=$i" >> "$log"
  echo "2026-09-16 INFO other_component noise i=$i" >> "$log"
done

# Stub compose records its argv and exits, so follow targets are bounded.
stub="$tmp/bin/compose"; mkdir -p "$tmp/bin"; arglog="$tmp/args.log"
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> %q\n' "$arglog" > "$stub"
chmod +x "$stub"

# --- help lists the new targets ------------------------------------------
h="$("${MAKE[@]}" help 2>&1)"
miss=""
for t in router-logs router-logs-follow cliproxyapi-logs cliproxyapi-logs-follow; do
  grep -q "$t" <<<"$h" || miss+=" $t"
done
[[ -z "$miss" ]] && ok "help lists the four log targets" || no "help missing:$miss"

# --- router-logs: default last 20 matches, path with spaces ---------------
out="$(HERMES_HOME="$hh" "${MAKE[@]}" router-logs 2>&1)"; rc=$?
[[ $rc -eq 0 && "$(wc -l <<<"$out")" -eq 20 ]] \
  && [[ "$(head -n1 <<<"$out")" == *"i=6" && "$(tail -n1 <<<"$out")" == *"i=25" ]] \
  && ! grep -q noise <<<"$out" \
  && ok "router-logs: last 20 model_router lines, no noise, spaces in path" \
  || no "router-logs default (rc=$rc, lines=$(wc -l <<<"$out"))"

# --- router-logs: LINES override (make var and environment) ---------------
out="$(HERMES_HOME="$hh" "${MAKE[@]}" router-logs LINES=5 2>&1)"
[[ "$(wc -l <<<"$out")" -eq 5 && "$(tail -n1 <<<"$out")" == *"i=25" ]] \
  && ok "router-logs: LINES=5 make variable" || no "LINES=5 make var (out=$out)"
out="$(HERMES_HOME="$hh" LINES=3 "${MAKE[@]}" router-logs 2>&1)"
[[ "$(wc -l <<<"$out")" -eq 3 ]] \
  && ok "router-logs: LINES=3 environment variable" || no "LINES=3 env (out=$out)"

# --- router-logs: no matching lines --------------------------------------
hh2="$tmp/nomatch"; mkdir -p "$hh2/logs"; echo "only noise here" > "$hh2/logs/agent.log"
out="$(HERMES_HOME="$hh2" "${MAKE[@]}" router-logs 2>&1)"; rc=$?
[[ $rc -eq 0 ]] && grep -q "No model_router lines" <<<"$out" \
  && ok "router-logs: no-match -> clear notice, exit 0" || no "no-match (rc=$rc, out=$out)"

# --- router-logs: missing log file ---------------------------------------
out="$(HERMES_HOME="$tmp/absent" "${MAKE[@]}" router-logs 2>&1)"; rc=$?
[[ $rc -eq 0 ]] && grep -q "No Hermes agent log at" <<<"$out" \
  && ok "router-logs: missing file -> clear notice, exit 0" || no "missing file (rc=$rc, out=$out)"

# --- LINES validation on all four targets ---------------------------------
badfail=0
for bad in abc 0 -3 "5; touch $tmp/pwned" 1.5; do
  for tgt in router-logs router-logs-follow cliproxyapi-logs cliproxyapi-logs-follow; do
    : > "$arglog"
    out="$(HERMES_HOME="$hh" LINES="$bad" "${MAKE[@]}" "$tgt" COMPOSE="$stub" 2>&1)"; rc=$?
    [[ $rc -ne 0 ]] && grep -q "positive integer" <<<"$out" && [[ ! -s "$arglog" && ! -e "$tmp/pwned" ]] \
      || { no "$tgt: LINES='$bad' accepted (rc=$rc, out=$out)"; badfail=1; }
  done
done
[[ $badfail -eq 0 ]] && ok "invalid LINES rejected on all four targets (compose never called)"

# --- empty LINES falls back to the default --------------------------------
out="$(HERMES_HOME="$hh" LINES= "${MAKE[@]}" router-logs 2>&1)"; rc=$?
[[ $rc -eq 0 && "$(wc -l <<<"$out")" -eq 20 ]] \
  && ok "router-logs: empty LINES -> default 20" || no "empty LINES (rc=$rc)"

# --- cliproxyapi-logs: stub compose argv ----------------------------------
: > "$arglog"; "${MAKE[@]}" cliproxyapi-logs COMPOSE="$stub" >/dev/null 2>&1
[[ "$(cat "$arglog")" == "logs --tail=200 cliproxyapi" ]] \
  && ok "cliproxyapi-logs: logs --tail=200 cliproxyapi" || no "cliproxyapi-logs args ($(cat "$arglog"))"
: > "$arglog"; "${MAKE[@]}" cliproxyapi-logs COMPOSE="$stub" LINES=42 >/dev/null 2>&1
[[ "$(cat "$arglog")" == "logs --tail=42 cliproxyapi" ]] \
  && ok "cliproxyapi-logs: LINES=42 respected" || no "cliproxyapi-logs LINES ($(cat "$arglog"))"
: > "$arglog"; "${MAKE[@]}" cliproxyapi-logs-follow COMPOSE="$stub" >/dev/null 2>&1
[[ "$(cat "$arglog")" == "logs -f --tail=200 cliproxyapi" ]] \
  && ok "cliproxyapi-logs-follow: logs -f --tail=200 cliproxyapi" || no "follow args ($(cat "$arglog"))"

# --- router-logs-follow: snapshot, live append, log rotation (bounded) ----
out="$tmp/follow.out"
setsid env HERMES_HOME="$hh" make -C "$ROOT" --no-print-directory router-logs-follow > "$out" 2>&1 &
mpid=$!
sleep 1
echo "2026-09-16 INFO model_router decision i=live" >> "$log"
echo "2026-09-16 INFO other_component noise i=live" >> "$log"
sleep 1
mv "$log" "$log.1"
echo "2026-09-16 INFO model_router decision i=rotated" > "$log"
sleep 2
kill -TERM -- "-$mpid" 2>/dev/null; wait "$mpid" 2>/dev/null
grep -q "i=25" "$out" && grep -q "i=live" "$out" && grep -q "i=rotated" "$out" \
  && ! grep -q "noise i=live" "$out" \
  && ok "router-logs-follow: snapshot + live line + line after rotation, no noise" \
  || no "router-logs-follow (out: $(tail -n 5 "$out" 2>/dev/null | tr '\n' '|'))"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
