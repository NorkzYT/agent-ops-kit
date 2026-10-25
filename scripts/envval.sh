#!/usr/bin/env bash
# envval.sh KEY [FILE] — print one dotenv value using the shared lib.sh parser
# (handles `export`, quotes and CRLF) so the Makefile and the scripts read .env
# the same way. Prints nothing and exits 0 when the key is absent or empty.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib.sh
. "$ROOT/scripts/lib.sh"
env_file_get "${1:?usage: envval.sh KEY [FILE]}" "${2:-$ROOT/.env}" 2>/dev/null || true
