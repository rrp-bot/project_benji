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

    # Write to /sandbox (default) — files go to /sandbox/{real_credentials,config,cred-helper}
    # Copy to ~/.aws afterward:  cp /sandbox/{real_credentials,config,cred-helper} ~/.aws/

    # If files will be copied to ~/.aws, pass --install-dir so credential_process paths are correct:
    python3 agent-mint-credentials.py --ip 1.2.3.4 --config config/account_config.yaml \\
        --output-dir /sandbox --install-dir ~/.aws
"""

import argparse
import base64
import json
import sys
import time
from datetime import datetime, timezone

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

    task_detail = ecs_client.describe_tasks(
        cluster=cluster, tasks=[tasks["taskArns"][0]]
    )
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
        print(
            "ERROR: EcsClusterName not found in shared stack outputs", file=sys.stderr
        )
        sys.exit(1)

    vpc_id = outputs.get("VpcId")
    if not vpc_id:
        print("ERROR: VpcId not found in shared stack outputs", file=sys.stderr)
        sys.exit(1)

    egress_ip = _discover_proxy_public_ip(env_session, cluster, env_id)

    print(f"Discovered proxy egress IP: {egress_ip}", file=sys.stderr)
    print(f"Discovered VPC ID:          {vpc_id}", file=sys.stderr)

    return egress_ip, vpc_id, cluster


def _inject_credentials(
    env_session: boto3.Session,
    cluster: str,
    env_id: str,
    creds_file: str,
    profiles: list[str],
):
    """Push credentials, config, and cred-helper into the agent container via ECS Exec."""
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

    aws_dir = "/home/agent/.aws"
    helper_path = f"{aws_dir}/cred-helper"
    config_content = _generate_config(profiles, helper_path)

    encoded_creds = base64.b64encode(creds_file.encode()).decode()
    encoded_config = base64.b64encode(config_content.encode()).decode()
    encoded_helper = base64.b64encode(CRED_HELPER_SCRIPT.encode()).decode()

    cmd = (
        f"mkdir -p {aws_dir}"
        f" && echo {encoded_creds} | base64 -d > {aws_dir}/real_credentials"
        f" && echo {encoded_config} | base64 -d > {aws_dir}/config"
        f" && echo {encoded_helper} | base64 -d > {helper_path}"
        f" && chmod +x {helper_path}"
    )

    print(f"Injecting credentials into {env_id} ({task_arn})...", file=sys.stderr)
    ecs_client.execute_command(
        cluster=cluster,
        task=task_arn,
        container="agent",
        interactive=True,
        command=f"/bin/bash -c '{cmd}'",
    )
    print(
        "Credentials injected (real_credentials + config + cred-helper).",
        file=sys.stderr,
    )


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
) -> tuple[str, list[str], datetime]:
    with open(config_path) as f:
        config = yaml.safe_load(f)

    print(f"Binding to IP: {ip}", file=sys.stderr)
    print(f"Binding to VPC: {vpc_id}", file=sys.stderr)

    policy_json = _build_restriction_policy(ip, vpc_id)

    if mint_profile:
        print(
            f"Using AWS profile '{mint_profile}' for credential minting",
            file=sys.stderr,
        )
        mint_session = boto3.Session(profile_name=mint_profile)
    else:
        mint_session = boto3.Session()
    sts = mint_session.client("sts")
    sections = []
    profiles = []
    earliest_expiry = None

    for account in config["accounts"]:
        profile = account["profile"]
        profiles.append(profile)
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

    return header + "\n".join(sections), profiles, earliest_expiry


def _generate_config(profiles: list[str], cred_helper_path: str) -> str:
    sections = []
    for profile in profiles:
        sections.append(
            f"[profile {profile}]\n"
            f"credential_process = python3 {cred_helper_path} {profile}\n"
        )
    return "\n".join(sections)


CRED_HELPER_SCRIPT = '''\
#!/usr/bin/env python3
"""AWS credential_process helper — reads real_credentials and outputs SDK JSON."""
import configparser
import json
import os
import re
import sys

if len(sys.argv) != 2:
    print("Usage: cred-helper <profile>", file=sys.stderr)
    sys.exit(1)

profile = sys.argv[1]
cred_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "real_credentials")

with open(cred_file) as f:
    content = f.read()

expiry_match = re.search(r"^# Expires:\\s*(.+)$", content, re.MULTILINE)
expiration = expiry_match.group(1).strip() if expiry_match else None

cp = configparser.ConfigParser()
cp.read_string(content)

if profile not in cp:
    print(f"Profile '{profile}' not found in {cred_file}", file=sys.stderr)
    sys.exit(1)

output = {
    "Version": 1,
    "AccessKeyId": cp[profile]["aws_access_key_id"],
    "SecretAccessKey": cp[profile]["aws_secret_access_key"],
    "SessionToken": cp[profile]["aws_session_token"],
}
if expiration:
    output["Expiration"] = expiration

print(json.dumps(output))
'''


def main():
    parser = argparse.ArgumentParser(
        description="Mint IP-bound STS credentials for agent environments"
    )
    parser.add_argument(
        "--env-id",
        help="Environment ID — discover egress IP and VPC from live infrastructure",
    )
    parser.add_argument(
        "--mint-profile", help="AWS profile for STS assume-role (credential minting)"
    )
    parser.add_argument(
        "--inject",
        action="store_true",
        help="Inject credentials into the agent container via ECS Exec (requires --env-id)",
    )
    parser.add_argument(
        "--ip",
        help="Egress IP (overrides egress_ip in config, ignored if --env-id is set)",
    )
    parser.add_argument("--config", required=True, help="Path to YAML config file")
    parser.add_argument(
        "--duration",
        type=int,
        help="STS session duration in seconds (overrides config)",
    )
    parser.add_argument(
        "--output-dir", default="/sandbox", help="Output directory (default: /sandbox)"
    )
    parser.add_argument(
        "--install-dir",
        help="Directory where files will live after copying (used for credential_process path in config, default: same as --output-dir)",
    )
    parser.add_argument(
        "--cred-helper-path",
        help="Absolute path to cred-helper for credential_process in config (overrides --install-dir)",
    )
    parser.add_argument(
        "--keep-alive",
        action="store_true",
        help="Continuously re-mint credentials 5 minutes before expiry",
    )
    args = parser.parse_args()

    if args.inject and not args.env_id:
        parser.error("--inject requires --env-id")
    if args.keep_alive and not args.inject:
        parser.error("--keep-alive requires --inject")

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

    while True:
        creds_file, profiles, earliest_expiry = mint_credentials(
            args.config, ip, vpc_id, args.mint_profile, args.duration
        )

        if args.inject:
            _inject_credentials(env_session, cluster, args.env_id, creds_file, profiles)
        else:
            import os

            out_dir = args.output_dir
            os.makedirs(out_dir, exist_ok=True)

            # credential_process needs the path where cred-helper will actually live
            # (may differ from out_dir if files are copied to e.g. ~/.aws afterward)
            install_dir = args.install_dir or out_dir
            helper_path = args.cred_helper_path or os.path.join(
                os.path.abspath(install_dir), "cred-helper"
            )
            config_content = _generate_config(profiles, helper_path)

            creds_path = os.path.join(out_dir, "real_credentials")
            with open(creds_path, "w") as f:
                f.write(creds_file)
            os.chmod(creds_path, 0o600)

            config_path = os.path.join(out_dir, "config")
            with open(config_path, "w") as f:
                f.write(config_content)
            os.chmod(config_path, 0o600)

            helper_file_path = os.path.join(out_dir, "cred-helper")
            with open(helper_file_path, "w") as f:
                f.write(CRED_HELPER_SCRIPT)
            os.chmod(helper_file_path, 0o755)

            print(f"Written to: {out_dir}/", file=sys.stderr)
            print(f"  real_credentials  — STS credentials (INI)", file=sys.stderr)
            print(
                f"  config            — AWS config with credential_process",
                file=sys.stderr,
            )
            print(
                f"  cred-helper       — credential_process helper script",
                file=sys.stderr,
            )
            if install_dir != os.path.abspath(out_dir):
                print(
                    f"  (credential_process paths written for install location: {install_dir})",
                    file=sys.stderr,
                )

        if not args.keep_alive:
            break

        if hasattr(earliest_expiry, "timestamp"):
            expiry_ts = earliest_expiry.timestamp()
        else:
            expiry_ts = datetime.fromisoformat(
                str(earliest_expiry).replace("Z", "+00:00")
            ).timestamp()

        refresh_at = expiry_ts - 300
        now = datetime.now(timezone.utc).timestamp()
        sleep_secs = max(refresh_at - now, 30)

        print(
            f"Credentials expire at {earliest_expiry}. "
            f"Sleeping {int(sleep_secs)}s (refreshing 5 min before expiry)...",
            file=sys.stderr,
        )
        time.sleep(sleep_secs)


if __name__ == "__main__":
    main()
