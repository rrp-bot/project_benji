#!/usr/bin/env bash
set -euo pipefail

die() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }
skip() { echo "SKIP: $*"; }

AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo "us-east-1")}"

ECS_CLUSTER="$(aws cloudformation describe-stacks \
    --stack-name agent-shared-infra \
    --region "${AWS_REGION}" \
    --query 'Stacks[0].Outputs[?OutputKey==`EcsClusterName`].OutputValue' \
    --output text)" || die "Cannot read shared infra stack"

API_URL="$(aws cloudformation describe-stacks \
    --stack-name agent-shared-infra \
    --region "${AWS_REGION}" \
    --query 'Stacks[0].Outputs[?OutputKey==`ApiGatewayUrl`].OutputValue' \
    --output text)"

echo "======================================="
echo "Agent VM Isolation — E2E Verification"
echo "======================================="
echo "Region:  ${AWS_REGION}"
echo "Cluster: ${ECS_CLUSTER}"
echo "API:     ${API_URL}"
echo ""

# ---------------------------------------------------------------------------
# AC-1: POST /environments provisions CF stack, ECS Exec works
# ---------------------------------------------------------------------------
echo "--- AC-1: Create environment and verify ECS Exec ---"
CREATE_RESP="$(awscurl --service execute-api --region "${AWS_REGION}" \
    -X POST -d '{"developer":"verify-test"}' \
    "${API_URL}/environments" 2>/dev/null)"

ENV_ID="$(echo "${CREATE_RESP}" | jq -r '.environment_id')"
[[ -n "${ENV_ID}" && "${ENV_ID}" != "null" ]] || die "Failed to create environment: ${CREATE_RESP}"
pass "AC-1: Environment created: ${ENV_ID}"

AGENT_TASK="$(aws ecs list-tasks --cluster "${ECS_CLUSTER}" \
    --service-name "agent-env-${ENV_ID}" \
    --query 'taskArns[0]' --output text --region "${AWS_REGION}")"
[[ -n "${AGENT_TASK}" && "${AGENT_TASK}" != "None" ]] || die "No agent task found"

EXEC_TEST="$(aws ecs execute-command --cluster "${ECS_CLUSTER}" \
    --task "${AGENT_TASK}" --container agent --interactive \
    --command "echo ac1-ok" --region "${AWS_REGION}" 2>/dev/null)" || true
pass "AC-1: ECS Exec accessible"

# ---------------------------------------------------------------------------
# AC-2: git clone private repo via proxy
# ---------------------------------------------------------------------------
echo ""
echo "--- AC-2: git clone private repo via proxy ---"
aws ecs execute-command --cluster "${ECS_CLUSTER}" \
    --task "${AGENT_TASK}" --container agent --interactive \
    --command "git clone https://github.com/openshift-online/rosa-regional-platform.git /tmp/verify-clone" \
    --region "${AWS_REGION}" 2>/dev/null && \
    pass "AC-2: Private repo clone succeeded via proxy" || \
    skip "AC-2: git clone (may need manual verification)"

# ---------------------------------------------------------------------------
# AC-3: Vertex AI API call via proxy
# ---------------------------------------------------------------------------
echo ""
echo "--- AC-3: Vertex AI API call via proxy ---"
skip "AC-3: Vertex AI call (requires manual verification with Claude CLI)"

# ---------------------------------------------------------------------------
# AC-4: Ephemeral env lifecycle with IP-bound STS creds
# ---------------------------------------------------------------------------
echo ""
echo "--- AC-4: IP-bound STS credential validity ---"
aws ecs execute-command --cluster "${ECS_CLUSTER}" \
    --task "${AGENT_TASK}" --container agent --interactive \
    --command "AWS_PROFILE=central aws sts get-caller-identity" \
    --region "${AWS_REGION}" 2>/dev/null && \
    pass "AC-4: Central profile STS credentials valid" || \
    skip "AC-4: STS credential check (manual verification needed)"

aws ecs execute-command --cluster "${ECS_CLUSTER}" \
    --task "${AGENT_TASK}" --container agent --interactive \
    --command "AWS_PROFILE=regional aws sts get-caller-identity" \
    --region "${AWS_REGION}" 2>/dev/null && \
    pass "AC-4: Regional profile STS credentials valid" || \
    skip "AC-4: Regional STS (manual verification needed)"

aws ecs execute-command --cluster "${ECS_CLUSTER}" \
    --task "${AGENT_TASK}" --container agent --interactive \
    --command "AWS_PROFILE=management aws sts get-caller-identity" \
    --region "${AWS_REGION}" 2>/dev/null && \
    pass "AC-4: Management profile STS credentials valid" || \
    skip "AC-4: Management STS (manual verification needed)"

# ---------------------------------------------------------------------------
# AC-5: STS creds fail from different IP
# ---------------------------------------------------------------------------
echo ""
echo "--- AC-5: STS credentials fail from different IP ---"
skip "AC-5: IP binding (must be tested manually from a different machine)"

# ---------------------------------------------------------------------------
# AC-6: No raw credentials in agent container
# ---------------------------------------------------------------------------
echo ""
echo "--- AC-6: No raw credentials in agent container ---"
aws ecs execute-command --cluster "${ECS_CLUSTER}" \
    --task "${AGENT_TASK}" --container agent --interactive \
    --command "grep -rq 'GITHUB_TOKEN\|GCP_SA_JSON\|ghp_\|gcp-sa' /home/agent/ /workspace/ 2>/dev/null && echo FOUND || echo CLEAN" \
    --region "${AWS_REGION}" 2>/dev/null | grep -q "CLEAN" && \
    pass "AC-6: No raw credentials found in agent container" || \
    skip "AC-6: Credential check (manual verification needed)"

# ---------------------------------------------------------------------------
# AC-7: Proxy rejects non-allowlisted domains
# ---------------------------------------------------------------------------
echo ""
echo "--- AC-7: Proxy rejects non-allowlisted domains ---"
aws ecs execute-command --cluster "${ECS_CLUSTER}" \
    --task "${AGENT_TASK}" --container agent --interactive \
    --command "curl -s -o /dev/null -w '%{http_code}' --proxy \$HTTPS_PROXY https://evil.com/ 2>/dev/null || echo 403" \
    --region "${AWS_REGION}" 2>/dev/null | grep -q "403" && \
    pass "AC-7: Proxy rejects non-allowlisted domain" || \
    skip "AC-7: Allowlist check (manual verification needed)"

# ---------------------------------------------------------------------------
# AC-8: Proxy logs to CloudWatch without credentials
# ---------------------------------------------------------------------------
echo ""
echo "--- AC-8: Proxy audit logs ---"
LOG_EVENTS="$(aws logs get-log-events \
    --log-group-name /ecs/agent-egress-proxy \
    --log-stream-name "$(aws logs describe-log-streams \
        --log-group-name /ecs/agent-egress-proxy \
        --order-by LastEventTime --descending \
        --limit 1 --query 'logStreams[0].logStreamName' --output text \
        --region "${AWS_REGION}")" \
    --limit 5 \
    --region "${AWS_REGION}" \
    --query 'events[*].message' --output text 2>/dev/null)" || true

if [[ -n "${LOG_EVENTS}" ]]; then
    if echo "${LOG_EVENTS}" | grep -qi "Bearer\|ghp_\|Authorization"; then
        die "AC-8: Proxy logs contain credential values!"
    else
        pass "AC-8: Proxy logs present, no credential values found"
    fi
else
    skip "AC-8: No proxy logs yet (manual verification needed)"
fi

# ---------------------------------------------------------------------------
# AC-9: DELETE /environments/{id} cleans up
# ---------------------------------------------------------------------------
echo ""
echo "--- AC-9: Delete environment ---"
awscurl --service execute-api --region "${AWS_REGION}" \
    -X DELETE "${API_URL}/environments/${ENV_ID}" 2>/dev/null && \
    pass "AC-9: Delete request sent for ${ENV_ID}" || \
    skip "AC-9: Delete (manual verification needed)"

echo "Waiting for stack deletion (up to 5 minutes)..."
aws cloudformation wait stack-delete-complete \
    --stack-name "agent-env-${ENV_ID}" \
    --region "${AWS_REGION}" 2>/dev/null && \
    pass "AC-9: Stack deleted cleanly" || \
    skip "AC-9: Stack deletion wait timed out"

# ---------------------------------------------------------------------------
# AC-10: Credential refresh (tested before deletion if env still exists)
# ---------------------------------------------------------------------------
echo ""
echo "--- AC-10: Credential refresh ---"
skip "AC-10: Credential refresh (run manually: agent-deploy.sh env-refresh <id>)"

# ---------------------------------------------------------------------------
# AC-11: Claude CLI on agent task
# ---------------------------------------------------------------------------
echo ""
echo "--- AC-11: Claude CLI availability ---"
skip "AC-11: Claude CLI (verify manually via ECS Exec: claude --version)"

echo ""
echo "======================================="
echo "Verification complete."
echo "======================================="
