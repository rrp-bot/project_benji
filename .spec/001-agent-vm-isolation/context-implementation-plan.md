# Context: Implementation Plan

## Codebase Analysis

### Script Conventions (from ephemeral-env.sh / int-env.sh)

- Scripts follow: config → helpers → credential fetch → command functions → dispatcher
- `set -euo pipefail` for strict error handling
- `die()` for standardized errors to stderr
- State tracked in dotfiles (`.ephemeral-envs`) with atomic temp-file+rename writes
- Credentials never touch disk on host — passed via env vars or container flags
- The agent operates as a full developer with an ephemeral account

### Architecture (AWS-native, ECS + CloudFormation)

- **Provisioning Service**: API Gateway + Lambda (Python) — creates CF stacks, mints IP-bound STS creds, injects via ECS Exec
- **Egress Proxy**: ECS Fargate task — per-session (paired with agent), reads GitHub/GCP secrets from Secrets Manager via ECS native secrets integration
- **Agent**: ECS task on EC2 capacity provider — privileged mode for podman, dev environment with Claude CLI, workspace, IP-bound STS creds only
- **Both tasks deployed together per session** — proxy + agent as a pair in a single CloudFormation stack, session-isolated
- **All infrastructure as CloudFormation stacks** — shared infra stack (deployed once: VPC, ECS cluster, ECR, Secrets Manager, API Gateway, Lambda, IAM) + per-session environment stack (proxy + agent task pair)
- **Container images in ECR** — both proxy and agent have Dockerfiles, pushed to ECR

### Key Design Decisions from Q&A

- **Proxy on Fargate, agent on EC2 capacity provider** — proxy is lightweight and runs on Fargate. Agent needs privileged mode for podman (container-in-container for CI tooling), which requires an EC2 capacity provider with an ECS-optimized AMI.
- **Per-session proxy** — each deployment gets its own proxy + agent pair. No shared proxy. Prevents cross-session credential leakage if proxy is compromised.
- **Provisioning Lambda in Python** — matches ephemeral provider patterns, boto3 native
- **Credential refresh supported** — POST /environments/{id}/refresh re-mints IP-bound STS without destroying tasks
- **Unit tests only for proxy** (Go) — e2e tests deferred to future
- **CloudFormation stacks as session state** — the CF stacks themselves track active sessions. `GET /environments` lists stacks with the `agent-env-` prefix. No separate state store needed for PoC.
- **ECS Exec for credential injection** — provisioning Lambda uses ECS Exec to write IP-bound STS credentials and proxy CA cert into the agent task. No S3 bucket or SSM needed.
- **Single EC2 instance (no ASG)** — PoC simplicity. One ECS-optimized AMI instance registered to the ECS cluster for agent tasks.

### Key Files to Create

| Component                                                                                | Path                                     |
| ---------------------------------------------------------------------------------------- | ---------------------------------------- |
| Egress proxy Go module                                                                   | `cmd/egress-proxy/`                      |
| Proxy Dockerfile                                                                         | `cmd/egress-proxy/Dockerfile`            |
| Agent Dockerfile                                                                         | `cmd/agent-env/Dockerfile`               |
| CF template: agent environment (both tasks)                                              | `cloudformation/agent-environment.yaml`  |
| CF template: shared infra (VPC, ECS cluster, EC2 instance, ECR, SM, API GW, Lambda, IAM) | `cloudformation/agent-shared-infra.yaml` |
| Provisioning Lambda                                                                      | `cmd/agent-provisioner/` (Python)        |
| Bootstrap script (one-time secrets setup)                                                | `scripts/dev/agent-bootstrap-secrets.sh` |
| Makefile targets                                                                         | Append to `Makefile`                     |

### Existing Files to Modify

| File                              | Change                                                                                     |
| --------------------------------- | ------------------------------------------------------------------------------------------ |
| `Makefile`                        | Add `agent-env-create`, `agent-env-destroy`, `agent-env-list`, `agent-env-refresh` targets |
| `docs/development-environment.md` | Add agent environment section                                                              |

## Questions & Answers

### Implementation Questions

### Q1: Should the provisioning service (Lambda) be written in Go or Python?

**Answer:** Python — matches ephemeral provider patterns, boto3 native, faster Lambda cold starts.

### Q2: Should the shared infrastructure be a separate CF stack?

**Answer:** Yes. Two levels of CloudFormation stacks:

1. **Shared infra stack** (deployed once): VPC, subnets, ECS cluster, EC2 instance (ECS-optimized AMI), ECR repositories, Secrets Manager secrets, security groups, API Gateway + Lambda provisioning service, IAM roles.
2. **Per-session environment stack** (deployed per `POST /environments`): paired ECS tasks (proxy on Fargate + agent on EC2 capacity provider), task definitions, task-specific security groups, networking.

### Q3: Agent task: Fargate or EC2 capacity provider?

**Answer:** EC2 capacity provider. The agent must run podman for container-in-container execution (running the `rosa-regional-ci` image). Fargate does not support privileged mode. The proxy runs on Fargate (lightweight, no privileged needs). A single EC2 instance with the ECS-optimized AMI is registered to the cluster for agent tasks.

### Q4: Should credential refresh be supported?

**Answer:** Yes — POST /environments/{id}/refresh re-mints IP-bound STS sessions without destroying the tasks. Enables sessions beyond the 12-hour STS limit.

### Testing Questions

### Q5: Proxy testing approach?

**Answer:** Unit tests only for now (domain matching, credential injection, allowlist logic, log redaction). E2e integration tests deferred to future. The real AWS deployment serves as the integration test for the PoC.

### Q6: CF/Lambda infrastructure testing?

**Answer:** Manual deployment for real AWS verification. CF template validation via `aws cloudformation validate-template`. No LocalStack — real AWS deployment is the integration test for a PoC.
