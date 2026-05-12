"""Agent VM Isolation — Provisioning Lambda.

Handles environment lifecycle: create, list, delete, refresh credentials.
This is the sole credential minter — no other component touches raw secrets.
"""

import base64
import hashlib
import json
import os
import time

import boto3

ECS_CLUSTER = os.environ["ECS_CLUSTER"]
PROXY_SG_ID = os.environ["PROXY_SG_ID"]
AGENT_SG_ID = os.environ["AGENT_SG_ID"]
SUBNET_ID = os.environ["SUBNET_ID"]
PROXY_ECR_URI = os.environ["PROXY_ECR_URI"]
AGENT_ECR_URI = os.environ["AGENT_ECR_URI"]
PROXY_TASK_ROLE_ARN = os.environ["PROXY_TASK_ROLE_ARN"]
AGENT_TASK_ROLE_ARN = os.environ["AGENT_TASK_ROLE_ARN"]
EXECUTION_ROLE_ARN = os.environ["EXECUTION_ROLE_ARN"]

STACK_PREFIX = "agent-env-"
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "agent-environment.yaml")

def _read_template():
    with open(TEMPLATE_PATH) as f:
        return f.read()

cfn = boto3.client("cloudformation")
ecs = boto3.client("ecs")
ec2 = boto3.client("ec2")
sm = boto3.client("secretsmanager")
ssm = boto3.client("ssm")
sts_client = boto3.client("sts")


def lambda_handler(event, context):
    method = event["httpMethod"]
    path = event["resource"]

    if method == "POST" and path == "/environments":
        return create_environment(event)
    elif method == "GET" and path == "/environments":
        return list_environments()
    elif method == "GET" and path == "/environments/{id}":
        env_id = event["pathParameters"]["id"]
        return get_environment(env_id)
    elif method == "DELETE" and path == "/environments/{id}":
        env_id = event["pathParameters"]["id"]
        return delete_environment(env_id)
    elif method == "POST" and path == "/environments/{id}/provision":
        env_id = event["pathParameters"]["id"]
        return provision_environment(env_id)
    elif method == "POST" and path == "/environments/{id}/refresh":
        env_id = event["pathParameters"]["id"]
        return refresh_credentials(env_id)
    else:
        return response(404, {"error": "not found"})


def create_environment(event):
    body = json.loads(event.get("body") or "{}")
    developer = body.get("developer", "unknown")

    env_id = hashlib.sha256(
        f"{developer}-{time.time()}".encode()
    ).hexdigest()[:8]
    stack_name = f"{STACK_PREFIX}{env_id}"

    shared_stack = _get_shared_stack_outputs()

    params = [
        {"ParameterKey": "EnvironmentId", "ParameterValue": env_id},
        {"ParameterKey": "EcsCluster", "ParameterValue": ECS_CLUSTER},
        {"ParameterKey": "SubnetId", "ParameterValue": SUBNET_ID},
        {"ParameterKey": "ProxySecurityGroupId", "ParameterValue": PROXY_SG_ID},
        {"ParameterKey": "AgentSecurityGroupId", "ParameterValue": AGENT_SG_ID},
        {"ParameterKey": "ProxyImageUri", "ParameterValue": f"{PROXY_ECR_URI}:latest"},
        {"ParameterKey": "AgentImageUri", "ParameterValue": f"{AGENT_ECR_URI}:latest"},
        {"ParameterKey": "ProxyTaskRoleArn", "ParameterValue": PROXY_TASK_ROLE_ARN},
        {"ParameterKey": "AgentTaskRoleArn", "ParameterValue": AGENT_TASK_ROLE_ARN},
        {"ParameterKey": "ExecutionRoleArn", "ParameterValue": EXECUTION_ROLE_ARN},
        {"ParameterKey": "DeveloperIdentity", "ParameterValue": developer},
        {
            "ParameterKey": "SecretGithubTokenArn",
            "ParameterValue": shared_stack["SecretGithubTokenArn"],
        },
        {
            "ParameterKey": "SecretGcpSaJsonArn",
            "ParameterValue": shared_stack["SecretGcpSaJsonArn"],
        },
        {
            "ParameterKey": "CapacityProviderName",
            "ParameterValue": shared_stack["CapacityProviderName"],
        },
    ]

    if body.get("agent_cpu"):
        params.append(
            {"ParameterKey": "AgentCpu", "ParameterValue": str(body["agent_cpu"])}
        )
    if body.get("agent_memory"):
        params.append(
            {"ParameterKey": "AgentMemory", "ParameterValue": str(body["agent_memory"])}
        )

    cfn.create_stack(
        StackName=stack_name,
        TemplateBody=_read_template(),
        Parameters=params,
        Tags=[
            {"Key": "agent-env-id", "Value": env_id},
            {"Key": "developer", "Value": developer},
        ],
        Capabilities=["CAPABILITY_IAM"],
    )

    return response(202, {
        "environment_id": env_id,
        "stack_name": stack_name,
        "status": "creating",
    })


def get_environment(env_id):
    stack_name = f"{STACK_PREFIX}{env_id}"

    try:
        desc = cfn.describe_stacks(StackName=stack_name)
    except cfn.exceptions.ClientError:
        return response(404, {"error": f"environment {env_id} not found"})

    stack = desc["Stacks"][0]
    stack_status = stack["StackStatus"]

    if stack_status in ("CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS"):
        return response(200, {
            "environment_id": env_id,
            "status": "creating",
            "stack_status": stack_status,
        })

    if stack_status in ("CREATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_IN_PROGRESS"):
        return response(200, {
            "environment_id": env_id,
            "status": "failed",
            "stack_status": stack_status,
        })

    if stack_status not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        return response(200, {
            "environment_id": env_id,
            "status": stack_status,
        })

    tasks = ecs.list_tasks(
        cluster=ECS_CLUSTER,
        serviceName=f"agent-env-{env_id}",
        desiredStatus="RUNNING",
    )
    if not tasks.get("taskArns"):
        return response(200, {
            "environment_id": env_id,
            "status": "waiting_for_tasks",
            "stack_status": stack_status,
        })

    return response(200, {
        "environment_id": env_id,
        "status": "ready",
        "stack_status": stack_status,
        "ecs_exec_command": (
            f"aws ecs execute-command --cluster {ECS_CLUSTER} "
            f"--task $(aws ecs list-tasks --cluster {ECS_CLUSTER} "
            f"--service-name agent-env-{env_id} --query 'taskArns[0]' --output text) "
            f"--container agent --interactive --command /bin/bash"
        ),
    })


def list_environments():
    stacks = cfn.list_stacks(StackStatusFilter=["CREATE_COMPLETE", "UPDATE_COMPLETE"])
    envs = []
    for s in stacks.get("StackSummaries", []):
        if s["StackName"].startswith(STACK_PREFIX):
            env_id = s["StackName"][len(STACK_PREFIX):]
            envs.append({
                "environment_id": env_id,
                "stack_name": s["StackName"],
                "status": s["StackStatus"],
                "created": s["CreationTime"].isoformat(),
            })
    return response(200, {"environments": envs})


def delete_environment(env_id):
    stack_name = f"{STACK_PREFIX}{env_id}"
    cfn.delete_stack(StackName=stack_name)
    return response(200, {
        "environment_id": env_id,
        "status": "deleting",
    })


def provision_environment(env_id):
    egress_ip = _discover_egress_ip()
    skipped = []

    _wait_for_exec_ready(env_id)

    if _secrets_populated("central") and _secrets_populated("regional") and _secrets_populated("management"):
        _mint_and_inject_credentials(env_id, egress_ip)
    else:
        skipped.append("credentials")

    _inject_proxy_ca(env_id)
    proxy_endpoint = _get_proxy_endpoint(env_id)
    _configure_proxy_env(env_id, proxy_endpoint)
    _inject_env_vars(env_id)

    result = {
        "environment_id": env_id,
        "egress_ip": egress_ip,
        "proxy_endpoint": proxy_endpoint,
        "status": "provisioned",
    }
    if skipped:
        result["skipped"] = skipped
        result["status"] = "provisioned_partial"
    return response(200, result)


def refresh_credentials(env_id):
    if not (_secrets_populated("central") and _secrets_populated("regional") and _secrets_populated("management")):
        return response(400, {
            "environment_id": env_id,
            "error": "credential secrets not populated",
        })
    egress_ip = _discover_egress_ip()
    _mint_and_inject_credentials(env_id, egress_ip)
    return response(200, {
        "environment_id": env_id,
        "egress_ip": egress_ip,
        "status": "credentials_refreshed",
    })


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


SECRET_KEYS = {
    "central": ["agent-ephemeral/central-access-key", "agent-ephemeral/central-secret-key", "agent-ephemeral/central-assume-role-arn"],
    "regional": ["agent-ephemeral/regional-access-key", "agent-ephemeral/regional-secret-key"],
    "management": ["agent-ephemeral/management-access-key", "agent-ephemeral/management-secret-key"],
}


def _secrets_populated(account):
    for secret_id in SECRET_KEYS[account]:
        try:
            val = _get_secret(secret_id).strip()
            if not val or len(val) < 16:
                return False
        except Exception:
            return False
    return True


def _wait_for_exec_ready(env_id, timeout=120):
    """Wait until ECS Exec agent is running in the agent task."""
    agent_task_arn = _get_agent_task_arn(env_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _exec_command(agent_task_arn, "agent", "/bin/true")
            return
        except Exception:
            time.sleep(5)
    raise RuntimeError(f"ECS Exec not ready for {env_id} after {timeout}s")


def _get_shared_stack_outputs():
    resp = cfn.describe_stacks(StackName="agent-shared-infra")
    outputs = {}
    for o in resp["Stacks"][0].get("Outputs", []):
        outputs[o["OutputKey"]] = o["OutputValue"]
    return outputs


def _wait_for_services(env_id):
    """Wait for both proxy and agent services to have running tasks."""
    for service_name in [f"agent-proxy-{env_id}", f"agent-env-{env_id}"]:
        for _ in range(60):
            tasks = ecs.list_tasks(
                cluster=ECS_CLUSTER, serviceName=service_name, desiredStatus="RUNNING"
            )
            if tasks.get("taskArns"):
                break
            time.sleep(5)


def _discover_egress_ip():
    """Discover the EC2 host instance's public IP (egress IP for agent tasks)."""
    container_instances = ecs.list_container_instances(cluster=ECS_CLUSTER)
    if not container_instances.get("containerInstanceArns"):
        raise RuntimeError("No container instances in cluster")

    ci_detail = ecs.describe_container_instances(
        cluster=ECS_CLUSTER,
        containerInstances=[container_instances["containerInstanceArns"][0]],
    )
    instance_id = ci_detail["containerInstances"][0]["ec2InstanceId"]

    instances = ec2.describe_instances(InstanceIds=[instance_id])
    return instances["Reservations"][0]["Instances"][0]["PublicIpAddress"]


def _get_secret(secret_id):
    return sm.get_secret_value(SecretId=secret_id)["SecretString"]


IP_RESTRICTION_POLICY = """{
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "DenyFromNonAgentIP",
        "Effect": "Deny",
        "Action": "*",
        "Resource": "*",
        "Condition": {
            "NotIpAddress": {
                "aws:SourceIp": "%s/32"
            }
        }
    }]
}"""


def _mint_and_inject_credentials(env_id, egress_ip):
    """Read raw secrets, create IP-bound STS sessions, inject into agent task."""
    policy = IP_RESTRICTION_POLICY % egress_ip

    central_ak = _get_secret("agent-ephemeral/central-access-key")
    central_sk = _get_secret("agent-ephemeral/central-secret-key")
    central_role_arn = _get_secret("agent-ephemeral/central-assume-role-arn")

    central_session = boto3.Session(
        aws_access_key_id=central_ak, aws_secret_access_key=central_sk
    )
    central_sts = central_session.client("sts")
    central_creds = central_sts.assume_role(
        RoleArn=central_role_arn,
        RoleSessionName=f"agent-{env_id}",
        Policy=policy,
        DurationSeconds=43200,
    )["Credentials"]

    regional_ak = _get_secret("agent-ephemeral/regional-access-key")
    regional_sk = _get_secret("agent-ephemeral/regional-secret-key")

    regional_session = boto3.Session(
        aws_access_key_id=regional_ak, aws_secret_access_key=regional_sk
    )
    regional_sts = regional_session.client("sts")
    regional_creds = regional_sts.get_session_token(
        DurationSeconds=43200, Policy=policy
    )["Credentials"]

    mgmt_ak = _get_secret("agent-ephemeral/management-access-key")
    mgmt_sk = _get_secret("agent-ephemeral/management-secret-key")

    mgmt_session = boto3.Session(
        aws_access_key_id=mgmt_ak, aws_secret_access_key=mgmt_sk
    )
    mgmt_sts = mgmt_session.client("sts")
    mgmt_creds = mgmt_sts.get_session_token(
        DurationSeconds=43200, Policy=policy
    )["Credentials"]

    creds_file = _format_credentials_file(central_creds, regional_creds, mgmt_creds)
    encoded = _b64(creds_file)

    task_arn = _get_agent_task_arn(env_id)
    _exec_command(
        task_arn,
        "agent",
        f"/bin/bash -c 'mkdir -p /home/agent/.aws && echo {encoded} | base64 -d > /home/agent/.aws/credentials'",
    )


def _format_credentials_file(central, regional, management):
    def section(name, creds):
        return (
            f"[{name}]\n"
            f"aws_access_key_id = {creds['AccessKeyId']}\n"
            f"aws_secret_access_key = {creds['SecretAccessKey']}\n"
            f"aws_session_token = {creds['SessionToken']}\n"
        )

    return (
        section("central", central)
        + "\n"
        + section("regional", regional)
        + "\n"
        + section("management", management)
    )


def _get_proxy_endpoint(env_id):
    """Get the proxy task's private IP for HTTPS_PROXY."""
    tasks = ecs.list_tasks(
        cluster=ECS_CLUSTER,
        serviceName=f"agent-proxy-{env_id}",
        desiredStatus="RUNNING",
    )
    if not tasks.get("taskArns"):
        raise RuntimeError(f"No running proxy tasks for {env_id}")

    task_detail = ecs.describe_tasks(
        cluster=ECS_CLUSTER, tasks=[tasks["taskArns"][0]]
    )
    attachments = task_detail["tasks"][0].get("attachments", [])
    for att in attachments:
        if att["type"] == "ElasticNetworkInterface":
            for detail in att.get("details", []):
                if detail["name"] == "privateIPv4Address":
                    return f"http://{detail['value']}:3128"

    raise RuntimeError(f"Could not determine proxy IP for {env_id}")


def _configure_proxy_env(env_id, proxy_endpoint):
    """Set HTTPS_PROXY in the agent task's environment."""
    bashrc_addition = (
        f"export HTTPS_PROXY={proxy_endpoint}\n"
        f"export HTTP_PROXY={proxy_endpoint}\n"
        f"export https_proxy={proxy_endpoint}\n"
        f"export http_proxy={proxy_endpoint}\n"
        "export AWS_CA_BUNDLE=/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem\n"
    )
    encoded = _b64(bashrc_addition)
    task_arn = _get_agent_task_arn(env_id)
    _exec_command(
        task_arn,
        "agent",
        f"/bin/bash -c 'echo {encoded} | base64 -d >> /home/agent/.bashrc'",
    )


def _inject_env_vars(env_id):
    """Read env vars from SSM Parameter Store and inject into agent .bashrc."""
    try:
        param = ssm.get_parameter(Name="/agent-env/env-vars")
        lines = param["Parameter"]["Value"].strip().splitlines()
    except Exception:
        return

    exports = "\n".join(
        line.strip() if line.strip().startswith("export ") else f"export {line.strip()}"
        for line in lines if line.strip() and not line.startswith("#")
    )
    if not exports:
        return

    encoded = _b64(exports + "\n")
    task_arn = _get_agent_task_arn(env_id)
    _exec_command(
        task_arn,
        "agent",
        f"/bin/bash -c 'echo {encoded} | base64 -d >> /home/agent/.bashrc'",
    )


def _inject_proxy_ca(env_id):
    """Have the agent container fetch the CA cert from the proxy and install it."""
    proxy_endpoint = _get_proxy_endpoint(env_id)
    proxy_host = proxy_endpoint.replace("http://", "").replace(":3128", "")

    agent_task_arn = _get_agent_task_arn(env_id)
    _exec_command(
        agent_task_arn,
        "agent",
        f"/bin/bash -c 'curl -sf http://{proxy_host}:3128/ca.crt > /etc/pki/ca-trust/source/anchors/proxy-ca.crt && update-ca-trust'",
    )


def _get_agent_task_arn(env_id):
    tasks = ecs.list_tasks(
        cluster=ECS_CLUSTER,
        serviceName=f"agent-env-{env_id}",
        desiredStatus="RUNNING",
    )
    if not tasks.get("taskArns"):
        raise RuntimeError(f"No running agent tasks for {env_id}")
    return tasks["taskArns"][0]


def _b64(data):
    return base64.b64encode(data.encode()).decode()


def _exec_command(task_arn, container, command):
    ecs.execute_command(
        cluster=ECS_CLUSTER,
        task=task_arn,
        container=container,
        interactive=True,
        command=command,
    )


def _exec_command_output(task_arn, container, command):
    resp = ecs.execute_command(
        cluster=ECS_CLUSTER,
        task=task_arn,
        container=container,
        interactive=True,
        command=command,
    )
    return resp.get("output", "")


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }
