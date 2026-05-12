#!/usr/bin/env bash
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }

command -v aws >/dev/null 2>&1 || die "aws CLI is required"

AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo "us-east-1")}"

SECRETS=(
    "agent-proxy/github-token:GitHub personal access token (repo scope)"
    "agent-proxy/gcp-sa-json:Google service account JSON (paste single line or path to file)"
)

echo "Agent VM Isolation — Bootstrap Secrets"
echo "======================================="
echo "Region: ${AWS_REGION}"
echo ""
echo "This script populates Secrets Manager with the credentials needed"
echo "for agent VM isolation. The secrets must already exist (created by"
echo "the agent-shared-infra CloudFormation stack)."
echo ""

for entry in "${SECRETS[@]}"; do
    secret_name="${entry%%:*}"
    description="${entry#*:}"

    echo "---"
    echo "${description}"
    echo "Secret: ${secret_name}"

    if [[ "${secret_name}" == "agent-proxy/gcp-sa-json" ]]; then
        echo "Enter path to JSON file, or paste the JSON string:"
        read -r value
        if [[ -f "${value}" ]]; then
            value=$(cat "${value}")
        fi
    else
        echo -n "Value: "
        read -rs value
        echo ""
    fi

    if [[ -z "${value}" ]]; then
        echo "SKIPPED (empty value)"
        continue
    fi

    aws secretsmanager put-secret-value \
        --region "${AWS_REGION}" \
        --secret-id "${secret_name}" \
        --secret-string "${value}" \
        --output text --query Name

    echo "OK"
done

echo ""
echo "Bootstrap complete. Verify with:"
echo "  aws secretsmanager list-secrets --region ${AWS_REGION} --filter Key=name,Values=agent-"
