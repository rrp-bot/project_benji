#!/usr/bin/env python3
"""Write a sourceable shell file from a profile in agent_credentials.

Usage:
    python3 agent-use-profile.py central
    source agent_profile.env
"""

import configparser
import sys


def main():
    if len(sys.argv) < 2:
        print("Usage: eval $(python3 agent-use-profile.py <profile> [credentials-file])", file=sys.stderr)
        sys.exit(1)

    profile = sys.argv[1]
    creds_file = sys.argv[2] if len(sys.argv) > 2 else "agent_credentials"

    config = configparser.ConfigParser()
    config.read(creds_file)

    if profile not in config:
        print(f"ERROR: Profile '{profile}' not found in {creds_file}", file=sys.stderr)
        sys.exit(1)

    section = config[profile]
    output = "agent_profile.env"
    with open(output, "w") as f:
        f.write(f"export AWS_ACCESS_KEY_ID={section['aws_access_key_id']}\n")
        f.write(f"export AWS_SECRET_ACCESS_KEY={section['aws_secret_access_key']}\n")
        f.write(f"export AWS_SESSION_TOKEN={section['aws_session_token']}\n")
        f.write("unset AWS_PROFILE AWS_SHARED_CREDENTIALS_FILE\n")

    print(f"Written to: {output} — run: source {output}", file=sys.stderr)


if __name__ == "__main__":
    main()
