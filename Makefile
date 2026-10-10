# agent-ops-kit — operator entry points.
#   make help                 list targets
#   make init                 create .env (+secrets), render CLIProxyAPI config
#   make up                   start the Docker half (Honcho, Ollama, CLIProxyAPI)
#   make claude-proxy-install claude-max-proxy on this host (systemd user service)
#   make hermes-install       install/configure Hermes on this host (Discord, Honcho, Browser Use)
#   make hermes-config-apply  re-apply .env to ~/.hermes (forced render, no gateway restart)
SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

COMPOSE ?= docker compose
ENV_FILE ?= .env
S ?=
NAME ?=

# Read a value from .env using the same parser the scripts use (scripts/lib.sh
# via scripts/envval.sh), so Make and the scripts never disagree on quoting.
define envval
$(shell bash scripts/envval.sh $(1) $(ENV_FILE))
endef

# $(call tail_lines,<default>) sets shell var $$n to LINES (env or `make LINES=n`)
# validated as a positive integer, falling back to <default>.
define tail_lines
n="$${LINES:-$(1)}"; [[ "$$n" =~ ^[1-9][0-9]*$$ ]] || { echo "LINES must be a positive integer (got '$$n')" >&2; exit 2; }
endef

.PHONY: help init up down restart pull update ps status logs \
	    cliproxyapi-logs cliproxyapi-logs-follow router-logs router-logs-follow \
	    auth-codex auth-claude-proxy claude-proxy-install claude-proxy-update claude-proxy-restart claude-proxy-logs claude-cli-update \
	    models honcho-health doctor route-smoke test-router specialist-route specialist-logs specialist-logs-follow specialist-status specialist-install specialist-enable specialist-disable test-specialists test-specialist-activate \
	    local-route-status local-models local-route-readback specialist-classify-probe test-local-route test-private-honcho \
	    windows-vm-network windows-vm-network-status windows-vm-network-off \
	    hermes-install hermes-config-apply hermes-profile knowledge-convert knowledge-stage knowledge-refresh knowledge-inventory knowledge-classify knowledge-reconcile knowledge-learn knowledge-learn-status test-knowledge-learn test-knowledge-stage test-knowledge-refresh test-knowledge-classify test-document-converter hermes-restart hermes-logs hermes-ui hermes-ui-status hermes-ui-stop clean

# `##@ Title` lines start a help section; `target: ## text` lines are listed under it.
help: ## Show this help
	@echo "Usage: make <target> [VAR=value]"
	@awk 'BEGIN{FS=":.*##"} /^##@ /{printf "\n\033[1m%s\033[0m\n",substr($$0,5); next} /^[a-zA-Z_-]+( [a-zA-Z_-]+)*:.*##/{printf "  \033[36m%-25s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)

##@ Setup and Docker stack

init: ## Create .env with generated secrets and render the CLIProxyAPI config
	@bash scripts/stack-init.sh

up: init ## Start (or update) the Docker half: Honcho, Ollama, CLIProxyAPI
	@$(COMPOSE) up -d --remove-orphans
	@echo; echo "Docker half is starting. Next: make auth-codex, make claude-proxy-install, make auth-claude-proxy, make doctor"

down: ## Stop the Docker half (data volumes are kept)
	@$(COMPOSE) down

restart: ## Restart all containers, or one with S=<service>
	@$(COMPOSE) restart $(S)

pull: ## Pull newer images
	@$(COMPOSE) pull

update: ## Pull images, restart containers, refresh + rebuild + restart the Claude proxy
	@$(COMPOSE) pull
	@$(COMPOSE) up -d --remove-orphans
	@$(MAKE) --no-print-directory claude-proxy-update

ps status: ## Container status
	@$(COMPOSE) ps

logs: ## Tail container logs (all, or S=<service>)
	@$(COMPOSE) logs -f --tail=200 $(S)

clean: ## Stop the Docker half AND delete its volumes: Honcho memory, Ollama models (asks first)
	@read -r -p "This deletes Honcho memory and the Ollama model. Type 'yes' to continue: " a; \
	[[ "$$a" == "yes" ]] && $(COMPOSE) down -v || echo "aborted"

##@ Proxies and auth

auth-codex: ## Log the ChatGPT/Codex subscription into CLIProxyAPI (device code flow)
	@echo "Follow the URL shown, sign in with your ChatGPT account, then come back."
	@$(COMPOSE) run --rm --no-deps -it cliproxyapi ./CLIProxyAPI -config /CLIProxyAPI/config.yaml -codex-device-login
	@$(COMPOSE) restart cliproxyapi
	@echo "Verify with: make models"

claude-proxy-install: ## Build claude-max-proxy from the vendored sources and run it as a systemd user service (re-run after editing .env)
	@bash scripts/claude-max-proxy/install.sh

claude-proxy-update: ## Refresh vendor/claude-max-api-proxy from upstream if reachable (else keep the vendored copy), rebuild, restart
	@bash scripts/claude-max-proxy/sync-upstream.sh
	@bash scripts/claude-max-proxy/install.sh

auth-claude-proxy: ## Log the Claude Max subscription into claude-max-proxy (token stored in .env, service restarted)
	@bash scripts/auth-claude-proxy.sh

claude-proxy-restart: ## Restart the claude-max-proxy service
	@systemctl --user restart claude-max-proxy.service && systemctl --user --no-pager --lines=0 status claude-max-proxy.service

claude-proxy-logs: ## Tail claude-max-proxy logs
	@journalctl --user -u claude-max-proxy.service -n 200 -f

claude-cli-update: ## Update the Claude Code CLI itself (`claude update`); the proxy is claude-proxy-update
	@export PATH="$$HOME/.local/bin:$$PATH"; \
	 command -v claude >/dev/null || { echo "claude CLI not found (make claude-proxy-install installs it)" >&2; exit 1; }; \
	 claude update; claude --version

models: ## List models exposed by both proxies (distinguishes down from empty)
	@bash scripts/models.sh

cliproxyapi-logs: ## Last 200 CLIProxyAPI container log lines (LINES=<n> to override)
	@$(call tail_lines,200); $(COMPOSE) logs --tail="$$n" cliproxyapi

cliproxyapi-logs-follow: ## Follow CLIProxyAPI container logs from the last 200 lines (LINES=<n> to override)
	@$(call tail_lines,200); $(COMPOSE) logs -f --tail="$$n" cliproxyapi

##@ Hermes

hermes-install: ## Install Hermes on this host and wire it to Discord, Honcho, both proxies, Browser Use
	@bash scripts/hermes-install.sh

# Re-apply .env to ~/.hermes: config.yaml, SOUL.md and honcho.json are re-rendered
# (.bak copies kept), skills and plugins refreshed. The running gateway keeps the
# old config until make hermes-restart.
hermes-config-apply: ## Apply .env changes: HERMES_SKIP_GATEWAY=1 bash scripts/hermes-install.sh --force; no gateway restart (then make hermes-restart)
	@HERMES_SKIP_GATEWAY=1 bash scripts/hermes-install.sh --force

hermes-restart: ## Restart the Hermes gateway (Discord bot)
	@hermes gateway restart

hermes-logs: ## Tail Hermes gateway logs
	@tail -n 200 -f "$${HERMES_HOME:-$$HOME/.hermes}/logs/gateway.log"

hermes-profile: ## Create/refresh a Hermes profile and sync its books from data/knowledge/<NAME> (NAME=marketing [KNOWLEDGE_ROOT=/mnt/books/knowledge])
	@test -n "$(NAME)" || { echo "usage: make hermes-profile NAME=<profile> [KNOWLEDGE_ROOT=<dir>]"; exit 2; }
	@bash scripts/hermes-profile.sh "$(NAME)"

hermes-ui: ## Start the Hermes dashboard (config/keys web UI) on 127.0.0.1 (HERMES_UI_PORT, default 9119)
	@bash scripts/hermes-ui.sh start

hermes-ui-status: ## Show whether the Hermes dashboard is running
	@bash scripts/hermes-ui.sh status

hermes-ui-stop: ## Stop the Hermes dashboard
	@bash scripts/hermes-ui.sh stop

##@ Health and routing

doctor: ## Check the whole stack (containers, both proxies, Honcho, Hermes)
	@bash scripts/doctor.sh

honcho-health: ## Check the Honcho API
	@curl -fsS http://127.0.0.1:$(or $(call envval,HONCHO_PORT),8000)/health && echo

route-smoke: ## Read-only routing smoke test: probe /v1/models, degrade-check tiers, run the canary
	@bash scripts/route-smoke.sh

router-logs: ## Last 20 model_router lines from the Hermes agent log (LINES=<n> to override)
	@bash scripts/router-logs.sh snapshot

router-logs-follow: ## Recent model_router lines, then follow live (survives log rotation)
	@bash scripts/router-logs.sh follow

# Local model roles (routine qwen3.5 / fallback qwen3.6 / agent qwen3.8 by
# default; docs/model-routing.md, Local model roles). All read-only: metadata
# calls go only to the configured local endpoint; nothing is restarted.
local-route-status: ## Read-only: local roles vs endpoint, native fallback chains + aux routes as Hermes resolves them, privacy, specialist knobs
	@PATH="$$HOME/.local/bin:$$PATH" python3 scripts/local-route.py status

local-models: ## Read-only: models on the local Ollama endpoint (capabilities, size, loaded, role) [JSON=1 BASE_URL=...]
	@args=(catalog); \
	 [[ "$${JSON:-0}" == "1" ]] && args+=(--json); \
	 [[ -n "$${BASE_URL:-}" ]] && args+=(--base-url "$$BASE_URL"); \
	 python3 scripts/local-route.py "$${args[@]}"

local-route-readback: ## Read-only: live config vs what hermes-install.sh --force renders now (Hermes' own resolver) + local-only invariants; exit 1 on drift
	@python3 scripts/local-route.py readback

specialist-classify-probe: ## Dry-run the hybrid specialist router with the LIVE classifier for TASK="..." (content-free trace; nothing dispatched) [CLASSIFIER=local|cliproxy NO_CALL=1 MODEL= FALLBACK_MODEL= TIMEOUT=]
	@args=(probe); \
	 [[ -n "$${CLASSIFIER:-}" ]] && args+=(--classifier "$$CLASSIFIER"); \
	 [[ "$${NO_CALL:-0}" == "1" ]] && args+=(--no-call); \
	 [[ -n "$${MODEL:-}" ]] && args+=(--model "$$MODEL"); \
	 [[ -n "$${FALLBACK_MODEL+x}" ]] && args+=(--fallback-model "$$FALLBACK_MODEL"); \
	 [[ -n "$${TIMEOUT:-}" ]] && args+=(--timeout "$$TIMEOUT"); \
	 python3 scripts/local-route.py "$${args[@]}"

##@ Specialist routing

specialist-route: ## Dry-run the specialist router for TASK="..." or TASK=- < prompt.txt (offline; PLAN=1 shows the note; no TASK prints usage)
	@python3 scripts/specialist-route.py $(if $(PLAN),--plan) $(if $(REQUIRE_READY),--require-ready)

specialist-logs: ## Last 20 live specialist-routing decisions from the Hermes agent log (content-free; LINES=<n>)
	@bash scripts/router-logs.sh snapshot specialists

specialist-logs-follow: ## Recent specialist-routing decisions, then follow live (survives log rotation)
	@bash scripts/router-logs.sh follow specialists

# Activation: install = plugin + specialist_* keys via `hermes config set`, read
# back, never restarts. enable = install + a guarded gateway restart (asks; YES=1
# on the make command line skips the question; refused inside Hermes or an
# agent it runs) + a check that the router registered with specialists=on.
specialist-status: ## Read-only: plugin vs repo, specialist_* settings, gateway, restart pending, knowledge readiness
	@PATH="$$HOME/.local/bin:$$PATH" python3 scripts/specialist-activate.py status

specialist-install: ## Install the router plugin + set specialist_routing on (hermes config set, read back); NO restart [MAX=4 REQUIRE_READY=0|1 CLASSIFIER=none|local|cliproxy PROFILES=a,b|all DRY_RUN=1]
	@args=(install); \
	 [[ -n "$${MAX:-}" ]] && args+=(--max "$$MAX"); \
	 [[ "$${REQUIRE_READY:-}" == "1" ]] && args+=(--require-ready); \
	 [[ "$${REQUIRE_READY:-}" == "0" ]] && args+=(--no-require-ready); \
	 [[ -n "$${CLASSIFIER:-}" ]] && args+=(--classifier "$$CLASSIFIER"); \
	 [[ -n "$${PROFILES+x}" ]] && args+=(--profiles "$$PROFILES"); \
	 [[ "$${DRY_RUN:-0}" == "1" ]] && args+=(--dry-run); \
	 PATH="$$HOME/.local/bin:$$PATH" python3 scripts/specialist-activate.py "$${args[@]}"

specialist-enable: ## specialist-install, then restart the gateway (asks; YES=1 skips; refused inside Hermes) and verify specialists=on [same vars, WAIT=90]
	@args=(enable); \
	 [[ -n "$${MAX:-}" ]] && args+=(--max "$$MAX"); \
	 [[ "$${REQUIRE_READY:-}" == "1" ]] && args+=(--require-ready); \
	 [[ "$${REQUIRE_READY:-}" == "0" ]] && args+=(--no-require-ready); \
	 [[ -n "$${CLASSIFIER:-}" ]] && args+=(--classifier "$$CLASSIFIER"); \
	 [[ -n "$${PROFILES+x}" ]] && args+=(--profiles "$$PROFILES"); \
	 [[ "$${DRY_RUN:-0}" == "1" ]] && args+=(--dry-run); \
	 [[ "$(if $(filter command line,$(origin YES)),$(YES))" == "1" ]] && args+=(--yes); \
	 [[ -n "$${WAIT:-}" ]] && args+=(--wait "$$WAIT"); \
	 PATH="$$HOME/.local/bin:$$PATH" python3 scripts/specialist-activate.py "$${args[@]}"

specialist-disable: ## Set specialist_routing: false (read back); NO restart: then make hermes-restart [DRY_RUN=1]
	@args=(disable); \
	 [[ "$${DRY_RUN:-0}" == "1" ]] && args+=(--dry-run); \
	 PATH="$$HOME/.local/bin:$$PATH" python3 scripts/specialist-activate.py "$${args[@]}"

##@ Knowledge

knowledge-convert: ## Copy-convert EPUB/PDF books to Markdown (SRC=/mnt/books/edu DEST=data/knowledge/<profile> [ARGS="--dry-run 'Book folder'"])
	@test -n "$(SRC)" -a -n "$(DEST)" || { echo "usage: make knowledge-convert SRC=<book dir> DEST=<output dir> [ARGS=...]"; exit 2; }
	@python3 hermes/skills/document-converter/scripts/doc2md.py --source "$(SRC)" --dest "$(DEST)" $(ARGS)

knowledge-stage: ## Build data/knowledge/<NAME> from hermes/profiles/<NAME>/knowledge-sources.txt (NAME=strategy [BOOKS_ROOT=/mnt/books KNOWLEDGE_CONVERT=0])
	@test -n "$(NAME)" || { echo "usage: make knowledge-stage NAME=<profile> [BOOKS_ROOT=<dir>]"; exit 2; }
	@bash scripts/knowledge-stage.sh "$(NAME)"

knowledge-refresh: ## After adding books: stage + sync every book-backed profile (or NAME=<profile>) and report updated/current/missing/uncategorized
	@bash scripts/knowledge-refresh.sh $(NAME)

knowledge-inventory: ## Read-only: library vs. book lists by content: uncategorized, already imported (source not linked), missing sources
	@bash scripts/knowledge-refresh.sh --inventory $(NAME)

# Values reach the script through the environment (command-line make variables
# are exported), so book paths with spaces or quotes are passed through intact.
# APPLY counts only when given on the make command line, never from a stray
# exported shell variable.
knowledge-classify: ## AI proposals for uncategorized books (dry-run; already-staged books are reported, never re-classified). APPLY=1 assigns guardrail-clean ones; APPROVE='<profile>=<source>' / APPROVALS=<file> [APPROVED_ONLY=1 SOURCE=<path> CLASSIFIER=local METADATA_ONLY=1 LIMIT=n]
	@args=(); \
	 [[ -n "$${SOURCE:-}" ]] && args+=("$$SOURCE"); \
	 [[ "$(if $(filter command line,$(origin APPLY)),$(APPLY))" == "1" ]] && args+=(--apply); \
	 [[ "$${APPROVED_ONLY:-0}" == "1" ]] && args+=(--approved-only); \
	 [[ "$${METADATA_ONLY:-0}" == "1" ]] && args+=(--metadata-only); \
	 [[ -n "$${APPROVE:-}" ]] && args+=(--approve "$$APPROVE"); \
	 [[ -n "$${APPROVALS:-}" ]] && args+=(--approvals "$$APPROVALS"); \
	 [[ -n "$${CLASSIFIER:-}" ]] && args+=(--classifier "$$CLASSIFIER"); \
	 [[ -n "$${LIMIT:-}" ]] && args+=(--limit "$$LIMIT"); \
	 python3 scripts/knowledge-classify.py "$${args[@]}"

# Proposal only by default. APPLY counts only on the make command line and only
# together with explicit approvals; it appends book-list lines, nothing else.
knowledge-reconcile: ## Proposal only: staged knowledge with no book-list source, library copies of staged books. APPLY=1 APPROVE='<profile>=<library path>' / APPROVALS=<file> appends approved lines [SCOPE=<library path>]
	@args=(); \
	 [[ "$(if $(filter command line,$(origin APPLY)),$(APPLY))" == "1" ]] && args+=(--apply); \
	 [[ -n "$${APPROVE:-}" ]] && args+=(--approve "$$APPROVE"); \
	 [[ -n "$${APPROVALS:-}" ]] && args+=(--approvals "$$APPROVALS"); \
	 [[ -n "$${SCOPE:-}" ]] && args+=(--scope "$$SCOPE"); \
	 python3 scripts/knowledge-reconcile.py "$${args[@]}"

# One bounded headless /learn turn per knowledge file into the exact skill
# <profile>-knowledge; state, logs and a lock live in the profile's
# knowledge-learn/ folder, so an interrupted run resumes where it stopped.
knowledge-learn: ## Learn synced books into <NAME>-knowledge, one file per Hermes run, resumable (NAME=<profile>|all [TIMEOUT=1800 MAX_TURNS=200 LIMIT=n JOBS=1 DRY_RUN=1 FORCE=1 RETRY=1])
	@test -n "$(NAME)" || { echo "usage: make knowledge-learn NAME=<profile>|all [TIMEOUT=s LIMIT=n JOBS=n DRY_RUN=1]   (status: make knowledge-learn-status)"; exit 2; }
	@args=($(NAME)); \
	 [[ -n "$${TIMEOUT:-}" ]] && args+=(--timeout "$$TIMEOUT"); \
	 [[ -n "$${MAX_TURNS:-}" ]] && args+=(--max-turns "$$MAX_TURNS"); \
	 [[ -n "$${LIMIT:-}" ]] && args+=(--limit "$$LIMIT"); \
	 [[ -n "$${JOBS:-}" ]] && args+=(--jobs "$$JOBS"); \
	 [[ "$${DRY_RUN:-0}" == "1" ]] && args+=(--dry-run); \
	 [[ "$${FORCE:-0}" == "1" ]] && args+=(--force); \
	 [[ "$${RETRY:-0}" == "1" ]] && args+=(--retry); \
	 PATH="$$HOME/.local/bin:$$PATH" python3 scripts/knowledge-learn.py "$${args[@]}"

knowledge-learn-status: ## Read-only: raw synced knowledge vs. learned <profile>-knowledge skill per profile (NAME=<profile> [JSON=1])
	@args=(--status $(NAME)); \
	 [[ "$${JSON:-0}" == "1" ]] && args+=(--json); \
	 python3 scripts/knowledge-learn.py "$${args[@]}"

##@ Windows VM

windows-vm-network: ## Publish CLIProxyAPI (:8317) + Honcho (:8000) to the Windows VM over Tailscale Serve (Docker stays on 127.0.0.1)
	@bash scripts/windows-vm-network.sh apply

windows-vm-network-status: ## Verify the loopback services and the two Tailscale Serve forwarders for the Windows VM
	@bash scripts/windows-vm-network.sh status

windows-vm-network-off: ## Remove only the two Tailscale Serve forwarders (unrelated Serve/Funnel config is left intact)
	@bash scripts/windows-vm-network.sh off

##@ Tests (offline)

test-router: ## Run the model-router unit + zero-egress privacy canary suite (offline)
	@python3 -m unittest discover -s scripts/tests -p 'test_*.py'

test-local-route: ## Run the local-role / hybrid classifier / config-render tests (temp HERMES_HOME, stub endpoint; offline)
	@python3 -m unittest discover -s scripts/tests -p 'test_local_route.py'

test-private-honcho: ## Run the #private marker / private Honcho validator tests (offline, explicit env; no live Honcho)
	@python3 -m unittest discover -s scripts/tests -p 'test_private_honcho.py'

test-specialists: ## Run only the specialist-router tests (offline, temp fixtures)
	@python3 -m unittest discover -s scripts/tests -p 'test_specialist*.py'

test-specialist-activate: ## Run only the specialist activation tests (stub hermes/systemctl, temp HERMES_HOME; offline)
	@python3 -m unittest discover -s scripts/tests -p 'test_specialist_activate.py'

test-knowledge-learn: ## Run the knowledge-learn tests (temp HERMES_HOME, stub hermes and /learn prompt; offline)
	@python3 -m unittest discover -s scripts/tests -p 'test_knowledge_learn.py'

test-knowledge-refresh: ## Run the knowledge-refresh tests (temp library, profiles, HERMES_HOME; offline)
	@bash scripts/test_knowledge_refresh.sh

test-knowledge-classify: ## Run the knowledge-classify/reconcile/match tests (synthetic library, stub classifier server; offline)
	@python3 -m unittest discover -s scripts/tests -p 'test_knowledge_*.py'

test-knowledge-stage: ## Run the knowledge-stage tests (temp library, stub converter, offline)
	@bash scripts/test_knowledge_stage.sh

test-document-converter: ## Run the document-converter tests (synthetic EPUB/PDF fixtures, offline)
	@python3 -m unittest discover -s scripts/tests -p 'test_document_converter.py'
