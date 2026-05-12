# Implementation Plan: Agent VM Isolation (ECS-based)

## Technical Approach

Three components deployed as CloudFormation stacks in a single AWS region:

1. **Shared infrastructure stack** (deployed once): VPC, subnets, ECS cluster, ECR repositories, Secrets Manager secrets, security groups, API Gateway + Lambda provisioning service.
2. **Per-session environment stack** (deployed per developer session): paired ECS Fargate tasks (proxy + agent), task definitions, task-specific security groups, networking.
3. **Container images** (built and pushed to ECR): egress proxy (Go binary), agent environment (CI tools + Claude CLI).

The provisioning Lambda is the orchestrator — it creates CF stacks, waits for tasks to start, discovers the agent's public IP, mints IP-bound STS credentials, and injects them via ECS Exec.

## Implementation

### Milestone 1: Egress Proxy

Build and test the Go proxy binary before any infrastructure work. This is the most complex custom component.

#### Phase 1: Go module and core proxy

- [ ] Step 1: Create `cmd/egress-proxy/` with `go.mod`, `main.go`
- [ ] Step 2: Implement the HTTP CONNECT handler that accepts tunnel requests
- [ ] Step 3: Implement CA certificate generation at startup (self-signed, `crypto/x509`)
- [ ] Step 4: Implement per-domain TLS certificate generation signed by the CA
- [ ] Step 5: Implement the MITM flow: accept CONNECT → check allowlist → 200 → TLS handshake with generated cert → connect to upstream → relay traffic
- [ ] Step 6: Write unit tests for domain allowlist matching (exact match, wildcard suffix)

#### Phase 2: Credential injection

- [ ] Step 7: Implement GitHub static token injection — read `GITHUB_TOKEN` env var, inject `Authorization: Bearer` header for `github.com` and `api.github.com` requests
- [ ] Step 8: Implement Google OAuth2 token injection — read `GCP_SA_JSON` env var, create `oauth2.TokenSource` via `golang.org/x/oauth2/google`, inject bearer token for `*-aiplatform.googleapis.com` and `oauth2.googleapis.com`
- [ ] Step 9: Write unit tests for credential injection logic (correct header, correct domain matching, header replacement)

#### Phase 3: Logging and health

- [ ] Step 10: Implement structured JSON logging to stdout (fields: timestamp, source_ip, destination, method, path, status, duration_ms, credential_type). Ensure credential values are never logged.
- [ ] Step 11: Implement `/healthz` endpoint — check GitHub token validity via `api.github.com/rate_limit`, check Google OAuth2 token via `TokenSource.Token()`
- [ ] Step 12: Write unit tests for log redaction (verify Authorization headers are stripped)
- [ ] Step 13: Run all proxy tests: `cd cmd/egress-proxy && go test ./...`
  - **Expected output**: all tests pass, no credential values in test log output

#### Phase 4: Container image

- [ ] Step 14: Create `cmd/egress-proxy/Dockerfile` — multi-stage build: Go builder → scratch/distroless final image with static binary
- [ ] Step 15: Build and test locally: `docker build -t agent-egress-proxy cmd/egress-proxy/`
- [ ] Step 16: Verify container starts and `/healthz` responds (with dummy credentials)

### Milestone 2: Agent Container Image

Build the agent container that includes CI tooling and Claude CLI.

#### Phase 5: Agent Dockerfile

- [ ] Step 17: Create `cmd/agent-env/Dockerfile` based on `rosa-regional-ci` (UBI9). Add:
  - Claude CLI installation
  - podman (for running the CI container image, replicating local developer workflow)
  - git, SSH client
  - AWS CLI (already in rosa-regional-ci)
  - Workspace directory setup (`/workspace/`)
  - `HTTPS_PROXY` env var placeholder (set at runtime)
  - Entrypoint that keeps the container running (for ECS Exec access)
  - Note: task runs in privileged mode on EC2 capacity provider to support podman
- [ ] Step 18: Build and test locally: `docker build -t agent-env cmd/agent-env/`
- [ ] Step 19: Verify Claude CLI and podman are available: `docker run --privileged --rm agent-env bash -c 'claude --version && podman --version'`

### Milestone 3: Shared Infrastructure CloudFormation

Deploy the one-time shared resources.

#### Phase 6: Shared infra CF template

- [ ] Step 20: Create `cloudformation/agent-shared-infra.yaml` with:
  - VPC with public and private subnets (or reference existing VPC)
  - ECS cluster with Fargate capacity provider (for proxy tasks)
  - Single EC2 instance (ECS-optimized AMI) registered to the ECS cluster for agent tasks (supports privileged mode for podman). No ASG — PoC simplicity.
  - ECR repositories: `agent-egress-proxy`, `agent-env`
  - Security group for proxy tasks (inbound 3128 from agent SG only)
  - Security group for agent tasks (egress to proxy SG + AWS API endpoints only)
  - Secrets Manager secrets (empty, populated by bootstrap script):
    - `agent-ephemeral/central-access-key`, `central-secret-key`, `central-assume-role-arn`
    - `agent-ephemeral/regional-access-key`, `regional-secret-key`
    - `agent-ephemeral/management-access-key`, `management-secret-key`
    - `agent-proxy/github-token`
    - `agent-proxy/gcp-sa-json`
  - IAM role for provisioning Lambda (Secrets Manager read, CF create/delete, ECS, STS)
  - IAM role for proxy task (Secrets Manager read for GitHub/GCP only)
  - IAM role for agent task (minimal — ECS Exec support only)
  - API Gateway (REST, IAM auth) with routes: POST/GET/DELETE /environments, POST /environments/{id}/refresh
  - Lambda function (placeholder, deployed in next phase)
  - Stack outputs: ECS cluster ARN, ECR repo URIs, VPC/subnet IDs, API Gateway URL, security group IDs, EC2 instance ID
- [ ] Step 21: Validate template: `aws cloudformation validate-template --template-body file://cloudformation/agent-shared-infra.yaml`

#### Phase 7: Bootstrap secrets

- [ ] Step 22: Create `scripts/dev/agent-bootstrap-secrets.sh` — interactive script that prompts for each secret value and writes to Secrets Manager using the secret names from the shared infra stack
- [ ] Step 23: Test bootstrap script against real AWS: run script, verify secrets are populated
  - **Expected output**: `aws secretsmanager get-secret-value` returns values for all 9 secrets

### Milestone 4: Provisioning Lambda

The orchestrator that creates per-session environments.

#### Phase 8: Lambda implementation

- [ ] Step 24: Create `cmd/agent-provisioner/` with Python Lambda handler
- [ ] Step 25: Implement `POST /environments` handler:
  1. Generate environment ID (short hash)
  2. Create per-session CF stack from `agent-environment.yaml` template (parameters: env ID, ECR image URIs, security group IDs, subnet IDs, ECS cluster)
  3. Wait for stack creation (CF waiter or async with status polling)
  4. Discover agent task's public IP from task ENI
  5. Read raw AWS credentials from Secrets Manager
  6. Mint IP-bound STS sessions for all three accounts (central, regional, management) with `aws:SourceIp = <agent-IP>/32` session policy
  7. Inject STS credentials into agent task via ECS Exec (write to `~/.aws/credentials`)
  8. Inject proxy CA certificate into agent task trust store via ECS Exec
  9. Return environment ID, agent task ARN, public IP, ECS Exec connection command
- [ ] Step 26: Implement `GET /environments` handler — list CF stacks with `agent-env-` prefix, return status and metadata from stack outputs/tags
- [ ] Step 27: Implement `DELETE /environments/{id}` handler — delete the CF stack
- [ ] Step 28: Implement `POST /environments/{id}/refresh` handler — re-read secrets, re-mint IP-bound STS sessions, re-inject via ECS Exec
- [ ] Step 29: Create Lambda deployment package with dependencies (boto3 is built-in, add any others)
- [ ] Step 30: Update shared infra CF template to deploy the Lambda function code (inline or S3)

#### Phase 9: Per-session CF template

- [ ] Step 31: Create `cloudformation/agent-environment.yaml` with:
  - ECS task definition for proxy (Fargate, image from ECR, secrets from SM, port 3128, health check, awslogs)
  - ECS task definition for agent (EC2 capacity provider, privileged mode for podman, image from ECR, environment variables including HTTPS_PROXY, awslogs, ECS Exec enabled)
  - ECS service or standalone tasks for both (awsvpc networking, public IP for agent)
  - Task-specific security group rules (proxy accepts from agent only, agent egress to proxy only)
  - Stack parameters: environment ID, ECR image URIs, ECS cluster, VPC/subnet IDs, security group IDs, task size (CPU/memory)
  - Stack outputs: agent task ARN, proxy task ARN, agent public IP, ECS cluster for Exec
- [ ] Step 32: Validate template: `aws cloudformation validate-template --template-body file://cloudformation/agent-environment.yaml`

### Milestone 5: Push Images and Deploy

#### Phase 10: ECR push and shared stack deployment

- [ ] Step 33: Push proxy image to ECR: `docker tag agent-egress-proxy <ecr-uri>:latest && docker push <ecr-uri>:latest`
- [ ] Step 34: Push agent image to ECR: `docker tag agent-env <ecr-uri>:latest && docker push <ecr-uri>:latest`
- [ ] Step 35: Deploy shared infra stack: `aws cloudformation create-stack --stack-name agent-shared-infra --template-body file://cloudformation/agent-shared-infra.yaml --capabilities CAPABILITY_IAM`
- [ ] Step 36: Run bootstrap secrets script to populate Secrets Manager
- [ ] Step 37: Verify API Gateway endpoint is accessible

#### Phase 11: End-to-end verification

- [ ] Step 38: Call `POST /environments` via the API Gateway — verify CF stack creates successfully and both tasks start
- [ ] Step 39: ECS Exec into agent task — verify Claude CLI is available
- [ ] Step 40: From agent task, `git clone` a private repo — verify it succeeds via proxy (AC-2)
- [ ] Step 41: From agent task, make a Vertex AI API call — verify OAuth2 works via proxy (AC-3)
- [ ] Step 42: From agent task, run an ephemeral environment provision — verify IP-bound STS credentials work (AC-4)
- [ ] Step 43: From a different machine, attempt to use the STS credentials — verify AccessDenied (AC-5)
- [ ] Step 44: Search agent container for GitHub token and GCP SA JSON — verify they don't exist (AC-6)
- [ ] Step 45: Attempt a CONNECT to a non-allowlisted domain via the proxy — verify 403 (AC-7)
- [ ] Step 46: Check CloudWatch Logs for proxy audit entries — verify no credential values logged (AC-8)
- [ ] Step 47: Call `DELETE /environments/{id}` — verify CF stack is deleted cleanly (AC-9)
- [ ] Step 48: Call `POST /environments/{id}/refresh` — verify STS credentials are refreshed (AC-10)
- [ ] Step 49: Run Claude CLI on the agent and interact with Vertex AI — verify it works (AC-11)

### Milestone 6: Makefile and Documentation

#### Phase 12: Developer interface

- [ ] Step 50: Add Makefile targets:
  - `agent-env-create`: call POST /environments
  - `agent-env-destroy`: call DELETE /environments/{id}
  - `agent-env-list`: call GET /environments
  - `agent-env-refresh`: call POST /environments/{id}/refresh
  - `agent-proxy-build`: build proxy image
  - `agent-env-build`: build agent image
  - `agent-images-push`: push both images to ECR
  - `agent-infra-deploy`: deploy shared infra stack
  - `agent-bootstrap-secrets`: run bootstrap secrets script
- [ ] Step 51: Update `docs/development-environment.md` with agent environment section covering:
  - Prerequisites (AWS account, secrets populated)
  - Quick start (make agent-env-create → ECS Exec in → Claude CLI)
  - Architecture overview (link to design doc)
  - Credential refresh
  - Troubleshooting

## Summary of Changes in Key Technical Areas

### Components to Create

| Component           | Path                                     | Language   | Description                                |
| ------------------- | ---------------------------------------- | ---------- | ------------------------------------------ |
| Egress proxy        | `cmd/egress-proxy/`                      | Go         | TLS-terminating MITM forward proxy         |
| Agent container     | `cmd/agent-env/`                         | Dockerfile | Dev environment with CI tools + Claude CLI |
| Provisioning Lambda | `cmd/agent-provisioner/`                 | Python     | API handler for environment lifecycle      |
| Shared infra CF     | `cloudformation/agent-shared-infra.yaml` | CF YAML    | VPC, ECS, ECR, SM, API GW, Lambda, IAM     |
| Per-session CF      | `cloudformation/agent-environment.yaml`  | CF YAML    | Proxy + agent task pair                    |
| Bootstrap script    | `scripts/dev/agent-bootstrap-secrets.sh` | Bash       | One-time secrets population                |

### Components to Modify

| Component | Path                              | Change                        |
| --------- | --------------------------------- | ----------------------------- |
| Makefile  | `Makefile`                        | Add agent-\* targets          |
| Dev docs  | `docs/development-environment.md` | Add agent environment section |

### API Changes

New API Gateway endpoints (IAM-authenticated):

| Method | Path                         | Description                                            |
| ------ | ---------------------------- | ------------------------------------------------------ |
| POST   | `/environments`              | Create new agent environment (proxy + agent task pair) |
| GET    | `/environments`              | List active environments                               |
| DELETE | `/environments/{id}`         | Destroy environment (delete CF stack)                  |
| POST   | `/environments/{id}/refresh` | Re-mint IP-bound STS credentials                       |

## Testing Strategy

- **Proxy unit tests** (`cmd/egress-proxy/`): domain allowlist matching, credential injection, log redaction. Run with `go test ./...`.
- **CF template validation**: `aws cloudformation validate-template` for both templates.
- **End-to-end in real AWS**: deploy shared infra stack and a test environment, run through acceptance criteria (AC-1 through AC-11), then tear down. The real deployment IS the integration test for a PoC.
- **E2e automation**: deferred to future — automated test that creates environment, runs verification, tears down.

## Deployment Considerations

- **One-time setup**: deploy shared infra stack, bootstrap secrets, push container images.
- **Per-update**: rebuild and push container images, update Lambda code via CF stack update.
- **Region**: single region initially, chosen based on proximity and ECS Fargate availability.
- **Cost**: two Fargate tasks per session (proxy is lightweight, agent needs more CPU/memory for Claude CLI). Tasks only run when a session is active.
- **Cleanup**: orphaned CF stacks should be cleaned up. Consider a scheduled Lambda that deletes stacks older than a configurable TTL.
