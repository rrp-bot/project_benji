# Requirements Specification: Agent VM Isolation

## Problem Statement

The spec-to-pr LLM agent autonomously implements, deploys, tests, and debugs code changes using credentials for AWS (ephemeral environment management), GitHub (repository access), and Google Cloud (Vertex AI API access). Unlike deterministic CI jobs, LLMs can be prompt-injected or behave unexpectedly, making credential exposure a first-order security concern. Even inside AWS, credentials available via instance metadata (IMDS) or task role credentials can be exfiltrated by the LLM and used from elsewhere. The agent must operate with strong credential isolation so that exfiltration of any credential from the agent's environment is either impossible or useless outside that environment.

## Solution Overview

An AWS-native architecture with three components managed by ECS:

- **Provisioning Service** — An API Gateway + Lambda (Python) that developers invoke with their AWS credentials to spin up agent dev environments. This is the only component that touches raw credentials. It reads secrets from AWS Secrets Manager, creates IP-bound STS sessions scoped to the agent task's public IP, and injects them into the agent container. The provisioning service is deterministic code, not an LLM.
- **Egress Proxy** — An ECS Fargate task running a custom Go TLS-terminating MITM forward proxy. Deployed per-session alongside the agent task. It reads GitHub tokens and Google service account credentials from Secrets Manager at startup (via ECS native secrets integration) and injects authentication headers into proxied requests. Per-session isolation prevents cross-session credential leakage.
- **Agent Task** — An ECS task on an EC2 capacity provider, running in privileged mode to support podman for container-in-container execution. This is the developer's working environment. Runs Claude CLI (interactive initially, autonomous later), the CI container image (`rosa-regional-ci`) via podman for ephemeral environment operations, and holds only IP-bound AWS STS credentials that are useless from any other IP. All GitHub and Google API traffic routes through the paired egress proxy task. The EC2 capacity provider is required because the agent must run the same container-based tooling that developers use locally (e.g., `ephemeral-env.sh` launching the CI container via podman).

Each dev environment is a CloudFormation stack containing both tasks as a pair, created and destroyed via the provisioning API.

**Evolution path:**

1. **Day 1:** Developer calls API → CF stack creates proxy + agent task pair → ECS Exec in → run Claude CLI interactively
2. **Day N:** Developer (or Jira webhook) calls API with a ticket + prompt → CF stack creates task pair → agent works autonomously → opens PR when done

## Functional Requirements

### FR-1: Egress Proxy (ECS Fargate Task, Per-Session)

- FR-1.1: Implement a TLS-terminating MITM forward proxy in Go that handles `CONNECT` tunnels.
- FR-1.2: Generate a CA certificate at startup for signing per-domain TLS certificates. The CA cert is made available to the paired agent task (via shared volume, S3, or Secrets Manager).
- FR-1.3: Inject `Authorization: Bearer <GITHUB_TOKEN>` for requests to `github.com` and `api.github.com`. Token is read from Secrets Manager at startup via ECS native secrets integration.
- FR-1.4: Manage full OAuth2 token lifecycle for Google Vertex AI (`*-aiplatform.googleapis.com`): read service account JSON from Secrets Manager at startup, handle JWT signing, token exchange, caching, and automatic refresh using `golang.org/x/oauth2/google`.
- FR-1.5: Enforce a domain allowlist — reject `CONNECT` requests to non-allowlisted domains before TLS is established.
- FR-1.6: Domain allowlist: `github.com`, `api.github.com`, `*.googleapis.com`.
- FR-1.7: Accept inbound connections only from the paired agent task (enforced via security group and/or task networking).
- FR-1.8: Log all proxied requests as structured JSON to stdout (CloudWatch Logs via ECS) with fields: timestamp (RFC3339), source_ip, destination, method, path, status, duration_ms, credential_type. Never log credential values, request/response bodies, or Authorization headers.
- FR-1.9: The proxy container image is pushed to ECR.
- FR-1.10: Expose a `/healthz` endpoint that verifies both GitHub token validity (via `api.github.com/rate_limit`) and Google OAuth2 token status. Returns 200 when healthy, 503 when degraded. Used by ECS health checks.
- FR-1.11: Each session gets its own proxy task. A compromised proxy cannot leak credentials to other sessions.

### FR-2: Agent (ECS Task on EC2 Capacity Provider)

- FR-2.1: Run the LLM agent (Claude SDK via Vertex AI) with `HTTPS_PROXY` configured to point at the paired proxy task.
- FR-2.2: Run the existing CI container image (`rosa-regional-ci`) via podman for the full ephemeral environment lifecycle — the agent operates as a real developer with an ephemeral account (provision, teardown, resync, shell, bastion, port-forwarding, e2e). The task runs in privileged mode on an EC2 capacity provider to support container-in-container execution.
- FR-2.3: Hold AWS STS credentials scoped with `aws:SourceIp` condition matching the task's public IP. Credentials are written to an AWS credentials file. The agent task role does NOT have ephemeral environment permissions — those come only from the IP-bound STS creds.
- FR-2.4: No GitHub token or Google service account credentials exist anywhere in the agent container.
- FR-2.5: Trust the paired proxy's CA certificate (injected during task startup).
- FR-2.6: Claude CLI is installed and available for interactive or automated use.
- FR-2.7: A workspace directory (e.g., `/workspace/`) supports multiple repository checkouts simultaneously.
- FR-2.8: Accessible via ECS Exec for interactive development.
- FR-2.9: Podman is installed in the agent container for running the CI container image, replicating the local developer workflow.

### FR-3: Provisioning Service

- FR-3.1: Expose an API (API Gateway + Lambda) authenticated with the developer's AWS IAM credentials.
- FR-3.2: On `POST /environments`: create a CloudFormation stack that provisions both the proxy and agent ECS tasks as a pair.
- FR-3.3: Read raw AWS credentials (for the three ephemeral accounts) from Secrets Manager. These are the static keys that were previously in Vault.
- FR-3.4: After the agent task is running, determine the egress IP that AWS APIs will see for outbound traffic. For agent tasks on the EC2 capacity provider (which do not get public IPs on task ENIs), this is the EC2 host instance's public IP or the NAT gateway EIP. Mint IP-bound STS sessions for all three accounts (central, regional, management) using `aws:SourceIp = <egress-IP>/32` as the session policy condition.
- FR-3.5: Inject the IP-bound STS credentials into the agent task via ECS Exec (`ExecuteCommand`). Write them to the AWS credentials file with named profiles (`central`, `regional`, `management`).
- FR-3.6: Configure the agent task's `HTTPS_PROXY` to point at the paired proxy task endpoint.
- FR-3.7: On `DELETE /environments/{id}`: delete the CloudFormation stack (stops both tasks, cleans up resources).
- FR-3.8: On `GET /environments`: list active environments with status, task IDs, IP, and creation time.
- FR-3.9: On `POST /environments/{id}/refresh`: re-mint IP-bound STS sessions and inject into the agent task without restarting. Enables sessions beyond the 12-hour STS limit.
- FR-3.10: The provisioning service is the only component that touches raw credentials from Secrets Manager. It is deterministic code, not an LLM.

### FR-4: CloudFormation Stack

- FR-4.1: Each dev environment is a self-contained CloudFormation stack.
- FR-4.2: Stack includes: ECS task definitions for both proxy (Fargate) and agent (EC2 capacity provider, privileged mode), security groups, IAM task roles, and networking configuration.
- FR-4.3: Stack parameters: task size (CPU/memory), repos to clone, session duration, developer identity tag.
- FR-4.4: Stack outputs: task ARNs, ECS cluster, agent egress IP, ECS Exec connection details.
- FR-4.5: The stack is disposable — `delete-stack` cleanly removes all resources.
- FR-4.6: Both tasks run in the same VPC/subnet with awsvpc networking.
- FR-4.7: Networking: the agent security group allows egress only to the proxy task and AWS API endpoints. The EC2 host instance's public IP or NAT gateway EIP serves as the deterministic egress IP for STS `aws:SourceIp` binding. No unrestricted internet access from the agent.

### FR-5: Secrets Management

- FR-5.1: Raw credentials (AWS account keys, GitHub PAT, GCP service account JSON) are stored in AWS Secrets Manager.
- FR-5.2: A one-time bootstrap step populates Secrets Manager with these credentials (manual via console/CLI or a `make agent-bootstrap-secrets` target).
- FR-5.3: The provisioning service's IAM role has `secretsmanager:GetSecretValue` for the specific secret ARNs.
- FR-5.4: The egress proxy's ECS task role has `secretsmanager:GetSecretValue` for the GitHub and GCP secret ARNs only.
- FR-5.5: Agent tasks have NO access to Secrets Manager.

## Technical Requirements

### TR-1: Security

- TR-1.1: AWS STS sessions use the shortest practical session duration to limit the window of misuse.
- TR-1.2: The STS role's IAM policy covers the full ephemeral environment lifecycle (provision, teardown, resync, shell, bastion, port-forwarding, e2e). The agent operates as a real developer with an ephemeral account. IAM write permissions (GetRole, UpdateAssumeRolePolicy) are excluded; cross-account trust policies are pre-configured.
- TR-1.3: All three AWS account profiles (central, regional, management) must have IP-scoped STS sessions. IP binding propagates through AssumeRole chains automatically. See `iam-trust-chain.yaml` for the complete trust chain specification.
- TR-1.4: The proxy's CA private key exists only within the proxy container/task.
- TR-1.5: Non-allowlisted destinations are rejected at the `CONNECT` stage before TLS is established.
- TR-1.6: The proxy task security group accepts inbound connections only from the paired agent task.
- TR-1.7: Agent tasks have no task role with ephemeral environment permissions. The task role is minimal (ECS Exec support only).
- TR-1.8: The provisioning service is the sole credential minter — no other component touches raw secrets.
- TR-1.9: Agent task egress is restricted via security group to the proxy task and AWS API endpoints only.
- TR-1.10: Per-session proxy isolation — each agent gets its own proxy. A compromised proxy cannot affect other sessions.

### TR-2: Performance

- TR-2.1: Network latency between agent and proxy tasks is within the same VPC/subnet — sub-millisecond.
- TR-2.2: The proxy adds minimal overhead beyond TLS termination and re-encryption.

### TR-3: Operability

- TR-3.1: Health checks verify: proxy reachability from agent, STS credential validity, GitHub/Google API connectivity through proxy.
- TR-3.2: Both tasks are disposable and fast to provision via CloudFormation.
- TR-3.3: CloudWatch Logs captures proxy audit logs and agent task logs.
- TR-3.4: The provisioning API provides environment listing, status, and credential refresh.

## Acceptance Criteria

- AC-1: Calling `POST /environments` provisions a CF stack with both tasks, and the developer can ECS Exec into the agent task.
- AC-2: From the agent task, `git clone` of a private repo succeeds via the proxy.
- AC-3: From the agent task, Vertex AI API calls succeed via the proxy (OAuth2 token refresh works transparently).
- AC-4: The agent can perform the full ephemeral environment lifecycle (provision, resync, teardown) with IP-bound STS credentials.
- AC-5: Attempting to use the AWS STS credentials from a different IP address fails with an access denied error.
- AC-6: No GitHub token, Google service account JSON, or raw AWS keys exist anywhere in the agent container (verified by grep/find).
- AC-7: The proxy rejects CONNECT requests to domains not on the allowlist.
- AC-8: The proxy logs all proxied requests to CloudWatch without logging credential values.
- AC-9: `DELETE /environments/{id}` cleanly removes the CF stack and all resources.
- AC-10: `POST /environments/{id}/refresh` re-mints STS credentials without restarting the tasks.
- AC-11: Claude CLI runs successfully on the agent task and can interact with Vertex AI via the proxy.

## Constraints

- The ephemeral provider tooling (1,665 lines of Python, boto3, YAML deep-merge, concurrent pipeline polling) is impractical to rewrite — it must be used as-is via the CI container image or equivalent tooling installed in the agent container.
- STS credentials have a maximum lifetime (12 hours default); the provisioning service supports credential refresh to extend sessions.
- Infrastructure is defined as CloudFormation stacks.
- Single AWS region for initial deployment.
- This is PoC work — optimise for simplicity and speed of iteration.

### Dependencies

- AWS account with permissions to create: ECS, API Gateway, Lambda, Secrets Manager, CloudFormation, VPC resources, ECR
- Go toolchain for building the egress proxy container image
- CI tooling (from `rosa-regional-ci`) available as a container image in ECR or installed in the agent image
- Google service account JSON with Vertex AI access (stored in Secrets Manager)
- GitHub personal access token with repo scope (stored in Secrets Manager)
- Raw AWS credentials for the three ephemeral accounts (stored in Secrets Manager)
