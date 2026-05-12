# Context: Requirements

## Questions & Answers

### Discovery Questions

### Q1: Should the agent environment run as AWS-managed ECS tasks (vs. local VMs or standalone EC2)?

**Default if unknown:** Yes — ECS provides disposable, session-isolated compute with native Secrets Manager integration and CloudFormation lifecycle management.
**Answer:** Yes. After iterating through local VMs (Vagrant/QEMU) and standalone EC2, the final architecture uses ECS tasks. The proxy runs on Fargate, and the agent runs on an EC2 capacity provider (for privileged mode / podman support).

### Q2: Should provisioning be a single API call that stands up both tasks, networking, and credentials?

**Default if unknown:** Yes — a single `POST /environments` call creates a CloudFormation stack containing both the proxy and agent tasks as a pair.
**Answer:** Yes. An API Gateway + Lambda provisioning service handles environment lifecycle. `POST /environments` creates the CF stack, discovers the agent's public IP, mints IP-bound STS sessions, and injects credentials via ECS Exec.

### Q3: Should the egress proxy be a standalone Go module in this repository (e.g., under `cmd/egress-proxy/`)?

**Default if unknown:** Yes — it's custom code specific to this project.
**Answer:** Yes, in this repo under cmd/egress-proxy/. Container image pushed to ECR.

### Q4: How should the agent access the ephemeral provider tooling?

**Default if unknown:** Run the CI container image via podman inside the agent task.
**Answer:** The agent task runs on an EC2 capacity provider in privileged mode so it can run podman. It executes the existing `rosa-regional-ci` container image via podman to access the ephemeral provider tooling, replicating the local developer workflow.

### Q5: Should each developer session get its own isolated proxy + agent pair?

**Default if unknown:** Yes — per-session isolation prevents cross-session credential leakage if a proxy is compromised.
**Answer:** Yes. Each `POST /environments` creates a new CloudFormation stack with a dedicated proxy + agent pair. No shared proxy. Sessions are fully isolated.

## Context Gathering Results

### Existing Codebase Patterns

- **No Go module exists** — the egress proxy will be the first Go code in the repo. A new `go.mod` under `cmd/egress-proxy/` is needed.
- **Existing `cli-proxy/` pattern** — a credential-isolating Unix socket daemon (Python) that wraps CLI tools like `gh`. Uses a sidecar container to hold credentials, with a policy engine for dangerous command denial. Similar concept to the egress proxy but container-based. Will likely be removed eventually; the egress proxy is the replacement approach.
- **Ephemeral provider** — Python orchestrator at `ci/ephemeral-provider/` that provisions/tears down dev environments. Needs 7 Vault credential keys (3 AWS accounts + GitHub token). Runs inside the `rosa-regional-ci` container image (UBI9-based with Terraform, Helm, AWS CLI, etc.).
- **Vault integration** — OIDC login to `vault.ci.openshift.org`, fetches credentials from KV store. The modified `ephemeral-env.sh` already supports pre-set env vars to skip OIDC. In the ECS architecture, raw credentials are stored in AWS Secrets Manager instead of Vault.
- **AWS three-account model** — Central (pipeline provisioner, STS AssumeRole), Regional (RC infra), Management (MC infra). STS credentials are temporary with session tokens.
- **Makefile** — 20+ ephemeral targets already exist. New `agent-*` targets should follow the same pattern.
- **CI image** — `rosa-regional-ci` built from `ci/Containerfile`. The agent task runs this via podman rather than installing tools directly.
- **Infrastructure as CloudFormation** — All infrastructure (shared and per-session) is defined as CloudFormation stacks. This aligns with disposable environments and clean teardown via `delete-stack`.

### Expert Questions

### Q6: Should the egress proxy's domain allowlist be hardcoded in the Go binary, or loaded from a config file at startup?

**Default if unknown:** Hardcoded — the allowlist is small and static (github.com, api.github.com, \*.googleapis.com), and a config file adds complexity for no benefit.
**Answer:** Hardcoded is simpler for the initial PoC implementation. The agent has no access to the proxy container's filesystem anyway.

### Q7: Should the existing `cli-proxy/` credential isolation pattern be replaced by or integrated with the egress proxy, or should they coexist as separate approaches?

**Default if unknown:** Coexist — `cli-proxy/` handles CLI tool wrapping (gh, aws) with policy enforcement, while the egress proxy handles HTTPS traffic credential injection. They solve different problems.
**Answer:** Leave cli-proxy/ in place for now — it's likely to be removed eventually. The egress proxy is the replacement approach.

### Q8: How should credentials flow into the ECS tasks?

**Default if unknown:** Secrets Manager with ECS native secrets integration for the proxy, and IP-bound STS sessions injected via ECS Exec for the agent.
**Answer:** Two credential paths:

1. **Proxy task**: GitHub token and GCP service account JSON are stored in Secrets Manager and injected via ECS native secrets integration (task definition `secrets` block → env vars at startup).
2. **Agent task**: The provisioning Lambda reads raw AWS credentials from Secrets Manager, creates IP-bound STS sessions scoped to the agent task's public IP, and injects them into the agent task via ECS Exec (writes to `~/.aws/credentials` with named profiles). The provisioning service is the sole credential minter.

### Q9: Should the proxy task be persistent (shared across sessions) or per-session (paired with each agent)?

**Default if unknown:** Per-session — prevents cross-session credential leakage if a proxy is compromised.
**Answer:** Per-session. Each deployment creates its own proxy + agent pair in a dedicated CloudFormation stack. A compromised proxy cannot leak credentials to other sessions.
