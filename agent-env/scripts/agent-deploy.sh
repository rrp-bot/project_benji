#!/usr/bin/env bash
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_ENV_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo "us-east-1")}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"

PROXY_ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/agent-egress-proxy"
AGENT_ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/agent-env"
PROVISIONER_ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/agent-provisioner"
PROXY_DIR="${AGENT_ENV_DIR}/../egress-proxy"
PROVISIONER_DIR="${AGENT_ENV_DIR}/provisioner"
CF_DIR="${AGENT_ENV_DIR}/cloudformation"

COMMAND="${1:-help}"
shift || true

case "${COMMAND}" in
    proxy-build)
        echo "Building egress proxy image..."
        docker build -t agent-egress-proxy "${PROXY_DIR}"
        echo "Done."
        ;;

    agent-build)
        echo "Building agent environment image..."
        docker build -t agent-env "${AGENT_ENV_DIR}"
        echo "Done."
        ;;

    provisioner-build)
        echo "Building provisioner image..."
        docker build -t agent-provisioner -f "${PROVISIONER_DIR}/Dockerfile" "${AGENT_ENV_DIR}"
        echo "Done."
        ;;

    images-build)
        echo "Building all images..."
        docker build -t agent-egress-proxy "${PROXY_DIR}" &
        docker build -t agent-env "${AGENT_ENV_DIR}" &
        docker build -t agent-provisioner -f "${PROVISIONER_DIR}/Dockerfile" "${AGENT_ENV_DIR}" &
        wait
        echo "Done."
        ;;

    images-push)
        echo "Logging into ECR..."
        aws ecr get-login-password --region "${AWS_REGION}" | \
            docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

        for repo in agent-egress-proxy agent-env agent-provisioner; do
            if ! aws ecr describe-repositories --repository-names "${repo}" --region "${AWS_REGION}" >/dev/null 2>&1; then
                echo "Creating ECR repository: ${repo}"
                aws ecr create-repository --repository-name "${repo}" --region "${AWS_REGION}" \
                    --image-scanning-configuration scanOnPush=true >/dev/null
            fi
        done

        echo "Pushing proxy image..."
        docker tag agent-egress-proxy "${PROXY_ECR_URI}:latest"
        docker push "${PROXY_ECR_URI}:latest"

        echo "Pushing agent image..."
        docker tag agent-env "${AGENT_ECR_URI}:latest"
        docker push "${AGENT_ECR_URI}:latest"

        echo "Pushing provisioner image..."
        docker tag agent-provisioner "${PROVISIONER_ECR_URI}:latest"
        docker push "${PROVISIONER_ECR_URI}:latest"

        echo "Updating Lambda function code..."
        aws lambda update-function-code \
            --function-name agent-provisioner \
            --image-uri "${PROVISIONER_ECR_URI}:latest" \
            --region "${AWS_REGION}" >/dev/null 2>&1 && echo "Lambda updated." || echo "Lambda not yet deployed, skipping update."

        echo "Done."
        ;;

    infra-deploy)
        STACK_NAME="agent-shared-infra"
        TEMPLATE="${CF_DIR}/agent-shared-infra.yaml"

        echo "Deploying shared infrastructure stack: ${STACK_NAME}"
        echo "Region: ${AWS_REGION}"

        if aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1; then
            echo "Stack exists, updating..."
            aws cloudformation update-stack \
                --stack-name "${STACK_NAME}" \
                --template-body "file://${TEMPLATE}" \
                --capabilities CAPABILITY_NAMED_IAM \
                --region "${AWS_REGION}" \
                "$@" 2>&1 || {
                    echo "No updates needed or update failed."
                }
            echo "Waiting for update to complete..."
            aws cloudformation wait stack-update-complete \
                --stack-name "${STACK_NAME}" \
                --region "${AWS_REGION}" 2>/dev/null || true
        else
            echo "Creating stack..."
            aws cloudformation create-stack \
                --stack-name "${STACK_NAME}" \
                --template-body "file://${TEMPLATE}" \
                --capabilities CAPABILITY_NAMED_IAM \
                --region "${AWS_REGION}" \
                "$@"
            echo "Waiting for creation to complete..."
            aws cloudformation wait stack-create-complete \
                --stack-name "${STACK_NAME}" \
                --region "${AWS_REGION}"
        fi

        API_ID="$(aws cloudformation describe-stacks \
            --stack-name "${STACK_NAME}" \
            --region "${AWS_REGION}" \
            --query 'Stacks[0].Outputs[?OutputKey==`ApiGatewayUrl`].OutputValue' \
            --output text | grep -o '[a-z0-9]*\.execute-api' | cut -d. -f1)"
        if [[ -n "${API_ID}" ]]; then
            echo "Redeploying API Gateway..."
            aws apigateway create-deployment \
                --rest-api-id "${API_ID}" \
                --stage-name v1 \
                --region "${AWS_REGION}" >/dev/null
        fi

        echo ""
        echo "Stack outputs:"
        aws cloudformation describe-stacks \
            --stack-name "${STACK_NAME}" \
            --region "${AWS_REGION}" \
            --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
            --output table
        ;;

    infra-destroy)
        STACK_NAME="agent-shared-infra"
        echo "Deleting shared infrastructure stack: ${STACK_NAME}"
        read -rp "Are you sure? This will delete all shared agent infrastructure. [y/N] " confirm
        [[ "${confirm}" =~ ^[Yy]$ ]] || die "Aborted."

        aws cloudformation delete-stack \
            --stack-name "${STACK_NAME}" \
            --region "${AWS_REGION}"
        echo "Waiting for deletion..."
        aws cloudformation wait stack-delete-complete \
            --stack-name "${STACK_NAME}" \
            --region "${AWS_REGION}"
        echo "Done."
        ;;

    env-create)
        API_URL="$(aws cloudformation describe-stacks \
            --stack-name agent-shared-infra \
            --region "${AWS_REGION}" \
            --query 'Stacks[0].Outputs[?OutputKey==`ApiGatewayUrl`].OutputValue' \
            --output text)"

        DEVELOPER="${USER:-unknown}"
        echo "Creating agent environment for ${DEVELOPER}..."

        CREATE_RESP="$(awscurl --service execute-api \
            --region "${AWS_REGION}" \
            -X POST \
            -d "{\"developer\": \"${DEVELOPER}\"}" \
            "${API_URL}/environments")"
        echo "${CREATE_RESP}"

        ENV_ID="$(echo "${CREATE_RESP}" | python3 -c "import sys,json; print(json.load(sys.stdin)['environment_id'])")"
        echo ""
        echo "Environment ${ENV_ID} creation started. Waiting for it to be ready..."

        while true; do
            sleep 15
            STATUS_RESP="$(awscurl --service execute-api \
                --region "${AWS_REGION}" \
                "${API_URL}/environments/${ENV_ID}" 2>/dev/null)"
            STATUS="$(echo "${STATUS_RESP}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null)"

            echo "  Status: ${STATUS}"

            case "${STATUS}" in
                ready)
                    echo ""
                    echo "Environment ready. Provisioning (credentials, proxy CA, proxy env)..."
                    PROV_RESP="$(awscurl --service execute-api \
                        --region "${AWS_REGION}" \
                        -X POST \
                        "${API_URL}/environments/${ENV_ID}/provision")"
                    echo "${PROV_RESP}" | python3 -m json.tool
                    break
                    ;;
                failed)
                    echo ""
                    echo "Environment creation failed."
                    echo "${STATUS_RESP}" | python3 -m json.tool
                    exit 1
                    ;;
                creating|waiting_for_tasks)
                    continue
                    ;;
                *)
                    echo "Unexpected status: ${STATUS}"
                    echo "${STATUS_RESP}"
                    exit 1
                    ;;
            esac
        done
        ;;

    env-list)
        API_URL="$(aws cloudformation describe-stacks \
            --stack-name agent-shared-infra \
            --region "${AWS_REGION}" \
            --query 'Stacks[0].Outputs[?OutputKey==`ApiGatewayUrl`].OutputValue' \
            --output text)"

        awscurl --service execute-api \
            --region "${AWS_REGION}" \
            "${API_URL}/environments"
        echo ""
        ;;

    env-destroy)
        ENV_ID="${1:-}"
        [[ -n "${ENV_ID}" ]] || die "Usage: agent-deploy.sh env-destroy <env-id>"

        API_URL="$(aws cloudformation describe-stacks \
            --stack-name agent-shared-infra \
            --region "${AWS_REGION}" \
            --query 'Stacks[0].Outputs[?OutputKey==`ApiGatewayUrl`].OutputValue' \
            --output text)"

        echo "Destroying environment ${ENV_ID}..."
        awscurl --service execute-api \
            --region "${AWS_REGION}" \
            -X DELETE \
            "${API_URL}/environments/${ENV_ID}"
        echo ""
        ;;

    env-refresh)
        ENV_ID="${1:-}"
        [[ -n "${ENV_ID}" ]] || die "Usage: agent-deploy.sh env-refresh <env-id>"

        API_URL="$(aws cloudformation describe-stacks \
            --stack-name agent-shared-infra \
            --region "${AWS_REGION}" \
            --query 'Stacks[0].Outputs[?OutputKey==`ApiGatewayUrl`].OutputValue' \
            --output text)"

        echo "Refreshing credentials for ${ENV_ID}..."
        awscurl --service execute-api \
            --region "${AWS_REGION}" \
            -X POST \
            "${API_URL}/environments/${ENV_ID}/refresh"
        echo ""
        ;;

    status)
        echo "=== Shared Infrastructure ==="
        aws cloudformation describe-stacks \
            --stack-name agent-shared-infra \
            --region "${AWS_REGION}" \
            --query 'Stacks[0].{Status:StackStatus,Created:CreationTime}' \
            --output table 2>/dev/null || echo "Not deployed."

        echo ""
        echo "=== ECR Images ==="
        for repo in agent-egress-proxy agent-env; do
            echo -n "  ${repo}: "
            aws ecr describe-images \
                --repository-name "${repo}" \
                --region "${AWS_REGION}" \
                --query 'imageDetails | sort_by(@, &imagePushedAt) | [-1].{Tag:imageTags[0],Pushed:imagePushedAt}' \
                --output text 2>/dev/null || echo "no images"
        done

        echo ""
        echo "=== Active Environments ==="
        aws cloudformation list-stacks \
            --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
            --region "${AWS_REGION}" \
            --query "StackSummaries[?starts_with(StackName,'agent-env-')].[StackName,StackStatus,CreationTime]" \
            --output table 2>/dev/null || echo "None."
        ;;

    help|*)
        cat <<'USAGE'
Agent VM Isolation — Deploy & Manage

Build:
  proxy-build          Build egress proxy container image
  agent-build          Build agent environment container image
  provisioner-build    Build provisioner Lambda container image
  images-build         Build all images in parallel

Push:
  images-push          Push all images to ECR

Infrastructure:
  infra-deploy         Deploy (or update) shared infrastructure CF stack
  infra-destroy        Delete shared infrastructure stack

Environments:
  env-create           Create a new agent environment
  env-list             List active environments
  env-destroy <id>     Destroy an environment
  env-refresh <id>     Refresh STS credentials

Status:
  status               Show infrastructure and environment status

Environment variables:
  AWS_REGION           AWS region (default: from aws config)
  AWS_ACCOUNT_ID       AWS account ID (default: from sts get-caller-identity)
USAGE
        ;;
esac
