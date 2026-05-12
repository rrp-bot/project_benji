# Project Benji — Agent VM Isolation

## Project Overview

Credential-isolated ECS-based development environments for running LLM agents (Claude Code) against multi-account AWS infrastructure. The core security property: **no credentials exist inside the agent container**. All GitHub and Google Cloud authentication is handled at the network layer by a TLS-intercepting egress proxy; AWS credentials are IP-bound STS sessions that are useless from any other IP.

### Three Components

1. **Egress Proxy** (`egress-proxy/`) — Go TLS-terminating MITM forward proxy. Runs as a Fargate task per session. Injects GitHub PAT and Google OAuth2 tokens into proxied requests. Enforces a compiled domain allowlist. Reads credentials from Secrets Manager at startup via ECS native secrets integration.

2. **Agent Environment** (`agent-env/`) — UBI9 container running on an EC2 capacity provider (privileged mode for podman). Developer's working environment with Claude CLI, git, gh, aws-cli, podman. Holds only IP-bound AWS STS credentials. All HTTPS traffic routes through the paired proxy.

3. **Provisioning Service** (`agent-env/provisioner/`) — API Gateway + Lambda (Python) that orchestrates environment lifecycle. The sole credential minter — reads raw secrets from Secrets Manager, creates IP-bound STS sessions, injects them into the agent container via ECS Exec.

### Infrastructure

- **Shared stack** (`agent-shared-infra`): VPC, ECS cluster, EC2 ASG, ECR repos, Secrets Manager, API Gateway, Lambda, VPC Endpoints
- **Per-environment stack** (`agent-env-{id}`): Paired proxy (Fargate) + agent (EC2) ECS tasks, created/destroyed via the provisioning API
- All infrastructure is CloudFormation

## Repository Structure

```
agent-env/                   # Agent container + provisioning
├── Dockerfile               # UBI9 agent image (Claude CLI, podman, aws-cli, gh)
├── cloudformation/          # CloudFormation templates
│   ├── agent-shared-infra.yaml   # Shared infrastructure (deploy once)
│   └── agent-environment.yaml    # Per-session stack (proxy + agent pair)
├── config/
│   └── CLAUDE.md            # Instructions injected into agent container
├── provisioner/
│   ├── Dockerfile           # Lambda container image
│   └── handler.py           # Provisioning Lambda (credential minting, env setup)
└── scripts/
    ├── agent-deploy.sh      # CLI for all build/deploy/manage operations
    ├── agent-bootstrap-secrets.sh  # One-time Secrets Manager setup
    ├── agent-mint-credentials.py   # Standalone credential minter
    ├── agent-use-profile.py        # Profile switcher helper
    └── agent-verify.sh             # E2E verification

egress-proxy/                # TLS MITM forward proxy (Go)
├── Dockerfile               # Multi-stage build (ubi9/go-toolset → ubi9-minimal)
├── main.go                  # Entrypoint, health check, /ca.crt endpoint
├── proxy.go                 # CONNECT handler, TLS interception
├── ca.go                    # Per-session CA generation, leaf cert cache
├── credentials.go           # GitHub PAT + Google OAuth2 injection
├── allowlist.go             # Domain allowlist (compiled in)
├── logger.go                # Structured JSON request logging
└── *_test.go                # Tests for each component

config/                      # Local config (gitignored)
├── account_config.yaml      # AWS account IDs, role ARNs, egress IP
├── account_config_ci.yaml   # CI variant
└── env-vars.txt             # Non-secret env vars (uploaded to SSM)

docs/
└── architecture.md          # Full architecture diagram and design

.spec/                       # Spec-driven development artifacts
└── 001-agent-vm-isolation/  # Original feature specification
```

## Development Workflow

### Prerequisites

- AWS CLI v2, configured with permissions to the agent account
- Docker or Podman for building images
- `awscurl` for IAM-signed API calls (`pip install awscurl`)

### Common Operations

All operations go through `make` targets:

```bash
# Build
make images-build       # Build all container images (parallel)
make proxy-build        # Build just the proxy
make env-build          # Build just the agent container
make provisioner-build  # Build just the provisioner Lambda

# Deploy
make images-push        # Push all images to ECR
make infra-deploy       # Deploy/update shared infrastructure stack

# Environment lifecycle
make env-create         # Create a new environment (creates CF stack, provisions)
make env-list           # List active environments
make env-shell ID=xxx   # Shell into an agent environment
make env-root-shell ID=xxx  # Root shell (debugging)
make env-refresh ID=xxx # Refresh STS credentials (extend sessions)
make env-destroy ID=xxx # Destroy an environment

# Configuration
make env-config         # Upload env vars from config/env-vars.txt to SSM
make bootstrap-secrets  # Bootstrap Secrets Manager (one-time)

# Status & verification
make status             # Show infrastructure and environment status
make verify             # Run E2E verification
```

### When Updating Secrets

When GitHub PAT, GCP SA JSON, or AWS credentials change in Secrets Manager:

1. **Proxy restart required** — the proxy reads secrets at startup and holds them in memory. Force a new deployment of the proxy ECS service.
2. **Re-inject proxy CA** — the new proxy generates a new CA. The agent container needs the new CA cert installed in its trust store, and proxy env vars updated to the new proxy IP. Re-run provisioning or manually curl the `/ca.crt` endpoint and run `update-ca-trust`.

### When Updating Agent Configuration

Non-secret env vars (Vertex AI project, Claude settings) live in `config/env-vars.txt`. After editing, run `make env-config` to upload to SSM. New environments will pick up the changes automatically; existing environments need re-provisioning.

## Security Model

- **No credentials in the agent container** for GitHub or Google Cloud — the proxy injects them
- **IP-bound AWS STS sessions** — credentials are useless from any IP other than the agent's egress IP
- **Domain allowlist** — the proxy rejects CONNECT requests to non-allowlisted domains before TLS
- **Per-session proxy isolation** — each agent gets its own proxy instance
- **Provisioner is the sole credential minter** — deterministic code, not an LLM
- **Agent runs as non-root** (UID 1000) — privileged mode is for podman fuse-overlayfs only

## Key Design Decisions

- **CloudFormation over Terraform** — per-session stacks are disposable and self-contained; CF's `delete-stack` cleanly removes everything
- **EC2 capacity provider for agent** — privileged mode needed for podman (container-in-container); Fargate doesn't support privileged
- **Compiled allowlist** — no runtime config to tamper with; changes require a proxy rebuild
- **ECS Exec for provisioning** — Lambda injects credentials/config into running containers rather than passing via env vars or volumes (which would expose secrets in task definitions)

## Formatting

- **Go**: `gofmt` for all Go files in `egress-proxy/`
- **Python**: Standard formatting for `handler.py` and scripts
- **YAML**: 2-space indentation for CloudFormation templates

## Testing

- **Go unit tests**: `cd egress-proxy && go test ./...`
- **E2E verification**: `make agent-verify` — tests credential isolation, proxy connectivity, allowlist enforcement
- **Manual verification**: Shell into an environment and verify git clone, AWS CLI, and Vertex AI calls work
