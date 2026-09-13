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
	    auth-codex auth-claude-proxy claude-proxy-install claude-proxy-update claude-proxy-restart claude-proxy-logs \
	    models honcho-health doctor \
	    hermes-install hermes-profile hermes-restart hermes-logs hermes-ui hermes-ui-status hermes-ui-stop clean

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

models: ## List models exposed by both proxies (distinguishes down from empty)
	@bash scripts/models.sh

honcho-health: ## Check the Honcho API
	@curl -fsS http://127.0.0.1:$(or $(call envval,HONCHO_PORT),8000)/health && echo

doctor: ## Check the whole stack (containers, both proxies, Honcho, Hermes)
	@bash scripts/doctor.sh

hermes-install: ## Install Hermes on this host and wire it to Discord, Honcho, both proxies, Browser Use
	@bash scripts/hermes-install.sh

hermes-profile: ## Create/refresh a Hermes profile and sync its books from data/knowledge/<NAME> (NAME=marketing [KNOWLEDGE_ROOT=/mnt/books/knowledge])
	@test -n "$(NAME)" || { echo "usage: make hermes-profile NAME=<profile> [KNOWLEDGE_ROOT=<dir>]"; exit 2; }
	@bash scripts/hermes-profile.sh "$(NAME)"

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
