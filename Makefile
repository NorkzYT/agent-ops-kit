# agent-ops-kit — operator entry points.
#   make help        list targets
#   make init        create .env (+secrets), render proxy config, fetch proxy sources
#   make up          start the Docker half (Honcho, Ollama, both proxies)
#   make hermes-install   install/configure Hermes on this host (Discord, Honcho, Browser Use)
SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

COMPOSE ?= docker compose
ENV_FILE ?= .env
S ?=
NAME ?=

# Read a value from .env without exporting the whole file.
define envval
$(shell sed -n 's/^$(1)=//p' $(ENV_FILE) 2>/dev/null | tail -n1 | tr -d '"' | tr -d "'")
endef

.PHONY: help init up down restart pull build update ps status logs \
        auth-codex auth-claude-proxy models honcho-health doctor \
        hermes-install hermes-profile hermes-restart hermes-logs clean

help: ## Show this help
	@awk 'BEGIN{FS=":.*##"} /^[a-zA-Z_-]+:.*##/{printf "  \033[36m%-20s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)

init: ## Create .env with generated secrets, render proxy config, sync proxy sources
	@bash scripts/stack-init.sh

up: init ## Start (or update) the Docker stack
	@$(COMPOSE) up -d --build --remove-orphans
	@echo; echo "Stack is starting. Next: make auth-codex, make auth-claude-proxy, make doctor"
	@test -n "$(call envval,CLAUDE_CODE_OAUTH_TOKEN)" || \
	  echo "note: claude-max-proxy idles until you run make auth-claude-proxy"

down: ## Stop the Docker stack (data volumes are kept)
	@$(COMPOSE) down

restart: ## Restart all services, or one with S=<service>
	@$(COMPOSE) restart $(S)

pull: ## Pull newer images
	@$(COMPOSE) pull --ignore-buildable

build: ## Rebuild the claude-max-proxy image
	@$(COMPOSE) build claude-max-proxy

update: ## Sync proxy sources, pull images, rebuild and restart
	@bash docker/claude-max-proxy/sync-checkout.sh "$(or $(call envval,CLAUDE_MAX_PROXY_DIR),./vendor/claude-max-api-proxy)"
	@$(COMPOSE) pull --ignore-buildable
	@$(COMPOSE) up -d --build --remove-orphans

ps status: ## Container status
	@$(COMPOSE) ps

logs: ## Tail logs (all, or S=<service>)
	@$(COMPOSE) logs -f --tail=200 $(S)

auth-codex: ## Log the ChatGPT/Codex subscription into CLIProxyAPI (device code flow)
	@echo "Follow the URL shown, sign in with your ChatGPT account, then come back."
	@$(COMPOSE) run --rm --no-deps -it cliproxyapi ./CLIProxyAPI -config /CLIProxyAPI/config.yaml -codex-device-login
	@$(COMPOSE) restart cliproxyapi
	@echo "Verify with: make models"

auth-claude-proxy: ## Log the Claude Max subscription into claude-max-proxy (stores the token in .env)
	@COMPOSE="$(COMPOSE)" bash scripts/auth-claude-proxy.sh

models: ## List models exposed by both proxies
	@echo "== CLIProxyAPI (ChatGPT subscription) http://127.0.0.1:$(or $(call envval,CLIPROXY_PORT),8317)/v1"; \
	curl -fsS -H "Authorization: Bearer $(call envval,CLIPROXY_API_KEY)" \
	  http://127.0.0.1:$(or $(call envval,CLIPROXY_PORT),8317)/v1/models \
	  | sed -e 's/},{/},\n{/g' | grep -o '"id":"[^"]*"' | sed 's/"id"://' || echo "  (not reachable: run make auth-codex)"
	@echo; echo "== claude-max-proxy (Claude Max) http://127.0.0.1:$(or $(call envval,CLAUDE_MAX_PROXY_PORT),3456)/v1"; \
	curl -fsS http://127.0.0.1:$(or $(call envval,CLAUDE_MAX_PROXY_PORT),3456)/v1/models \
	  | sed -e 's/},{/},\n{/g' | grep -o '"id":"[^"]*"' | sed 's/"id"://' || echo "  (not reachable: run make auth-claude-proxy)"

honcho-health: ## Check the Honcho API
	@curl -fsS http://127.0.0.1:$(or $(call envval,HONCHO_PORT),8000)/health && echo

doctor: ## Check the whole stack (Docker services, proxies, Honcho, Hermes)
	@bash scripts/doctor.sh

hermes-install: ## Install Hermes on this host and wire it to Discord, Honcho, both proxies, Browser Use
	@bash scripts/hermes-install.sh

hermes-profile: ## Create a Hermes profile from hermes/profiles/<NAME> (make hermes-profile NAME=marketing)
	@test -n "$(NAME)" || { echo "usage: make hermes-profile NAME=<profile>"; exit 2; }
	@bash scripts/hermes-profile.sh "$(NAME)"

hermes-restart: ## Restart the Hermes gateway (Discord bot)
	@hermes gateway restart

hermes-logs: ## Tail Hermes gateway logs
	@tail -n 200 -f "$${HERMES_HOME:-$$HOME/.hermes}/logs/gateway.log"

clean: ## Stop the stack AND delete its data volumes (asks first)
	@read -r -p "This deletes Honcho memory, Ollama models and proxy state. Type 'yes' to continue: " a; \
	[[ "$$a" == "yes" ]] && $(COMPOSE) down -v || echo "aborted"
