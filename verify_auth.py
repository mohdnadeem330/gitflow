#!/usr/bin/env python3
"""
verify_auth.py — Verifies a Salesforce org connection from sf org display --json output.

Usage:
    sf org display --target-org <alias> --json | python3 verify_auth.py

Exit codes:
    0  — Connected successfully
    1  — Auth failed (expired token, revoked session, etc.)
"""
import json
import sys


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Could not parse org display output: {exc}", file=sys.stderr)
        return 1

    if data.get("status", 1) != 0:
        msg = data.get("message", "unknown error")
        print(f"Auth verification failed: {msg}", file=sys.stderr)
        print("", file=sys.stderr)
        print("The SFDX Auth URL may have expired. Common causes:", file=sys.stderr)
        print("  - Integration user password was changed", file=sys.stderr)
        print("  - Admin revoked all active sessions for the user", file=sys.stderr)
        print("  - Connected App refresh token policy expired the token", file=sys.stderr)
        print("  - Org-wide Refresh Token Policy expired the token", file=sys.stderr)
        print("", file=sys.stderr)
        print("To regenerate the auth URL:", file=sys.stderr)
        print("  1. sf org login web --alias prod-backup", file=sys.stderr)
        print("  2. sf org display --verbose --target-org prod-backup --json", file=sys.stderr)
        print("  3. Copy the sfdxAuthUrl value to the SFDX_AUTH_URL pipeline secret", file=sys.stderr)
        return 1

    result = data.get("result", {})
    print(f"Connected: {result.get('username', '?')} @ {result.get('instanceUrl', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())