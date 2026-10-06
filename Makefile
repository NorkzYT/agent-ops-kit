# agent-ops-kit — operator entry points.
#   make help                 list targets
#   make init                 create .env (+secrets), render CLIProxyAPI config
#   make up                   start the Docker half (Honcho, Ollama, CLIProxyAPI)
#   make claude-proxy-install claude-max-proxy on this host (systemd user service)
#   make hermes-install       install/configure Hermes on this host (Discord, Honcho, Browser Use)
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

.PHONY: help init up down restart pull update ps status logs \
	    cliproxyapi-logs cliproxyapi-logs-follow router-logs router-logs-follow \
	    auth-codex auth-claude-proxy claude-proxy-install claude-proxy-update claude-proxy-restart claude-proxy-logs claude-cli-update \
	    models honcho-health doctor route-smoke test-router \
	    windows-vm-network windows-vm-network-status windows-vm-network-off \
	    hermes-install hermes-profile knowledge-convert knowledge-stage knowledge-refresh knowledge-inventory test-knowledge-stage test-knowledge-refresh test-document-converter hermes-restart hermes-logs hermes-ui hermes-ui-status hermes-ui-stop clean

help: ## Show this help
	@awk 'BEGIN{FS=":.*##"} /^[a-zA-Z_-]+:.*##/{printf "  \033[36m%-22s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)

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

# $(call tail_lines,<default>) sets shell var $$n to LINES (env or `make LINES=n`)
# validated as a positive integer, falling back to <default>.
define tail_lines
n="$${LINES:-$(1)}"; [[ "$$n" =~ ^[1-9][0-9]*$$ ]] || { echo "LINES must be a positive integer (got '$$n')" >&2; exit 2; }
endef

router-logs: ## Last 20 model_router lines from the Hermes agent log (LINES=<n> to override)
	@bash scripts/router-logs.sh snapshot

router-logs-follow: ## Recent model_router lines, then follow live (survives log rotation)
	@bash scripts/router-logs.sh follow

cliproxyapi-logs: ## Last 200 CLIProxyAPI container log lines (LINES=<n> to override)
	@$(call tail_lines,200); $(COMPOSE) logs --tail="$$n" cliproxyapi

cliproxyapi-logs-follow: ## Follow CLIProxyAPI container logs from the last 200 lines (LINES=<n> to override)
	@$(call tail_lines,200); $(COMPOSE) logs -f --tail="$$n" cliproxyapi

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

test-router: ## Run the model-router unit + zero-egress privacy canary suite (offline)
	@python3 -m unittest discover -s scripts/tests -p 'test_*.py'

route-smoke: ## Read-only routing smoke test: probe /v1/models, degrade-check tiers, run the canary
	@bash scripts/route-smoke.sh

honcho-health: ## Check the Honcho API
	@curl -fsS http://127.0.0.1:$(or $(call envval,HONCHO_PORT),8000)/health && echo

doctor: ## Check the whole stack (containers, both proxies, Honcho, Hermes)
	@bash scripts/doctor.sh

windows-vm-network: ## Publish CLIProxyAPI (:8317) + Honcho (:8000) to the Windows VM over Tailscale Serve (Docker stays on 127.0.0.1)
	@bash scripts/windows-vm-network.sh apply

windows-vm-network-status: ## Verify the loopback services and the two Tailscale Serve forwarders for the Windows VM
	@bash scripts/windows-vm-network.sh status

windows-vm-network-off: ## Remove only the two Tailscale Serve forwarders (unrelated Serve/Funnel config is left intact)
	@bash scripts/windows-vm-network.sh off

hermes-install: ## Install Hermes on this host and wire it to Discord, Honcho, both proxies, Browser Use
	@bash scripts/hermes-install.sh

hermes-profile: ## Create/refresh a Hermes profile and sync its books from data/knowledge/<NAME> (NAME=marketing [KNOWLEDGE_ROOT=/mnt/books/knowledge])
	@test -n "$(NAME)" || { echo "usage: make hermes-profile NAME=<profile> [KNOWLEDGE_ROOT=<dir>]"; exit 2; }
	@bash scripts/hermes-profile.sh "$(NAME)"

knowledge-convert: ## Copy-convert EPUB/PDF books to Markdown (SRC=/mnt/books/edu DEST=data/knowledge/<profile> [ARGS="--dry-run 'Book folder'"])
	@test -n "$(SRC)" -a -n "$(DEST)" || { echo "usage: make knowledge-convert SRC=<book dir> DEST=<output dir> [ARGS=...]"; exit 2; }
	@python3 hermes/skills/document-converter/scripts/doc2md.py --source "$(SRC)" --dest "$(DEST)" $(ARGS)

knowledge-stage: ## Build data/knowledge/<NAME> from hermes/profiles/<NAME>/knowledge-sources.txt (NAME=strategy [BOOKS_ROOT=/mnt/books KNOWLEDGE_CONVERT=0])
	@test -n "$(NAME)" || { echo "usage: make knowledge-stage NAME=<profile> [BOOKS_ROOT=<dir>]"; exit 2; }
	@bash scripts/knowledge-stage.sh "$(NAME)"

knowledge-refresh: ## After adding books: stage + sync every book-backed profile (or NAME=<profile>) and report updated/current/missing/uncategorized
	@bash scripts/knowledge-refresh.sh $(NAME)

knowledge-inventory: ## Read-only: compare the book library with every knowledge-sources.txt; suggest lines for uncategorized books
	@bash scripts/knowledge-refresh.sh --inventory $(NAME)

test-knowledge-refresh: ## Run the knowledge-refresh tests (temp library, profiles, HERMES_HOME; offline)
	@bash scripts/test_knowledge_refresh.sh

test-knowledge-stage: ## Run the knowledge-stage tests (temp library, stub converter, offline)
	@bash scripts/test_knowledge_stage.sh

test-document-converter: ## Run the document-converter tests (synthetic EPUB/PDF fixtures, offline)
	@python3 -m unittest discover -s scripts/tests -p 'test_document_converter.py'

hermes-restart: ## Restart the Hermes gateway (Discord bot)
	@hermes gateway restart

hermes-logs: ## Tail Hermes gateway logs
	@tail -n 200 -f "$${HERMES_HOME:-$$HOME/.hermes}/logs/gateway.log"

hermes-ui: ## Start the Hermes dashboard (config/keys web UI) on 127.0.0.1 (HERMES_UI_PORT, default 9119)
	@bash scripts/hermes-ui.sh start

hermes-ui-status: ## Show whether the Hermes dashboard is running
	@bash scripts/hermes-ui.sh status

hermes-ui-stop: ## Stop the Hermes dashboard
	@bash scripts/hermes-ui.sh stop

clean: ## Stop the Docker half AND delete its volumes: Honcho memory, Ollama models (asks first)
	@read -r -p "This deletes Honcho memory and the Ollama model. Type 'yes' to continue: " a; \
	[[ "$$a" == "yes" ]] && $(COMPOSE) down -v || echo "aborted"
