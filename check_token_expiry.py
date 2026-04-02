#!/usr/bin/env python3
"""
check_token_expiry.py — Validate Salesforce org token health and warn on imminent expiry.

Reads `sf org display --json` output from stdin and:
  - Verifies the org is reachable and the token is active.
  - Emits Azure DevOps pipeline warning annotations if expiry is within --warn-days.
  - Exits 1 (hard failure) if the token is already expired or the org is unreachable.
  - Exits 0 in all other cases (healthy, or expiring-soon warning is non-blocking).

Usage (in pipeline):
    sf org display --target-org prod-backup --json 2>/dev/null | python3 check_token_expiry.py

Options:
    --warn-days N     Warn if expiry is within N days (default: 7).
    --org-alias ALIAS Label used in log/annotation messages.

Exit codes:
    0  — Token is healthy (or expiry check produced a warning but is non-fatal).
    1  — Token is expired, invalid, or org display JSON is unreadable.
"""

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta

# ── ADO annotation helpers ────────────────────────────────────────────────────

def ado_warning(message: str) -> None:
    print(f"##vso[task.logissue type=warning]{message}", flush=True)


def ado_error(message: str) -> None:
    print(f"##vso[task.logissue type=error]{message}", flush=True)


def ado_set_variable(name: str, value: str, is_secret: bool = False) -> None:
    secret_flag = ";issecret=true" if is_secret else ""
    print(f"##vso[task.setvariable variable={name}{secret_flag}]{value}", flush=True)


# ── Connected-status categories ───────────────────────────────────────────────

# Salesforce CLI connectedStatus values that indicate a live, usable token.
_HEALTHY_STATUSES = {
    "Connected",
    "Active",
}

# Statuses that definitively mean the token is dead.
_EXPIRED_STATUSES = {
    "RefreshTokenAuthError",
    "Expired",
    "InvalidSessionId",
    "INVALID_SESSION_ID",
}


# ── Core logic ────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Salesforce org token health from sf org display --json output."
    )
    parser.add_argument(
        "--warn-days",
        type=int,
        default=7,
        metavar="N",
        help="Emit a pipeline warning if the token expires within N days (default: 7).",
    )
    parser.add_argument(
        "--org-alias",
        default="",
        metavar="ALIAS",
        help="Org alias label used in log messages.",
    )
    return parser.parse_args()


def read_org_display_json() -> dict:
    """Read and parse sf org display --json from stdin."""
    raw = sys.stdin.read()
    if not raw.strip():
        print("ERROR: No input received on stdin. Pipe `sf org display --json` output here.",
              file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Could not parse sf org display JSON: {exc}", file=sys.stderr)
        print(f"  Raw input (first 400 chars): {raw[:400]}", file=sys.stderr)
        sys.exit(1)


def check_expiry(expiration_date_str: str, warn_days: int, org_label: str) -> None:
    """
    Parse the expirationDate string (YYYY-MM-DD) and emit warnings/errors.
    Connected App sessions include this; refresh-token orgs typically do not.
    """
    try:
        expiry_date = datetime.strptime(expiration_date_str, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        # Non-standard date format — log and move on rather than crashing.
        ado_warning(
            f"[{org_label}] Could not parse expirationDate '{expiration_date_str}'. "
            "Manual token health check recommended."
        )
        return

    now = datetime.now(tz=timezone.utc)
    days_remaining = (expiry_date - now).days

    if days_remaining < 0:
        ado_error(
            f"[{org_label}] SFDX Auth token EXPIRED on {expiration_date_str}. "
            "Rotate the SFDX_AUTH_URL secret immediately."
        )
        print(
            f"ERROR: [{org_label}] Token expired on {expiration_date_str} "
            f"({abs(days_remaining)} day(s) ago).",
            file=sys.stderr,
        )
        sys.exit(1)

    elif days_remaining <= warn_days:
        ado_warning(
            f"[{org_label}] SFDX Auth token expires in {days_remaining} day(s) "
            f"(on {expiration_date_str}). Rotate the SFDX_AUTH_URL secret before then."
        )
        print(
            f"WARNING: [{org_label}] Token expires in {days_remaining} day(s) "
            f"on {expiration_date_str}.",
            flush=True,
        )
        # Non-blocking — emit warning but do not exit 1.

    else:
        print(
            f"[{org_label}] Token valid. Expires {expiration_date_str} "
            f"({days_remaining} day(s) remaining).",
            flush=True,
        )


def main() -> int:
    args = parse_args()
    org_label = args.org_alias or "org"

    data = read_org_display_json()

    # Top-level status from sf CLI (non-zero means CLI itself failed).
    cli_status = data.get("status", 0)
    if cli_status != 0:
        message = data.get("message") or "Unknown Salesforce CLI error."
        ado_error(f"[{org_label}] sf org display returned status {cli_status}: {message}")
        print(f"ERROR: [{org_label}] sf org display failed: {message}", file=sys.stderr)
        return 1

    result = data.get("result") or {}

    # ── Connected status ──────────────────────────────────────────────────────
    connected_status = (result.get("connectedStatus") or "").strip()

    if connected_status in _EXPIRED_STATUSES:
        ado_error(
            f"[{org_label}] Org token is INVALID (connectedStatus: '{connected_status}'). "
            "Rotate the SFDX_AUTH_URL secret and re-run the pipeline."
        )
        print(
            f"ERROR: [{org_label}] Token invalid — connectedStatus: '{connected_status}'.",
            file=sys.stderr,
        )
        return 1

    if connected_status and connected_status not in _HEALTHY_STATUSES:
        # Unknown status — warn but don't block, since Salesforce adds new statuses.
        ado_warning(
            f"[{org_label}] Unexpected connectedStatus: '{connected_status}'. "
            "Verify token health manually."
        )
        print(
            f"WARNING: [{org_label}] Unrecognised connectedStatus: '{connected_status}'.",
            flush=True,
        )
    else:
        print(f"[{org_label}] connectedStatus: {connected_status or '(not provided)'}", flush=True)

    # ── Username / instance info ──────────────────────────────────────────────
    username     = result.get("username", "")
    instance_url = result.get("instanceUrl", "")
    alias        = result.get("alias", "")
    org_id       = result.get("orgId", "")

    print(f"[{org_label}] Username:    {username or '(unknown)'}", flush=True)
    print(f"[{org_label}] Alias:       {alias or '(none)'}", flush=True)
    print(f"[{org_label}] Instance:    {instance_url or '(unknown)'}", flush=True)
    print(f"[{org_label}] OrgId:       {org_id or '(unknown)'}", flush=True)

    # Expose non-secret org metadata as pipeline variables for downstream steps.
    if instance_url:
        ado_set_variable("SF_INSTANCE_URL", instance_url)
    if org_id:
        ado_set_variable("SF_ORG_ID", org_id)

    # ── Expiry date (present for Connected App session-based auth) ────────────
    expiration_date = (result.get("expirationDate") or "").strip()
    if expiration_date:
        check_expiry(expiration_date, args.warn_days, org_label)
    else:
        print(
            f"[{org_label}] No expirationDate in org display output "
            "(refresh-token auth — no calendar expiry to check).",
            flush=True,
        )

    print(f"\n[{org_label}] Token health check passed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
