#!/usr/bin/env bash
set -euo pipefail

log()  { printf "\n==> %s\n" "$*"; }
skip() { printf "    [SKIP] %s\n" "$*"; }
warn() { printf "\n[WARN] %s\n" "$*" >&2; }

is_linux() { [[ "$(uname -s 2>/dev/null || echo '')" == "Linux" ]]; }
has() { command -v "$1" >/dev/null 2>&1; }

if ! is_linux; then
  warn "linux_devtools.sh is Linux-only. Skipping."
  exit 0
fi

# ---- 0) Ensure Claude Code is installed ----
if ! has claude && [[ ! -x "${HOME}/.local/bin/claude" ]]; then
  log "Installing Claude Code (native installer)..."
  curl -fsSL https://claude.ai/install.sh | bash
else
  skip "Claude Code already installed."
fi

if ! has claude && [[ ! -x "${HOME}/.local/bin/claude" ]]; then
  warn "Claude Code binary not found after install attempt. Ensure ~/.local/bin is in PATH."
  exit 1
fi

# ---- 1) Linux notifications (notify-send) ----
if ! has notify-send; then
  if has apt-get; then
    log "Installing notify-send (libnotify-bin) via apt-get..."
    if [[ "$(id -u)" -eq 0 ]]; then
      apt-get update
      apt-get install -y libnotify-bin
    else
      sudo apt-get update
      sudo apt-get install -y libnotify-bin
    fi
  else
    warn "apt-get not found. Install notify-send manually (package is typically 'libnotify-bin')."
  fi
else
  skip "notify-send already installed."
fi

# ---- 2) Node/npm via fnm ----
node_major_or_zero() {
  local v
  v="$(node -v 2>/dev/null || true)"
  printf '%s\n' "${v}" | sed -E 's/^v([0-9]+)\..*/\1/; t; s/.*/0/'
}

setup_node_fnm() {
  if ! has fnm; then
    log "Installing fnm..."
    curl -fsSL https://fnm.vercel.app/install | bash
  else
    skip "fnm already installed."
  fi

  export PATH="$HOME/.local/share/fnm:$PATH"
  if has fnm; then
    eval "$(fnm env)"
  else
    warn "fnm not found after install; cannot proceed with Node install."
    return 1
  fi

  local current_node_major=0
  current_node_major="$(node_major_or_zero)"

  if ! has node || [[ "$current_node_major" -lt 24 ]]; then
    if has node; then
      log "System Node $(node -v) is below Node 24+. Installing/activating fnm LTS..."
    else
      log "Installing Node.js LTS..."
    fi
    INSTALL_OUT="$(fnm install lts-latest 2>&1 || true)"
    LTS_VER="$(printf '%s\n' "$INSTALL_OUT" | sed -nE 's/.*Installing Node (v[0-9]+\.[0-9]+\.[0-9]+).*/\1/p' | head -n1)"
    if [[ -n "${LTS_VER}" ]]; then
      fnm use "${LTS_VER}" || true
      fnm default "${LTS_VER}" || true
    else
      fnm use lts-latest || true
      fnm default lts-latest || true
    fi
  else
    skip "Node.js already installed: $(node -v)"
  fi

  if has corepack; then
    corepack enable || true
    corepack prepare yarn@stable --activate 2>/dev/null || true
  fi
}

NODE_MAJOR_NOW="$(node_major_or_zero)"
if ! has npm; then
  warn "npm not found. Installing Node/npm via fnm now..."
  setup_node_fnm || warn "Node/npm install failed. JS/Python LSP binaries will be skipped."
elif [[ "$NODE_MAJOR_NOW" -lt 24 ]]; then
  warn "Detected Node $(node -v 2>/dev/null || echo 'unknown') (below 24). Activating fnm LTS (Node 24+ needed by the claude-max-proxy node:sqlite store, Claude CLI and Browser Use)..."
  setup_node_fnm || warn "fnm Node activation failed; continuing with system Node."
  skip "npm already installed: $(npm -v)"
else
  skip "npm already installed: $(npm -v)"
fi

# ---- 3) Install language server binaries ----
if has npm; then
  # vtsls
  if ! has vtsls; then
    log "Installing vtsls (TypeScript/JS language server)..."
    npm i -g @vtsls/language-server typescript || warn "Failed to install vtsls/typescript via npm."
  else
    skip "vtsls already installed."
  fi

  # pyright
  if ! has pyright; then
    log "Installing pyright (Python language server)..."
    npm i -g pyright || warn "Failed to install pyright via npm."
  else
    skip "pyright already installed."
  fi
else
  warn "npm not available; skipping vtsls/pyright install."
fi

if has go; then
  if ! has gopls; then
    log "Installing gopls..."
    go install golang.org/x/tools/gopls@latest || warn "go install gopls failed."
  else
    skip "gopls already installed."
  fi
else
  skip "go not found; skipping gopls install."
fi

if has rustup; then
  if ! has rust-analyzer; then
    log "Installing rust-analyzer via rustup component..."
    rustup component add rust-analyzer || warn "rustup component add rust-analyzer failed."
  else
    skip "rust-analyzer already installed."
  fi
else
  skip "rustup not found; skipping rust-analyzer install."
fi

# ---- 4) Ensure PATH includes common install locations ----
if has npm; then
  NPM_PREFIX="$(npm config get prefix 2>/dev/null || true)"
  if [[ -n "${NPM_PREFIX}" && -d "${NPM_PREFIX}/bin" ]]; then
    export PATH="${NPM_PREFIX}/bin:${PATH}"
  fi
fi

if has go; then
  GOPATH_BIN="$(go env GOPATH 2>/dev/null)/bin"
  if [[ -d "${GOPATH_BIN}" ]]; then
    export PATH="${GOPATH_BIN}:${PATH}"
  fi
fi

if [[ -d "${HOME}/.cargo/bin" ]]; then
  export PATH="${HOME}/.cargo/bin:${PATH}"
fi

# ---- 5) Setup ntfy.sh notifications (cross-platform) ----
NTFY_CONFIG="$HOME/.config/claude-code/ntfy_topic"
if [ ! -f "$NTFY_CONFIG" ]; then
  log "Setting up ntfy.sh notifications..."
  mkdir -p "$(dirname "$NTFY_CONFIG")"

  # Generate unique topic: claude-<username>-<random>
  NTFY_TOPIC="claude-$(whoami)-$(head -c 6 /dev/urandom | base64 | tr -dc 'a-z0-9' | head -c 8)"
  echo "$NTFY_TOPIC" > "$NTFY_CONFIG"

  # Add to shell profiles if not already there
  for profile in "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [ -f "$profile" ]; then
      if ! grep -q "CLAUDE_NTFY_TOPIC" "$profile" 2>/dev/null; then
        echo "" >> "$profile"
        echo "# Claude Code notifications (ntfy.sh)" >> "$profile"
        echo "export CLAUDE_NTFY_TOPIC=\"\$(cat ~/.config/claude-code/ntfy_topic 2>/dev/null)\"" >> "$profile"
      fi
    fi
  done

  echo ""
  echo "=========================================="
  echo "  NOTIFICATIONS SETUP"
  echo "=========================================="
  echo ""
  echo "Your ntfy.sh topic: $NTFY_TOPIC"
  echo ""
  echo "Subscribe to get notifications:"
  echo "  Browser: https://ntfy.sh/$NTFY_TOPIC"
  echo "  Phone:   Install 'ntfy' app, subscribe to: $NTFY_TOPIC"
  echo "  Windows: https://github.com/binwiederhier/ntfy/releases"
  echo ""
  echo "Restart your shell or run: source ~/.bashrc"
  echo "=========================================="
else
  NTFY_TOPIC="$(cat "$NTFY_CONFIG")"
  skip "ntfy.sh already configured: $NTFY_TOPIC"
fi

log "Done."
