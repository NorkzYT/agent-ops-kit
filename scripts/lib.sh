#!/usr/bin/env bash
# lib.sh — small helpers shared by the agent-ops-kit scripts (bash, no deps).
# Source it; do not execute it.

log()  { printf '[agent-ops-kit] %s\n' "$*"; }
warn() { printf '[agent-ops-kit][warn] %s\n' "$*" >&2; }
die()  { printf '[agent-ops-kit][error] %s\n' "$*" >&2; exit 1; }

# env_file_get KEY FILE -> prints the last assignment of KEY in a dotenv file.
# Handles `export KEY=`, quotes and CRLF. Non-zero when absent or empty.
env_file_get() {
  local key="$1" file="$2" line val
  [[ -f "$file" ]] || return 1
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" | tail -n1 || true)"
  [[ -n "$line" ]] || return 1
  val="${line#*=}"
  val="${val%$'\r'}"
  val="${val%\"}"; val="${val#\"}"
  val="${val%\'}"; val="${val#\'}"
  [[ -n "$val" ]] || return 1
  printf '%s\n' "$val"
}

# env_file_set KEY VALUE FILE -> replace or append KEY=VALUE (idempotent).
env_file_set() {
  local key="$1" val="$2" file="$3"
  touch "$file"
  if grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file"; then
    # Replace in place, keeping other lines untouched.
    local tmp; tmp="$(mktemp)"
    awk -v k="$key" -v v="$val" '
      $0 ~ "^[[:space:]]*(export[[:space:]]+)?" k "=" { print k "=" v; next } { print }
    ' "$file" > "$tmp" && cat "$tmp" > "$file" && rm -f "$tmp"
  else
    printf '%s=%s\n' "$key" "$val" >> "$file"
  fi
}

# ensure_secret KEY FILE -> fill KEY with a random hex string when empty.
ensure_secret() {
  local key="$1" file="$2"
  if ! env_file_get "$key" "$file" >/dev/null 2>&1; then
    env_file_set "$key" "$(random_hex 24)" "$file"
    log "generated $key"
  fi
}

random_hex() {
  local n="${1:-24}"
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex "$n"
  elif [[ -r /dev/urandom ]]; then od -An -N"$n" -tx1 /dev/urandom | tr -d ' \n'
  else date +%s%N | sha256sum | cut -c1-"$((n * 2))"; fi
}

# render_template SRC DEST -> substitute __KEY__ placeholders from the environment.
# Every __UPPER_SNAKE__ token in SRC is replaced by the value of that variable
# (empty when unset). Uses only sed so it works everywhere bash does.
render_template() {
  local src="$1" dest="$2" tokens t expr=""
  tokens="$(grep -o '__[A-Z][A-Z0-9_]*__' "$src" | sort -u || true)"
  for t in $tokens; do
    local name="${t#__}"; name="${name%__}"
    local val="${!name:-}"
    val="${val//\\/\\\\}"; val="${val//|/\\|}"; val="${val//&/\\&}"
    expr+="s|${t}|${val}|g;"
  done
  mkdir -p "$(dirname "$dest")"
  sed -e "$expr" "$src" > "$dest"
}

have() { command -v "$1" >/dev/null 2>&1; }
