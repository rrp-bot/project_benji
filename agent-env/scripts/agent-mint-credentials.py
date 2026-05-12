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
    python3 agent-mint-credentials.py --ip 1.2.3.4 --config config/account_config.yaml
    python3 agent-mint-credentials.py --ip 1.2.3.4 --config config/account_config.yaml --duration 3600
"""

import argparse
import json
import sys
import time

import boto3
import yaml


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


def mint_credentials(config_path: str, ip_override: str | None = None, duration_override: int | None = None) -> str:
    with open(config_path) as f:
        config = yaml.safe_load(f)

    ip = ip_override or config.get("egress_ip")
    if not ip:
        print("ERROR: No egress_ip in config and no --ip flag provided", file=sys.stderr)
        sys.exit(1)

    vpc_id = config.get("vpc_id")
    if not vpc_id:
        print("ERROR: No vpc_id in config", file=sys.stderr)
        sys.exit(1)

    print(f"Binding to IP: {ip}", file=sys.stderr)
    print(f"Binding to VPC: {vpc_id}", file=sys.stderr)

    policy_json = _build_restriction_policy(ip, vpc_id)

    sts = boto3.client("sts")
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
    parser.add_argument("--ip", help="Egress IP (overrides egress_ip in config)")
    parser.add_argument("--config", required=True, help="Path to YAML config file")
    parser.add_argument("--duration", type=int, help="STS session duration in seconds (overrides config)")
    args = parser.parse_args()

    creds_file = mint_credentials(args.config, args.ip, args.duration)

    import os

    output = "agent_credentials"
    with open(output, "w") as f:
        f.write(creds_file)
    os.chmod(output, 0o600)
    print(f"Written to: {output}", file=sys.stderr)


if __name__ == "__main__":
    main()
