#!/usr/bin/env python3
"""Mint IP-bound STS credentials for agent environments.

Assumes roles defined in a YAML config file using the caller's current AWS
identity, applying an IP restriction policy to each session. Outputs an AWS
credentials file with one profile per entry.

Config file format (YAML):

    accounts:
      - profile: central
        role_arn: arn:aws:iam::123456789012:role/AgentRole
        duration: 43200
      - profile: regional
        role_arn: arn:aws:iam::987654321098:role/AgentRole
        duration: 43200

Usage:
    python3 agent-mint-credentials.py --env-id abc123 --config config/account_config.yaml
    python3 agent-mint-credentials.py --ip 1.2.3.4 --config config/account_config.yaml
    python3 agent-mint-credentials.py --ip 1.2.3.4 --config config/account_config.yaml --duration 3600
"""

import argparse
import base64
import json
import sys
import time

import boto3
import yaml


SHARED_STACK_NAME = "agent-shared-infra"


def _get_shared_stack_outputs(session: boto3.Session) -> dict:
    cfn = session.client("cloudformation")
    resp = cfn.describe_stacks(StackName=SHARED_STACK_NAME)
    outputs = {}
    for o in resp["Stacks"][0].get("Outputs", []):
        outputs[o["OutputKey"]] = o["OutputValue"]
    return outputs


def _discover_proxy_public_ip(session: boto3.Session, cluster: str, env_id: str) -> str:
    """Discover the proxy Fargate task's public IP (egress IP for agent traffic)."""
    ecs_client = session.client("ecs")
    ec2 = session.client("ec2")

    tasks = ecs_client.list_tasks(
        cluster=cluster,
        serviceName=f"agent-proxy-{env_id}",
        desiredStatus="RUNNING",
    )
    if not tasks.get("taskArns"):
        print(f"ERROR: No running proxy tasks for {env_id}", file=sys.stderr)
        sys.exit(1)

    task_detail = ecs_client.describe_tasks(cluster=cluster, tasks=[tasks["taskArns"][0]])
    attachments = task_detail["tasks"][0].get("attachments", [])

    eni_id = None
    for att in attachments:
        if att["type"] == "ElasticNetworkInterface":
            for detail in att.get("details", []):
                if detail["name"] == "networkInterfaceId":
                    eni_id = detail["value"]
                    break

    if not eni_id:
        print("ERROR: Could not find proxy ENI", file=sys.stderr)
        sys.exit(1)

    enis = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])
    association = enis["NetworkInterfaces"][0].get("Association", {})
    public_ip = association.get("PublicIp")
    if not public_ip:
        print("ERROR: Proxy ENI has no public IP", file=sys.stderr)
        sys.exit(1)

    return public_ip


def _discover_from_env(env_session: boto3.Session, env_id: str) -> tuple[str, str, str]:
    """Discover proxy egress IP, VPC ID, and cluster name from live infrastructure."""
    outputs = _get_shared_stack_outputs(env_session)

    cluster = outputs.get("EcsClusterName")
    if not cluster:
        print("ERROR: EcsClusterName not found in shared stack outputs", file=sys.stderr)
        sys.exit(1)

    vpc_id = outputs.get("VpcId")
    if not vpc_id:
        print("ERROR: VpcId not found in shared stack outputs", file=sys.stderr)
        sys.exit(1)

    egress_ip = _discover_proxy_public_ip(env_session, cluster, env_id)

    print(f"Discovered proxy egress IP: {egress_ip}", file=sys.stderr)
    print(f"Discovered VPC ID:          {vpc_id}", file=sys.stderr)

    return egress_ip, vpc_id, cluster


def _inject_credentials(env_session: boto3.Session, cluster: str, env_id: str, creds_file: str):
    """Push credentials into the agent container via ECS Exec."""
    ecs_client = env_session.client("ecs")

    tasks = ecs_client.list_tasks(
        cluster=cluster,
        serviceName=f"agent-env-{env_id}",
        desiredStatus="RUNNING",
    )
    if not tasks.get("taskArns"):
        print(f"ERROR: No running agent tasks for {env_id}", file=sys.stderr)
        sys.exit(1)

    task_arn = tasks["taskArns"][0]
    encoded = base64.b64encode(creds_file.encode()).decode()

    print(f"Injecting credentials into {env_id} ({task_arn})...", file=sys.stderr)
    ecs_client.execute_command(
        cluster=cluster,
        task=task_arn,
        container="agent",
        interactive=True,
        command=f"/bin/bash -c 'mkdir -p /home/agent/.aws && echo {encoded} | base64 -d > /home/agent/.aws/credentials'",
    )
    print("Credentials injected.", file=sys.stderr)


def _build_restriction_policy(ip: str, vpc_id: str | None = None) -> str:
    statements = [
        {
            "Sid": "AllowAll",
            "Effect": "Allow",
            "Action": "*",
            "Resource": "*",
        },
        {
            "Sid": "DenyNonAgentPublic",
            "Effect": "Deny",
            "Action": "*",
            "Resource": "*",
            "Condition": {
                "NotIpAddress": {"aws:SourceIp": f"{ip}/32"},
                "Null": {"aws:SourceIp": "false"},
            },
        },
        {
            "Sid": "DenyNonAgentVpc",
            "Effect": "Deny",
            "Action": "*",
            "Resource": "*",
            "Condition": {
                "StringNotEquals": {"aws:SourceVpc": vpc_id},
                "Null": {"aws:SourceIp": "true"},
            },
        },
    ]
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


def mint_credentials(
    config_path: str,
    ip: str,
    vpc_id: str,
    mint_profile: str | None = None,
    duration_override: int | None = None,
) -> str:
    with open(config_path) as f:
        config = yaml.safe_load(f)

    print(f"Binding to IP: {ip}", file=sys.stderr)
    print(f"Binding to VPC: {vpc_id}", file=sys.stderr)

    policy_json = _build_restriction_policy(ip, vpc_id)

    if mint_profile:
        print(f"Using AWS profile '{mint_profile}' for credential minting", file=sys.stderr)
        mint_session = boto3.Session(profile_name=mint_profile)
    else:
        mint_session = boto3.Session()
    sts = mint_session.client("sts")
    sections = []
    earliest_expiry = None

    for account in config["accounts"]:
        profile = account["profile"]
        role_arn = account["role_arn"]
        duration = duration_override or account.get("duration", 43200)

        print(f"Minting {profile} ({role_arn}, {duration}s)...", file=sys.stderr)

        resp = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"agent-{profile}-{int(time.time())}",
            Policy=policy_json,
            DurationSeconds=duration,
        )
        creds = resp["Credentials"]
        expiry = creds["Expiration"]

        if earliest_expiry is None or expiry < earliest_expiry:
            earliest_expiry = expiry

        sections.append(
            f"[{profile}]\n"
            f"aws_access_key_id = {creds['AccessKeyId']}\n"
            f"aws_secret_access_key = {creds['SecretAccessKey']}\n"
            f"aws_session_token = {creds['SessionToken']}\n"
        )

        print(f"  expires: {expiry}", file=sys.stderr)

    header = f"# Minted: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
    header += f"# Expires: {earliest_expiry.strftime('%Y-%m-%dT%H:%M:%SZ') if hasattr(earliest_expiry, 'strftime') else earliest_expiry}\n"
    header += f"# Bound to IP: {ip}\n\n"

    return header + "\n".join(sections)


def main():
    parser = argparse.ArgumentParser(
        description="Mint IP-bound STS credentials for agent environments"
    )
    parser.add_argument("--env-id", help="Environment ID — discover egress IP and VPC from live infrastructure")
    parser.add_argument("--mint-profile", help="AWS profile for STS assume-role (credential minting)")
    parser.add_argument("--inject", action="store_true", help="Inject credentials into the agent container via ECS Exec (requires --env-id)")
    parser.add_argument("--ip", help="Egress IP (overrides egress_ip in config, ignored if --env-id is set)")
    parser.add_argument("--config", required=True, help="Path to YAML config file")
    parser.add_argument("--duration", type=int, help="STS session duration in seconds (overrides config)")
    args = parser.parse_args()

    if args.inject and not args.env_id:
        parser.error("--inject requires --env-id")

    env_session = boto3.Session()

    if args.env_id:
        ip, vpc_id, cluster = _discover_from_env(env_session, args.env_id)
    else:
        with open(args.config) as f:
            config = yaml.safe_load(f)
        ip = args.ip or config.get("egress_ip")
        vpc_id = config.get("vpc_id")
        cluster = None
        if not ip:
            parser.error("No egress_ip in config and no --ip/--env-id flag provided")
        if not vpc_id:
            parser.error("No vpc_id in config and no --env-id flag provided")

    creds_file = mint_credentials(args.config, ip, vpc_id, args.mint_profile, args.duration)

    if args.inject:
        _inject_credentials(env_session, cluster, args.env_id, creds_file)
    else:
        import os

        output = "agent_credentials"
        with open(output, "w") as f:
            f.write(creds_file)
        os.chmod(output, 0o600)
        print(f"Written to: {output}", file=sys.stderr)


if __name__ == "__main__":
    main()
