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
	    browseros-neo-check browseros-neo-up browseros-neo-down browseros-neo-status browseros-neo-pause browseros-neo-resume browseros-neo-access browseros-neo-hermes-enable browseros-neo-hermes-disable test-browseros-neo \
	    marketing-video-install marketing-video-render marketing-video-gates test-marketing-video test-rtk-pilot ui-audit-install ui-audit test-ui-audit-pilot \
	    hermes-install hermes-config-apply hermes-dev-workflow-plan hermes-dev-workflow-apply hermes-profile knowledge-convert knowledge-stage knowledge-refresh knowledge-inventory knowledge-classify knowledge-benchmark knowledge-apply knowledge-legacy-report knowledge-reconcile knowledge-learn knowledge-learn-status test-knowledge-learn test-knowledge-stage test-knowledge-refresh test-knowledge-classify test-knowledge-apply test-document-converter hermes-restart hermes-logs hermes-ui hermes-ui-status hermes-ui-stop workflow-status workflow-gate test-kanban-workflow test-apply-dev-workflow hermes-runtime-patch-status hermes-runtime-patch-apply test-hermes-runtime-patch hermes-devtest-status hermes-devtest-install test-devtest-runner capability-validate capability-plan capability-select capability-compat capability-activation-check test-capabilities clean

# Rendered by scripts/make-help.py: `##@ Title` starts a section, `##> text`
# is a section note (one paragraph per line), `##| text` lines just above a
# target are wrapped under its entry, `target: ## text` is the summary.
# Color only on a terminal; COLOR=1 forces it, COLOR=0 / NO_COLOR=1 / TERM=dumb
# turn it off. HELP_WIDTH=<n> sets the wrap width.
help: ## Show this help (COLOR=0|1, NO_COLOR=1, HELP_WIDTH=<n>)
	@COLOR='$(COLOR)' NO_COLOR='$(NO_COLOR)' HELP_WIDTH='$(HELP_WIDTH)' python3 scripts/make-help.py $(MAKEFILE_LIST)

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

# Narrow rollout of the dev workflow: only the kanban-workflow skill files and the
# coder profile SOUL.md (when that profile exists), with backups and atomic
# replaces. No config.yaml/.env/honcho/plugins/memories; config keys go through
# `hermes config set` separately (docs/hermes-autonomous-development.md).
hermes-dev-workflow-plan: ## Show what the narrow dev-workflow rollout would copy (kanban-workflow skill + coder SOUL.md); writes nothing [CODER_HOME=<dir>]
	@python3 scripts/apply-dev-workflow.py --plan $(if $(CODER_HOME),--coder-home "$(CODER_HOME)",)

hermes-dev-workflow-apply: ## Copy the kanban-workflow skill + coder SOUL.md into ~/.hermes (backups, atomic, no config, no restart) [CODER_HOME=<dir>]
	@python3 scripts/apply-dev-workflow.py --apply $(if $(CODER_HOME),--coder-home "$(CODER_HOME)",)

hermes-restart: ## Restart the Hermes gateway (Discord bot)
	@hermes gateway restart

hermes-runtime-patch-status: ## Per kit runtime patch (hermes/runtime-patches): applied | applicable | conflict | ambiguous | blocked | dirty | unsafe in ~/.hermes/hermes-agent [SRC=<dir>]
	@bash scripts/hermes-runtime-patch.sh status $(if $(SRC),--src "$(SRC)",)

hermes-runtime-patch-apply: ## Apply the applicable kit runtime patches to an ISOLATED checkout SRC=<dir> (installed runtime refused; unexplained local changes on targets refused; a partly applied set resumes)
	@bash scripts/hermes-runtime-patch.sh apply $(if $(SRC),--src "$(SRC)",)

hermes-devtest-status: ## Trusted Go/PostgreSQL dev-test runner: installed vs kit digest, root ownership, pinned SHA-256 (docs/dev-test-runner.md)
	@bash scripts/install-hermes-devtest.sh status

hermes-devtest-install: ## Validate, then sudo-install the root-owned dev-test runner + profile file that pins its digest (asks for the sudo password; no restart)
	@bash scripts/install-hermes-devtest.sh install

test-apply-dev-workflow: ## Run the narrow dev-workflow rollout helper tests (temp Hermes homes; never ~/.hermes)
	@python3 -m unittest discover -s scripts/tests -p 'test_apply_dev_workflow.py'

test-hermes-runtime-patch: ## Run the runtime-patch helper tests (throwaway git checkout and fixture patches; never ~/.hermes)
	@bash scripts/test_hermes_runtime_patch.sh

test-devtest-runner: ## Run the dev-test runner tests (synthetic Go module + canary credential in the real Landlock/seccomp sandbox; never the operator's database)
	@python3 -m unittest discover -s scripts/tests -p 'test_devtest_runner.py'

hermes-logs: ## Tail Hermes gateway logs
	@tail -n 200 -f "$${HERMES_HOME:-$$HOME/.hermes}/logs/gateway.log"

workflow-status: ## Show every card of a Discord->Kanban workflow and its state: rounds, SHAs, lane evidence, review (ID=t_xxxx)
	@test -n "$(ID)" || { echo "usage: make workflow-status ID=<any card id of the request>"; exit 2; }
	@python3 hermes/skills/kanban-workflow/scripts/workflow.py status "$(ID)"

workflow-gate: ## Check the workflow gate: exit 0 iff the final SHA passed every lane and an independent review (ID=t_xxxx)
	@test -n "$(ID)" || { echo "usage: make workflow-gate ID=<report card id>"; exit 2; }
	@python3 hermes/skills/kanban-workflow/scripts/workflow.py gate "$(ID)"

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

specialist-classify-probe: ## Dry-run the specialist router with the LIVE classifier for TASK="..." (AI selection in mode ai; content-free trace; nothing dispatched) [MODE=ai|hybrid|keywords CLASSIFIER=local|cliproxy NO_CALL=1 MODEL= FALLBACK_MODEL= TIMEOUT=]
	@args=(probe); \
	 [[ -n "$${MODE:-}" ]] && args+=(--mode "$$MODE"); \
	 [[ -n "$${CLASSIFIER:-}" ]] && args+=(--classifier "$$CLASSIFIER"); \
	 [[ "$${NO_CALL:-0}" == "1" ]] && args+=(--no-call); \
	 [[ -n "$${MODEL:-}" ]] && args+=(--model "$$MODEL"); \
	 [[ -n "$${FALLBACK_MODEL+x}" ]] && args+=(--fallback-model "$$FALLBACK_MODEL"); \
	 [[ -n "$${TIMEOUT:-}" ]] && args+=(--timeout "$$TIMEOUT"); \
	 python3 scripts/local-route.py "$${args[@]}"

##@ Specialist routing

specialist-route: ## Offline keyword dry-run of the specialist router for TASK="..." or TASK=- < prompt.txt (no model; PLAN=1 shows the note; MODE=ai shows the gate; live AI: specialist-classify-probe)
	@python3 scripts/specialist-route.py $(if $(PLAN),--plan) $(if $(REQUIRE_READY),--require-ready) $(if $(MODE),--mode $(MODE))

specialist-logs: ## Last 20 live specialist-routing decisions from the Hermes agent log (content-free; LINES=<n>)
	@bash scripts/router-logs.sh snapshot specialists

specialist-logs-follow: ## Recent specialist-routing decisions, then follow live (survives log rotation)
	@bash scripts/router-logs.sh follow specialists

# Activation: install = plugin + specialist_* keys via `hermes config set`, read
# back, never restarts. enable = install + a guarded gateway restart (asks; YES=1
# on the make command line skips the question; refused inside Hermes or an
# agent it runs) + a check that the router registered with specialists=on.
specialist-status: ## Read-only: plugin vs repo, specialist_* settings (incl. selection mode), gateway, restart pending, knowledge readiness
	@PATH="$$HOME/.local/bin:$$PATH" python3 scripts/specialist-activate.py status

specialist-install: ## Install the router plugin + set specialist_routing on (hermes config set, read back); NO restart [MAX=0-15 MODE=ai|hybrid|keywords REQUIRE_READY=0|1 CLASSIFIER=none|local|cliproxy PROFILES=a,b|all DRY_RUN=1]
	@args=(install); \
	 [[ -n "$${MAX:-}" ]] && args+=(--max "$$MAX"); \
	 [[ -n "$${MODE:-}" ]] && args+=(--mode "$$MODE"); \
	 [[ "$${REQUIRE_READY:-}" == "1" ]] && args+=(--require-ready); \
	 [[ "$${REQUIRE_READY:-}" == "0" ]] && args+=(--no-require-ready); \
	 [[ -n "$${CLASSIFIER:-}" ]] && args+=(--classifier "$$CLASSIFIER"); \
	 [[ -n "$${PROFILES+x}" ]] && args+=(--profiles "$$PROFILES"); \
	 [[ "$${DRY_RUN:-0}" == "1" ]] && args+=(--dry-run); \
	 PATH="$$HOME/.local/bin:$$PATH" python3 scripts/specialist-activate.py "$${args[@]}"

specialist-enable: ## specialist-install, then restart the gateway (asks; YES=1 skips; refused inside Hermes) and verify specialists=on [same vars incl. MODE=, WAIT=90]
	@args=(enable); \
	 [[ -n "$${MAX:-}" ]] && args+=(--max "$$MAX"); \
	 [[ -n "$${MODE:-}" ]] && args+=(--mode "$$MODE"); \
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

capability-validate: ## Validate the capability registry, source lock, schemas and synthetic company/project examples (read-only; no install/runtime claim)
	@python3 scripts/capability_registry.py validate

capability-plan: ## Read-only capability plan for TASK=<envelope.json> and SELECTION=<selection.json> or CAPS="id id" (explicit offline); FORMAT=summary
	@test -n "$(TASK)" -a \( -n "$(SELECTION)" -o -n "$(CAPS)" \) || { echo "usage: make capability-plan TASK=task.json SELECTION=selection.json | CAPS=\"pm.lean-prd ...\" [FORMAT=summary]"; exit 2; }
	@python3 scripts/capability-plan.py --task "$(TASK)" $(if $(SELECTION),--selection "$(SELECTION)") $(foreach c,$(CAPS),--capability $(c)) $(if $(FORMAT),--format $(FORMAT))

capability-select: ## Read-only capability selection through the model-router seam for TASK=<envelope.json> TEXT=<prompt file|-> with FIXTURE=<cases.json> (offline) or LIVE=1 (configured classifier)
	@test -n "$(TASK)" -a -n "$(TEXT)" -a \( -n "$(FIXTURE)" -o -n "$(LIVE)" \) || { echo "usage: make capability-select TASK=task.json TEXT=prompt.txt FIXTURE=cases.json | LIVE=1"; exit 2; }
	@python3 scripts/capability-select.py --task "$(TASK)" --text-file "$(TEXT)" $(if $(FIXTURE),--fixture "$(FIXTURE)",--live)

capability-compat: ## Read-only: capability skills vs the installed Hermes skill rules (source parsed as text), name collisions, model-router drift
	@h="$${HERMES_HOME:-$$HOME/.hermes}"; python3 scripts/capability_compat.py --hermes-agent "$${HERMES_AGENT_DIR:-$$h/hermes-agent}" --skills-root "$$h/skills" --installed-plugin "$$h/plugins/model-router"

capability-activation-check: ## Read-only: the committed activation manifest equals the one computed from source (docs/capability-activation.md)
	@python3 scripts/capability_activation.py check

##@ Knowledge
##> Flow: inventory (what is new) -> classify (writes the reviewed plan plan-<report>.json with complete new-profile specs, plus <report>.review.md and the offline <report>.review.html workspace to read, filter, edit and export the plan) -> knowledge-apply PLAN=<plan> DRY_RUN=1 (report: ready / pending review / pending profile approval / pending error / pending stale for risk labels from an earlier release: regenerate the plan) -> edit/review the plan if needed (decide assign/skip/existing, adjust any new profile spec) -> knowledge-apply PLAN=<plan> APPROVE=1 APPROVE_ALL=<plan id> [SKIP_ERRORS=1] (one command: approve proposals, create profiles, canonical copy once, references, install/sync, learn) -> knowledge-learn-status
##> Legacy flow (still works): classify APPLY=1 / reconcile -> refresh (stage + sync) -> learn. Books assigned through a plan are stored once under data/knowledge-canonical and referenced from each profile's knowledge/refs/ (no duplicate Markdown); refresh never copies them again.
##> Ownership: one canonical profile per book. A second profile only by explicit approval (SECONDARY='<profile>=<source>' SECONDARY_REASON='...', or secondary_reason in the plan) for a substantial, recurring, independent purpose. Books already in several lists are reported, never changed.
##> Library: /mnt/books is read-only; nothing here writes, moves or deletes a book.

##| Read-only. Compares the library (/mnt/books) with every profile book list
##| (hermes/profiles/<p>/knowledge-sources.txt) and reports, by text content:
##| books no list includes, books already imported under another path, and
##| book-list lines missing from the library. Writes and converts nothing.
knowledge-inventory: ## Read-only: library vs. book lists by content: uncategorized, already imported (source not linked), missing sources
	@bash scripts/knowledge-refresh.sh --inventory $(NAME)

##| Manual converter. SRC and DEST are both required (usage error otherwise).
##| Converts the eligible EPUB/PDF files under SRC only (or the named paths in
##| ARGS) into Markdown under DEST. It does NOT find and convert every book under
##| /mnt/books; for profile books use knowledge-stage or knowledge-refresh, which
##| convert automatically. Copy-only; unchanged books are skipped as up-to-date.
knowledge-convert: ## Convert EPUB/PDF under SRC to Markdown in DEST; SRC and DEST required (SRC=/mnt/books/edu DEST=data/knowledge/<profile> [ARGS="--dry-run 'Book folder'"])
	@test -n "$(SRC)" -a -n "$(DEST)" || { echo "usage: make knowledge-convert SRC=<book dir> DEST=<output dir> [ARGS=...]"; exit 2; }
	@python3 hermes/skills/document-converter/scripts/doc2md.py --source "$(SRC)" --dest "$(DEST)" $(ARGS)

##| Builds data/knowledge/<NAME> from that profile's book list only. EPUB/PDF
##| books listed there are converted to Markdown automatically by default
##| (KNOWLEDGE_CONVERT=1; set 0 to copy only .md/.txt). Does not sync into
##| Hermes; run hermes-profile next (or use knowledge-refresh for both).
knowledge-stage: ## Build data/knowledge/<NAME> from hermes/profiles/<NAME>/knowledge-sources.txt (NAME=strategy [BOOKS_ROOT=/mnt/books KNOWLEDGE_CONVERT=0])
	@test -n "$(NAME)" || { echo "usage: make knowledge-stage NAME=<profile> [BOOKS_ROOT=<dir>]"; exit 2; }
	@bash scripts/knowledge-stage.sh "$(NAME)"

##| Runs knowledge-stage (with the same automatic EPUB/PDF conversion of listed
##| books) and then syncs into each installed profile, for every profile with a
##| book list or just NAME. Books no list includes are reported, not converted.
##| Does not run knowledge-learn.
knowledge-refresh: ## After adding books: stage + sync every book-backed profile (or NAME=<profile>) and report updated/current/missing/uncategorized
	@bash scripts/knowledge-refresh.sh $(NAME)

# Values reach the script through the environment (command-line make variables
# are exported), so book paths with spaces or quotes are passed through intact.
# APPLY counts only when given on the make command line, never from a stray
# exported shell variable.
##| Asks a model which profile each uncategorized book belongs to and prints
##| proposals (dry-run). Also writes the reviewed plan plan-<report>.json: one
##| editable item per book (content hash, proposal, decision assign/skip/
##| existing/review) and any new-profile proposal (never created here); apply
##| it with knowledge-apply. Legacy: APPLY=1 appends approved or guardrail-clean
##| book-list lines and stages a full copy (run knowledge-refresh afterwards).
##| Policy: one canonical profile per book; an approval for a book whose text
##| another profile already has is refused unless given as
##| SECONDARY='<profile>=<source>' with SECONDARY_REASON (the substantial,
##| recurring, independent purpose). Taxonomy: existing broad profiles first;
##| overlapping new-profile proposals are folded into one broad family per run
##| (hermes/knowledge-taxonomy.json, KNOWLEDGE_TAXONOMY=<file>|off); rerun
##| the same command to get consolidated proposals.
knowledge-classify: ## AI proposals + reviewed plan, Markdown review and offline HTML workspace for uncategorized books (dry-run). Then: knowledge-apply PLAN=<plan>. [MODEL=gpt-6.1-terra EFFORT=medium|high SOURCE=<path> CLASSIFIER=local METADATA_ONLY=1 LIMIT=n] Legacy: APPLY=1, APPROVE='<profile>=<source>' / APPROVALS=<file> [APPROVED_ONLY=1]
	@args=(); \
	 [[ -n "$${SOURCE:-}" ]] && args+=("$$SOURCE"); \
	 [[ "$(if $(filter command line,$(origin APPLY)),$(APPLY))" == "1" ]] && args+=(--apply); \
	 [[ "$${APPROVED_ONLY:-0}" == "1" ]] && args+=(--approved-only); \
	 [[ "$${METADATA_ONLY:-0}" == "1" ]] && args+=(--metadata-only); \
	 [[ -n "$${APPROVE:-}" ]] && args+=(--approve "$$APPROVE"); \
	 [[ -n "$${APPROVALS:-}" ]] && args+=(--approvals "$$APPROVALS"); \
	 [[ -n "$${SECONDARY:-}" ]] && args+=(--secondary "$$SECONDARY"); \
	 [[ -n "$${SECONDARY_REASON:-}" ]] && args+=(--secondary-reason "$$SECONDARY_REASON"); \
	 [[ -n "$${CLASSIFIER:-}" ]] && args+=(--classifier "$$CLASSIFIER"); \
	 [[ -n "$${MODEL:-}" ]] && args+=(--model "$$MODEL"); \
	 [[ -n "$${EFFORT:-}" ]] && args+=(--effort "$$EFFORT"); \
	 [[ -n "$${LIMIT:-}" ]] && args+=(--limit "$$LIMIT"); \
	 python3 scripts/knowledge-classify.py "$${args[@]}"

# Benchmark artifacts go under OUT only (default: a timestamped folder in the
# gitignored staging area). The runner imports the classifier's prompt and
# evidence builders but has no code path that touches book lists, profiles or
# data/knowledge. Values reach the script through the environment.
##| Compares CLIProxyAPI models and reasoning efforts on the classification
##| prompt against a versioned rubric (docs/benchmarks/knowledge-classify/
##| rubric-v1.json, built from the operator-reviewed plan). CATALOG=build
##| (default when the catalog is missing) precomputes the exact production
##| evidence from the library once; every cell then gets identical requests.
##| Writes results.json (raw per-request metadata), scores.json and report.md
##| under OUT. Never writes a plan, a manifest or a profile.
knowledge-benchmark: ## Benchmark classifier models x efforts against the rubric; writes results/scores/report under OUT only [RUBRIC=<json> CATALOG=<json>|build OUT=<dir> MODELS=a,b EFFORTS=medium,high REPEATS=2 CONCURRENCY=4 TIMEOUT=120 SOURCES=<n>|stratified]
	@args=(run --rubric "$${RUBRIC:-docs/benchmarks/knowledge-classify/rubric-v1.json}"); \
	 [[ -n "$${OUT:-}" ]] && args+=(--out "$$OUT"); \
	 [[ -n "$${CATALOG:-}" ]] && args+=(--catalog "$$CATALOG"); \
	 [[ -n "$${MODELS:-}" ]] && args+=(--models "$$MODELS"); \
	 [[ -n "$${EFFORTS:-}" ]] && args+=(--efforts "$$EFFORTS"); \
	 [[ -n "$${REPEATS:-}" ]] && args+=(--repeats "$$REPEATS"); \
	 [[ -n "$${CONCURRENCY:-}" ]] && args+=(--concurrency "$$CONCURRENCY"); \
	 [[ -n "$${TIMEOUT:-}" ]] && args+=(--timeout "$$TIMEOUT"); \
	 [[ -n "$${SOURCES:-}" ]] && args+=(--sources "$$SOURCES"); \
	 python3 scripts/knowledge-benchmark.py "$${args[@]}"

# APPROVE and APPROVE_ALL count only when given on the make command line, never
# from a stray exported shell variable. The plan path reaches the script through
# the environment so paths with spaces or quotes stay intact.
##| Applies one reviewed plan in one run. DRY_RUN=1 on an unedited classifier
##| plan is a report, not a wall of errors: ready (assign/skip/existing),
##| pending review (a proposed existing profile), pending profile approval (a
##| new-profile proposal), pending error (classifier error, no target); only
##| structural problems (stale hash, bad name) are errors. APPROVE_ALL=1 is the
##| explicit bulk approval: on a copy of the plan (the file is never edited)
##| pending review -> assign, complete new-profile proposals -> approved and
##| assigned; errors stay pending unless SKIP_ERRORS=1 skips them. Preview it
##| with DRY_RUN=1; the preview prints APPROVE_ALL=<plan id>, which binds the
##| apply to that exact file. Receipt: apply/<plan id>/approval.json.
##| Steps: validate (every executable item decided; approved specs complete;
##| source hashes unchanged; safe names) -> create approved new profiles
##| from hermes/profiles/_template -> append book lists + knowledge-assignments
##| .json -> copy/convert each unique book ONCE into data/knowledge-canonical ->
##| write tiny knowledge/refs/*.ref.md per profile (no duplicate Markdown) ->
##| install or sync the profiles -> knowledge-learn (resumable) -> report +
##| checkpoint under data/knowledge-canonical/apply/<plan id>/. Any refusal
##| stops before a write; a failed step stops nonzero and prints the resume
##| command (same plan; verified work is skipped). Legacy full copies in a
##| profile are reported, never migrated or deleted.
knowledge-apply: ## Apply a reviewed plan in one command: validate -> new profiles -> assign -> canonical copy once -> references -> install -> learn -> report (PLAN=<plan.json> APPROVE=1 [APPROVE_ALL=1|<plan id> SKIP_ERRORS=1 DRY_RUN=1 VERBOSE=1 LEARN=0 INSTALL=0])
	@test -n "$(PLAN)" || { echo "usage: make knowledge-apply PLAN=plan-<report>.json APPROVE=1 [APPROVE_ALL=1|<plan id> SKIP_ERRORS=1 DRY_RUN=1 VERBOSE=1 LEARN=0 INSTALL=0]   (plan written by: make knowledge-classify; DRY_RUN=1 reports ready/pending)"; exit 2; }
	@args=(--plan "$$PLAN"); \
	 [[ "$(if $(filter command line,$(origin APPROVE)),$(APPROVE))" == "1" ]] && args+=(--approve); \
	 aa="$(if $(filter command line,$(origin APPROVE_ALL)),$(APPROVE_ALL))"; \
	 [[ -n "$$aa" && "$$aa" != "0" ]] && args+=(--approve-all "$$aa"); \
	 [[ "$${SKIP_ERRORS:-0}" == "1" ]] && args+=(--skip-errors); \
	 [[ "$${VERBOSE:-0}" == "1" ]] && args+=(--verbose); \
	 [[ "$${DRY_RUN:-0}" == "1" ]] && args+=(--dry-run); \
	 [[ "$${LEARN:-1}" == "0" ]] && args+=(--no-learn); \
	 [[ "$${INSTALL:-1}" == "0" ]] && args+=(--no-install); \
	 PATH="$$HOME/.local/bin:$$PATH" python3 scripts/knowledge-apply.py "$${args[@]}"

##| Read-only. Lists files under data/knowledge/<profile> whose bytes equal a
##| canonical copy (legacy full copies of books now shared through refs/).
##| Nothing is migrated or deleted; remove a copy by hand after its reference
##| learned. Needs any plan file for the environment checks (PLAN=<plan>).
knowledge-legacy-report: ## Read-only: legacy profile-local copies of books that have a canonical copy (PLAN=<plan.json>)
	@test -n "$(PLAN)" || { echo "usage: make knowledge-legacy-report PLAN=<plan.json>"; exit 2; }
	@python3 scripts/knowledge-apply.py --plan "$$PLAN" --legacy-report

# Proposal only by default. APPLY counts only on the make command line and only
# together with explicit approvals; it appends book-list lines, nothing else.
##| Finds staged knowledge whose book-list line is gone and library copies of
##| books already staged, and proposes book-list fixes. APPLY=1 with approvals
##| appends the approved lines; nothing is deleted or converted. Also lists
##| books in several book lists (reported only). Linking a source another
##| profile already lists or stages needs SECONDARY= and SECONDARY_REASON=.
knowledge-reconcile: ## Proposal only: staged knowledge with no book-list source, library copies of staged books. APPLY=1 APPROVE='<profile>=<library path>' / APPROVALS=<file> appends approved lines [SCOPE=<library path>]
	@args=(); \
	 [[ "$(if $(filter command line,$(origin APPLY)),$(APPLY))" == "1" ]] && args+=(--apply); \
	 [[ -n "$${APPROVE:-}" ]] && args+=(--approve "$$APPROVE"); \
	 [[ -n "$${APPROVALS:-}" ]] && args+=(--approvals "$$APPROVALS"); \
	 [[ -n "$${SECONDARY:-}" ]] && args+=(--secondary "$$SECONDARY"); \
	 [[ -n "$${SECONDARY_REASON:-}" ]] && args+=(--secondary-reason "$$SECONDARY_REASON"); \
	 [[ -n "$${SCOPE:-}" ]] && args+=(--scope "$$SCOPE"); \
	 python3 scripts/knowledge-reconcile.py "$${args[@]}"

# One bounded headless /learn turn per knowledge file into the exact skill
# <profile>-knowledge; state, logs and a lock live in the profile's
# knowledge-learn/ folder, so an interrupted run resumes where it stopped.
##| Turns a profile's synced Markdown into its <NAME>-knowledge skill with one
##| headless Hermes /learn run per file. A knowledge/refs/*.ref.md reference is
##| learned from the shared canonical copy it points to, after the path and
##| sha256 validate. Re-running resumes; unchanged files are skipped. This is
##| the model-cost step; DRY_RUN=1 lists the work first.
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

##| Read-only. Per profile: synced files and size, how many the <NAME>-knowledge
##| skill has learned, how many are still to learn (new or changed) or blocked.
knowledge-learn-status: ## Read-only: raw synced knowledge vs. learned <profile>-knowledge skill per profile (NAME=<profile> [JSON=1])
	@args=(--status $(NAME)); \
	 [[ "$${JSON:-0}" == "1" ]] && args+=(--json); \
	 python3 scripts/knowledge-learn.py "$${args[@]}"

##| Creates the Hermes profile if needed (SOUL.md, .env) and copies the staged
##| Markdown from data/knowledge/<NAME> into it. Run after knowledge-stage;
##| knowledge-refresh already does this sync for installed profiles.
hermes-profile: ## Create/refresh a Hermes profile and sync its staged books from data/knowledge/<NAME> (NAME=marketing [KNOWLEDGE_ROOT=data/knowledge])
	@test -n "$(NAME)" || { echo "usage: make hermes-profile NAME=<profile> [KNOWLEDGE_ROOT=<dir>]"; exit 2; }
	@bash scripts/hermes-profile.sh "$(NAME)"

##@ Windows VM

windows-vm-network: ## Publish CLIProxyAPI (:8317) + Honcho (:8000) to the Windows VM over Tailscale Serve (Docker stays on 127.0.0.1)
	@bash scripts/windows-vm-network.sh apply

windows-vm-network-status: ## Verify the loopback services and the two Tailscale Serve forwarders for the Windows VM
	@bash scripts/windows-vm-network.sh status

windows-vm-network-off: ## Remove only the two Tailscale Serve forwarders (unrelated Serve/Funnel config is left intact)
	@bash scripts/windows-vm-network.sh off

##@ BrowserOS neo pilot

##> One persistent browser profile in Docker, viewed over noVNC (SSH tunnel), driven by Hermes over MCP. Loopback only; real-account login is NO-GO (docs/browseros-neo.md, Login status).
browseros-neo-check: ## Render the pilot's Compose config and refuse anything outside its loopback-only envelope
	@bash scripts/browseros-neo.sh check

browseros-neo-up: ## Check, start BrowserOS neo, then open the agent MCP path unless paused
	@bash scripts/browseros-neo.sh up

browseros-neo-down: ## Stop and remove the pilot containers (profile kept); operator approval: APPROVE_DOWN=yes
	@bash scripts/browseros-neo.sh down $(if $(filter yes,$(APPROVE_DOWN)),--approve-down,)

browseros-neo-status: ## Reconcile the pause record with the live gate, port and bindings (OPEN/DRAINING/CLOSED/INCONSISTENT)
	@bash scripts/browseros-neo.sh status

browseros-neo-pause: ## Close the agent MCP path, then drain the pinned run budget (CLOSED is not proof the page is quiet)
	@bash scripts/browseros-neo.sh pause

browseros-neo-resume: ## Re-check config/engine/profile against `up`, then reopen the agent MCP path
	@bash scripts/browseros-neo.sh resume

browseros-neo-access: ## Print the SSH tunnel and sign-in steps for the viewer
	@bash scripts/browseros-neo.sh access

browseros-neo-hermes-enable: ## Opt-in: register Neo in the current HERMES_HOME (disabled first, enabled last); needs APPROVE_ACTIVATION=yes
	@bash scripts/browseros-neo.sh hermes-enable $(if $(filter yes,$(APPROVE_ACTIVATION)),--approve-activation,)

browseros-neo-hermes-disable: ## Set mcp_servers.browseros_neo.enabled false in the current HERMES_HOME (entry kept)
	@bash scripts/browseros-neo.sh hermes-disable

##@ Marketing video

##> New videos render with HyperFrames (pinned, local; Chrome behind a dead proxy, no network-isolation proof); ENGINE=remotion selects the legacy Remotion lane explicitly. No fallback between engines. Claims and asset-rights gates run before any render (docs/marketing-video.md).
marketing-video-install: ## Install the pinned engine deps project-locally with npm ci --ignore-scripts [ENGINE=hyperframes|remotion]
	@bash scripts/marketing-video.sh install

##| Without PROPS/PROVENANCE/OUT it renders the tracked synthetic sample into tools/<engine>/out.
marketing-video-render: ## Gates, then render + QA [PROPS= PROVENANCE= OUT= ASSETS= RATIOS=16:9,9:16 QUALITY=draft|looks|delivery ENGINE=hyperframes|remotion]
	@bash scripts/marketing-video.sh render $(if $(PROPS),--props "$(PROPS)",) $(if $(PROVENANCE),--provenance "$(PROVENANCE)",) $(if $(OUT),--out "$(OUT)",) $(if $(ASSETS),--assets "$(ASSETS)",) $(if $(RATIOS),--ratios "$(RATIOS)",) $(if $(QUALITY),--quality "$(QUALITY)",)

marketing-video-gates: ## HyperFrames: claims, rights and project-asset gates only; builds the project, renders nothing [PROPS= PROVENANCE= OUT= ASSETS=]
	@bash scripts/marketing-video.sh gates $(if $(PROPS),--props "$(PROPS)",) $(if $(PROVENANCE),--provenance "$(PROVENANCE)",) $(if $(OUT),--out "$(OUT)",) $(if $(ASSETS),--assets "$(ASSETS)",)

##@ Tool pilots (local, advisory)

##> Bounded pilots on owned fixtures only: axe-core, Lighthouse CI (filesystem upload) and the Impeccable detector (advisory) against 127.0.0.1 fixtures (tools/ui-audit-pilot); RTK output-compression replay on captured fixtures (tools/rtk-pilot). No hooks, no global installs, no uploads.
ui-audit-install: ## Install the pinned UI-audit pilot deps project-locally (npm ci --ignore-scripts)
	@cd tools/ui-audit-pilot && npm ci --ignore-scripts --no-audit --no-fund

ui-audit: ## Run axe, Lighthouse CI and Impeccable on the owned fixtures; reports stay in tools/ui-audit-pilot/reports (exit 2 = seeded findings)
	@cd tools/ui-audit-pilot && rc=0; for s in axe lhci impeccable; do npm run --silent audit:$$s || rc=$$?; done; exit $$rc

##@ Tests (offline)

test-ui-audit-pilot: ## Run the UI-audit pilot tests (real axe/LHCI/Impeccable on 127.0.0.1 fixtures; needs ui-audit-install and Chrome)
	@cd tools/ui-audit-pilot && npm test

test-rtk-pilot: ## Run the RTK replay-pilot runner tests with stub binaries (offline; no real rtk, no hook, no network)
	@python3 -m unittest discover -s tools/rtk-pilot/tests -p 'test_*.py'

test-marketing-video: ## Run the marketing-video gate/builder tests and the engine dispatcher tests (offline, no render) [ENGINE=hyperframes|remotion]
	@bash scripts/marketing-video.sh test
	@bash scripts/test_marketing_video.sh

test-browseros-neo: ## Run the BrowserOS neo pilot config/wrapper/gate tests (offline, starts no containers)
	@bash scripts/test_browseros_neo.sh
	@bash scripts/test_browseros_neo_hermes_txn.sh

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

test-knowledge-apply: ## Run the reviewed-plan / knowledge-apply / bulk-approval tests (temp library, profiles, canonical store, stub hermes; offline)
	@python3 -m unittest discover -s scripts/tests -p 'test_knowledge_appl*.py' && python3 -m unittest discover -s scripts/tests -p 'test_knowledge_approve_all.py'

test-document-converter: ## Run the document-converter tests (synthetic EPUB/PDF fixtures, offline)
	@python3 -m unittest discover -s scripts/tests -p 'test_document_converter.py'

test-capabilities: ## Run the capability registry/planner tests (strict JSON, schemas, lock, readiness, planning, CLIs; temp dirs, offline)
	@python3 -m unittest discover -s scripts/tests -p 'test_capability_*.py'
	@python3 -m unittest discover -s scripts/tests -p 'test_specialist_capability_seam.py'

test-kanban-workflow: ## Run the Discord->Kanban workflow tests (fake runner, temp git repos; real hermes CLI on a temp board when installed), the repo_lease tests (leases, writers, manifests), the completion-policy tests and the dev-test mode tests
	@python3 -m unittest discover -s scripts/tests -p 'test_kanban_workflow.py'
	@python3 -m unittest discover -s scripts/tests -p 'test_repo_lease.py'
	@python3 -m unittest discover -s scripts/tests -p 'test_completion.py'
	@python3 -m unittest discover -s scripts/tests -p 'test_dev_test_mode.py'
