#!/usr/bin/env python3
"""
validate_permset.py — Pre-deploy validation for Salesforce permission set XML.

Runs a suite of structural and safety checks on the generated permission set
before it reaches the deploy step.  Optionally executes a Salesforce check-only
(validate-only) deployment to catch metadata resolution errors without committing
changes to the org.

Usage:
    python3 validate_permset.py <permset_xml_file> \\
        [--min-objects N] [--min-fields N] [--min-record-types N] \\
        [--check-only] [--target-org ALIAS] [--wait MINUTES] \\
        [--ado]

Options:
    --min-objects N        Fail if fewer than N objectPermissions (default: 10).
    --min-fields  N        Fail if fewer than N fieldPermissions (default: 50).
    --min-record-types N   Fail if fewer than N recordTypeVisibilities (default: 0).
    --check-only           Run a Salesforce validate-only (dry-run) deploy.
    --target-org ALIAS     Org alias used for check-only deploy.
    --wait MINUTES         Minutes to wait for check-only deploy (default: 10).
    --ado                  Emit Azure DevOps ##vso[...] pipeline annotations.

Exit codes:
    0  — All checks passed.
    1  — One or more checks failed.
"""

import argparse
import json
import locale
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

# ── Salesforce metadata namespace ─────────────────────────────────────────────

SF_NS  = "http://soap.sforce.com/2006/04/metadata"
SF_TAG = f"{{{SF_NS}}}"

# ── Dangerous user permissions — flagged but not blocking unless you choose ───

DANGEROUS_USER_PERMISSIONS = {
    "ModifyAllData",
    "ViewAllData",
    "ModifyMetadata",
    "ManageUsers",
    "ResetPasswords",
    "ManagePasswordPolicies",
    "ManageProfilesPermissionsets",
    "ManageRoles",
}

# ── SF CLI timeouts ───────────────────────────────────────────────────────────

SF_COMMAND_TIMEOUT_BASE = 120
DEPLOY_TIMEOUT_BUFFER   = 300


# ── ADO annotation helpers ────────────────────────────────────────────────────

def ado_warning(message: str) -> None:
    print(f"##vso[task.logissue type=warning]{message}", flush=True)


def ado_error(message: str) -> None:
    print(f"##vso[task.logissue type=error]{message}", flush=True)


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a Salesforce permission set XML before deployment."
    )
    parser.add_argument(
        "permset_xml",
        type=Path,
        help="Path to the permission set XML file to validate.",
    )
    parser.add_argument(
        "--min-objects",
        type=int,
        default=10,
        metavar="N",
        help="Minimum acceptable objectPermissions count (default: 10).",
    )
    parser.add_argument(
        "--min-fields",
        type=int,
        default=50,
        metavar="N",
        help="Minimum acceptable fieldPermissions count (default: 50).",
    )
    parser.add_argument(
        "--min-record-types",
        type=int,
        default=0,
        metavar="N",
        help="Minimum acceptable recordTypeVisibilities count (default: 0).",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Execute a Salesforce validate-only (no-commit) deployment.",
    )
    parser.add_argument(
        "--target-org",
        default="",
        metavar="ALIAS",
        help="Org alias for check-only deployment.",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=10,
        metavar="MINUTES",
        help="Minutes to wait for check-only deploy completion (default: 10).",
    )
    parser.add_argument(
        "--ado",
        action="store_true",
        help="Emit Azure DevOps ##vso[...] pipeline annotations.",
    )
    return parser.parse_args()


# ── XML validation ────────────────────────────────────────────────────────────

def validate_xml(
    path: Path,
    min_objects: int,
    min_fields: int,
    min_record_types: int,
    ado: bool,
) -> bool:
    """
    Parse the XML and run all structural checks.
    Returns True if all checks pass, False otherwise.
    """
    errors:   list[str] = []
    warnings: list[str] = []

    # ── 1. File existence ─────────────────────────────────────────────────────
    if not path.exists():
        msg = f"Permission set file not found: '{path}'"
        errors.append(msg)
        if ado:
            ado_error(msg)
        _report(errors, warnings)
        return False

    # ── 2. XML well-formedness ────────────────────────────────────────────────
    try:
        tree = ET.parse(str(path))
    except ET.ParseError as exc:
        msg = f"XML is not well-formed: {exc}"
        errors.append(msg)
        if ado:
            ado_error(msg)
        _report(errors, warnings)
        return False
    except OSError as exc:
        msg = f"Could not read file: {exc}"
        errors.append(msg)
        if ado:
            ado_error(msg)
        _report(errors, warnings)
        return False

    root = tree.getroot()

    # ── 3. Namespace check ────────────────────────────────────────────────────
    root_ns = ""
    if root.tag.startswith("{"):
        root_ns = root.tag.split("}")[0][1:]

    if root_ns != SF_NS:
        msg = (
            f"Unexpected XML namespace: '{root_ns}'. "
            f"Expected: '{SF_NS}'."
        )
        errors.append(msg)
        if ado:
            ado_error(msg)

    # ── 4. Root element name ──────────────────────────────────────────────────
    local_root = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    if local_root != "PermissionSet":
        msg = f"Root element is '{local_root}', expected 'PermissionSet'."
        errors.append(msg)
        if ado:
            ado_error(msg)

    # ── 5. Required metadata fields ───────────────────────────────────────────
    label = root.findtext(f"{SF_TAG}label") or ""
    if not label.strip():
        msg = "Missing or empty <label> element."
        errors.append(msg)
        if ado:
            ado_error(msg)

    # ── 6. Cardinality checks ─────────────────────────────────────────────────
    n_fields       = len(root.findall(f"{SF_TAG}fieldPermissions"))
    n_objects      = len(root.findall(f"{SF_TAG}objectPermissions"))
    n_record_types = len(root.findall(f"{SF_TAG}recordTypeVisibilities"))
    n_user_perms   = len(root.findall(f"{SF_TAG}userPermissions"))

    print(f"  fieldPermissions:       {n_fields}", flush=True)
    print(f"  objectPermissions:      {n_objects}", flush=True)
    print(f"  recordTypeVisibilities: {n_record_types}", flush=True)
    print(f"  userPermissions:        {n_user_perms}", flush=True)

    if n_objects < min_objects:
        msg = (
            f"objectPermissions count ({n_objects}) is below minimum threshold ({min_objects}). "
            "The permission set may have been incorrectly truncated."
        )
        errors.append(msg)
        if ado:
            ado_error(msg)

    if n_fields < min_fields:
        msg = (
            f"fieldPermissions count ({n_fields}) is below minimum threshold ({min_fields}). "
            "The permission set may have been incorrectly truncated."
        )
        errors.append(msg)
        if ado:
            ado_error(msg)

    if n_record_types < min_record_types:
        msg = (
            f"recordTypeVisibilities count ({n_record_types}) is below minimum "
            f"threshold ({min_record_types})."
        )
        errors.append(msg)
        if ado:
            ado_error(msg)

    # ── 7. Dangerous permission audit ─────────────────────────────────────────
    user_perm_names = {
        (elem.findtext(f"{SF_TAG}name") or "").strip()
        for elem in root.findall(f"{SF_TAG}userPermissions")
    }
    present_dangerous = sorted(user_perm_names & DANGEROUS_USER_PERMISSIONS)

    if present_dangerous:
        msg = (
            f"Elevated permissions present: {', '.join(present_dangerous)}. "
            "Confirm these are intentional."
        )
        warnings.append(msg)
        if ado:
            ado_warning(msg)

    # ── 8. Duplicate key detection ────────────────────────────────────────────
    sections = {
        "fieldPermissions":       "field",
        "objectPermissions":      "object",
        "recordTypeVisibilities": "recordType",
        "userPermissions":        "name",
    }
    for section, key_child in sections.items():
        seen:  set[str] = set()
        dupes: set[str] = set()
        for elem in root.findall(f"{SF_TAG}{section}"):
            key = (elem.findtext(f"{SF_TAG}{key_child}") or "").strip()
            if key in seen:
                dupes.add(key)
            seen.add(key)
        if dupes:
            msg = f"Duplicate entries in <{section}>: {', '.join(sorted(dupes))}"
            errors.append(msg)
            if ado:
                ado_error(msg)

    _report(errors, warnings)
    return len(errors) == 0


def _report(errors: list[str], warnings: list[str]) -> None:
    for w in warnings:
        print(f"  WARNING: {w}", flush=True)
    for e in errors:
        print(f"  ERROR:   {e}", flush=True)


# ── Check-only deploy ─────────────────────────────────────────────────────────

def _decode(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    enc = (locale.getpreferredencoding(False) or "utf-8").lower()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(enc, errors="replace")


def resolve_sf_path() -> str | None:
    for candidate in ("sf", "sf.cmd", "sf.exe"):
        path = shutil.which(candidate)
        if path:
            try:
                probe = subprocess.run(
                    [path, "--version"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    check=False,
                )
                if probe.returncode == 0:
                    return path
            except (subprocess.TimeoutExpired, OSError):
                continue
    return None


def run_check_only_deploy(
    permset_path: Path,
    target_org: str,
    wait_minutes: int,
    ado: bool,
) -> bool:
    """
    Run `sf project deploy validate` (check-only, no metadata commit).
    Returns True on success.
    """
    sf = resolve_sf_path()
    if sf is None:
        msg = "Salesforce CLI (sf) not found on PATH. Cannot run check-only deploy."
        print(f"ERROR: {msg}", file=sys.stderr)
        if ado:
            ado_error(msg)
        return False

    cmd = [
        sf, "project", "deploy", "validate",
        "--source-dir", str(permset_path),
        "--wait", str(wait_minutes),
        "--json",
    ]
    if target_org:
        cmd.extend(["--target-org", target_org])

    timeout_s = max(SF_COMMAND_TIMEOUT_BASE, (wait_minutes * 60) + DEPLOY_TIMEOUT_BUFFER)

    print(
        f"  Running: {' '.join(shlex.quote(p) for p in cmd)}",
        flush=True,
    )

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        msg = f"Check-only deploy timed out after {timeout_s}s."
        print(f"ERROR: {msg}", file=sys.stderr)
        if ado:
            ado_error(msg)
        return False
    except OSError as exc:
        msg = f"Failed to invoke sf CLI for check-only deploy: {exc}"
        print(f"ERROR: {msg}", file=sys.stderr)
        if ado:
            ado_error(msg)
        return False

    stdout = _decode(proc.stdout).strip()
    stderr = _decode(proc.stderr).strip()

    if stdout:
        print(stdout, flush=True)
    if stderr:
        print(stderr, file=sys.stderr)

    # Parse JSON result.
    try:
        result = json.loads(stdout)
        status = result.get("status", proc.returncode)
    except (json.JSONDecodeError, AttributeError):
        status = proc.returncode

    if status == 0:
        print("  Check-only deploy: PASSED.", flush=True)
        return True

    msg = "Check-only deploy FAILED — see output above for component errors."
    print(f"ERROR: {msg}", file=sys.stderr)
    if ado:
        ado_error(msg)
    return False


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    args = parse_args()

    print(f"Validating permission set: {args.permset_xml}", flush=True)
    print(flush=True)

    xml_ok = validate_xml(
        args.permset_xml,
        min_objects=args.min_objects,
        min_fields=args.min_fields,
        min_record_types=args.min_record_types,
        ado=args.ado,
    )

    if not xml_ok:
        print("\nValidation FAILED — see errors above.", flush=True)
        return 1

    print("\nStructural validation PASSED.", flush=True)

    if args.check_only:
        print("\nRunning Salesforce check-only (validate) deploy...", flush=True)
        deploy_ok = run_check_only_deploy(
            args.permset_xml,
            target_org=args.target_org,
            wait_minutes=args.wait,
            ado=args.ado,
        )
        if not deploy_ok:
            print("\nCheck-only deploy FAILED.", flush=True)
            return 1
        print("\nCheck-only deploy PASSED.", flush=True)

    print("\nAll validation checks PASSED.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
