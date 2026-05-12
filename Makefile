.PHONY: help proxy-build env-build provisioner-build images-build images-push infra-deploy infra-destroy env-create env-destroy env-list env-shell env-root-shell env-refresh env-config bootstrap-secrets status verify

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-25s\033[0m %s\n", $$1, $$2}'

# =============================================================================
# Agent VM Isolation
# =============================================================================
# Credential-isolated LLM agent environments (ECS-based).
# See docs/architecture.md for full design.

DEPLOY := ./agent-env/scripts/agent-deploy.sh

proxy-build: ## Build egress proxy container image
	@$(DEPLOY) proxy-build

env-build: ## Build agent environment container image
	@$(DEPLOY) agent-build

provisioner-build: ## Build provisioner Lambda container image
	@$(DEPLOY) provisioner-build

images-build: ## Build all container images (parallel)
	@$(DEPLOY) images-build

images-push: ## Push all images to ECR
	@$(DEPLOY) images-push

infra-deploy: ## Deploy (or update) shared infrastructure stack
	@$(DEPLOY) infra-deploy

infra-destroy: ## Delete shared infrastructure stack
	@$(DEPLOY) infra-destroy

env-create: ## Create a new environment
	@$(DEPLOY) env-create

env-destroy: ## Destroy an environment (ID=<env-id>)
	@$(DEPLOY) env-destroy $(ID)

env-list: ## List active environments
	@$(DEPLOY) env-list

env-shell: ## Shell into an environment (ID=<env-id>)
	@TASK=$$(aws ecs list-tasks --cluster agent-vm-isolation --service-name agent-env-$(ID) --desired-status RUNNING --query 'taskArns[0]' --output text) && \
	aws ecs execute-command --cluster agent-vm-isolation --task $$TASK --container agent --interactive --command "su - agent"

env-root-shell: ## Root shell into an environment (ID=<env-id>)
	@TASK=$$(aws ecs list-tasks --cluster agent-vm-isolation --service-name agent-env-$(ID) --desired-status RUNNING --query 'taskArns[0]' --output text) && \
	aws ecs execute-command --cluster agent-vm-isolation --task $$TASK --container agent --interactive --command "/bin/bash"

env-refresh: ## Refresh STS credentials for an environment (ID=<env-id>)
	@$(DEPLOY) env-refresh $(ID)

env-config: ## Upload env vars from config/env-vars.txt to SSM
	@aws ssm put-parameter --name /agent-env/env-vars --type String --overwrite --value "$$(cat config/env-vars.txt)" --query 'Version' --output text | xargs -I{} echo "Parameter updated (version {})"

bootstrap-secrets: ## Bootstrap Secrets Manager with credentials
	@./agent-env/scripts/agent-bootstrap-secrets.sh

status: ## Show infrastructure and environment status
	@$(DEPLOY) status

verify: ## Run E2E verification
	@./agent-env/scripts/agent-verify.sh
