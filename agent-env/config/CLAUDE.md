# Agent VM Isolation Environment

You are running inside an isolated ECS container with restricted network egress. This environment is designed for automated development workflows using the Claude SDK.

## Security Model

- **No credentials exist in this container.** All authentication (GitHub, Google Cloud, AWS) is handled transparently by the egress proxy at the network layer via TLS MITM. You do not need to configure tokens or credentials for these services.
- **GITHUB_TOKEN is a dummy value.** It exists only so tools like `gh` don't refuse to start. The proxy injects real credentials into requests to GitHub.
- **AWS credentials** are in `~/.aws/credentials` with profiles `central`, `regional`, and `management` — these are IP-bound STS sessions. Use `--profile <name>` with AWS CLI.

## Network Restrictions

All outbound traffic goes through an HTTPS egress proxy. Only allowlisted domains are reachable. If a request returns 403, the domain is not in the allowlist.

Allowed destinations:
- **GitHub**: github.com, api.github.com, codeload.github.com, cli.github.com, *.githubusercontent.com
- **Google Cloud**: *.googleapis.com (Vertex AI, GCS, etc.)
- **AWS**: *.amazonaws.com
- **Package registries**: registry.npmjs.org, pypi.org, files.pythonhosted.org, *.pythonhosted.org, astral.sh
- **Container registries**: *.redhat.com, *.quay.io
- **Tools**: get.helm.sh, dl.k8s.io, *.hashicorp.com, mirror.openshift.com

If you need a domain that is not listed, report it — do not try to bypass the proxy.

## Available Tools

- `git`, `gh` (GitHub CLI)
- `aws` (AWS CLI v2)
- `make`, `jq`, `openssl`, `openssh-clients`
- `podman` (rootless container builds)
- `node`, `npm`
- `claude` (Claude Code CLI)

Additional tools (terraform, helm, kubectl, uv, oc) can be installed at runtime — their download domains are in the allowlist.

## Development Workflow

This environment is intended for use with the Claude SDK to implement autonomous development loops:

1. **Clone** the target repository into `/workspace`
2. **Read** the spec or task description
3. **Implement** changes using available tools
4. **Test** using the project's test suite and `make` targets
5. **Commit and push** results

## Working Directory

`/workspace` is the default working directory. When working on a task, create a subdirectory for each repository checkout (e.g. `/workspace/rosa-regional-platform`, `/workspace/clm`). This keeps multi-repo work isolated and avoids conflicts when a task spans multiple projects.

## Proxy Configuration

The proxy is configured via environment variables (`HTTPS_PROXY`, `HTTP_PROXY`). The proxy CA certificate is installed in the system trust store at `/etc/pki/ca-trust/source/anchors/proxy-ca.crt`. If a tool or library requires an explicit CA bundle path, use this file. Do not modify proxy settings.
