#!/usr/bin/env bash
set -euo pipefail

START_MARKER="# >>> agent-ops-kit ccx >>>"
END_MARKER="# <<< agent-ops-kit ccx <<<"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

TARGET_RC="${HOME}/.bashrc"
if [[ "${1:-}" == "--rc-file" ]]; then
  if [[ -z "${2:-}" ]]; then
    echo "ERROR: --rc-file requires a file path" >&2
    exit 1
  fi
  TARGET_RC="$2"
fi

mkdir -p "$(dirname "$TARGET_RC")"
touch "$TARGET_RC"

BACKUP="${TARGET_RC}.bak.$(date +%Y%m%d-%H%M%S)"
cp "$TARGET_RC" "$BACKUP"

python3 - "$TARGET_RC" "$START_MARKER" "$END_MARKER" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
start = sys.argv[2]
end = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines()

out = []
i = 0
n = len(lines)

alias_re = re.compile(r"^\s*alias\s+ccx=")
fn_open_re = re.compile(r"^\s*ccx\s*\(\)\s*\{")
fn_line_re = re.compile(r"^\s*ccx\s*\(\)\s*$")

while i < n:
    line = lines[i]
    stripped = line.strip()

    if stripped == start:
        i += 1
        while i < n and lines[i].strip() != end:
            i += 1
        if i < n:
            i += 1
        continue

    if alias_re.match(line):
        i += 1
        continue

    if fn_open_re.match(line):
        depth = line.count("{") - line.count("}")
        i += 1
        while i < n and depth > 0:
            depth += lines[i].count("{") - lines[i].count("}")
            i += 1
        continue

    if fn_line_re.match(line):
        i += 1
        while i < n and lines[i].strip() == "":
            i += 1
        if i < n and lines[i].lstrip().startswith("{"):
            depth = lines[i].count("{") - lines[i].count("}")
            i += 1
            while i < n and depth > 0:
                depth += lines[i].count("{") - lines[i].count("}")
                i += 1
        continue

    out.append(line)
    i += 1

new_text = "\n".join(out).rstrip() + "\n"
path.write_text(new_text, encoding="utf-8")
PY

cat >> "$TARGET_RC" <<EOF

$START_MARKER
ccx() {
  local d="\$PWD"
  while [[ "\$d" != "/" ]]; do
    if [[ -x "\$d/.claude/bin/codex-local" ]]; then
      "\$d/.claude/bin/codex-local" "\$@"
      return \$?
    fi
    d="\$(dirname "\$d")"
  done
  echo "ERROR: .claude/bin/codex-local not found from \$PWD upward" >&2
  return 1
}
$END_MARKER
EOF

chmod +x "${REPO_ROOT}/.claude/bin/codex-local" 2>/dev/null || true
chmod +x "${REPO_ROOT}/.claude/scripts/claude-editor.sh" 2>/dev/null || true

if ! bash -n "$TARGET_RC"; then
  cp "$BACKUP" "$TARGET_RC"
  echo "ERROR: ${TARGET_RC} still has syntax errors. Restored backup: ${BACKUP}" >&2
  exit 1
fi

echo "Updated: ${TARGET_RC}"
echo "Backup:  ${BACKUP}"
echo
echo "Next:"
echo "  source ${TARGET_RC}"
echo "  type ccx"
echo "  ccx"
