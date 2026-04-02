#!/usr/bin/env python3
"""
drift_report.py — Structured change report for Salesforce permission set XML diffs.

Compares the previous committed version of a permission set against the newly
regenerated one and emits a human-readable summary plus a machine-readable JSON
report.  Designed to run between the generate and commit steps of the pipeline.

Usage:
    python3 drift_report.py <old_xml_file> <new_xml_file> \\
        [--output PATH] [--ado] [--fail-on-dangerous]

Options:
    --output PATH         Write JSON report to this path (default: drift-report.json).
    --ado                 Emit Azure DevOps ##vso[...] annotations.
    --fail-on-dangerous   Exit 2 if dangerous permissions (e.g. ModifyAllData) were
                          added in this run (useful as a gate in CI).

Exit codes:
    0  — Report generated successfully (drift may or may not have been detected).
    1  — Error (unreadable file, parse failure).
    2  — Dangerous permission added and --fail-on-dangerous was set.

JSON report schema:
    {
      "timestamp": "...",
      "drift_detected": true/false,
      "summary": { "fieldPermissions": {...}, "objectPermissions": {...}, ... },
      "details": {
        "fieldPermissions":        { "added": [...], "removed": [...] },
        "objectPermissions":       { "added": [...], "removed": [...] },
        "recordTypeVisibilities":  { "added": [...], "removed": [...] },
        "userPermissions":         { "added": [...], "removed": [...] }
      },
      "dangerous_permissions_added": [...]
    }
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

# ── Salesforce metadata namespace ─────────────────────────────────────────────

SF_NS  = "http://soap.sforce.com/2006/04/metadata"
SF_TAG = f"{{{SF_NS}}}"

# ── Section definitions: (xml_tag, key_child_tag) ────────────────────────────

SECTIONS = {
    "fieldPermissions":       "field",
    "objectPermissions":      "object",
    "recordTypeVisibilities": "recordType",
    "userPermissions":        "name",
}

# ── Permissions that warrant an extra warning if newly added ──────────────────

DANGEROUS_PERMISSIONS = {
    "ModifyAllData",
    "ViewAllData",
    "ModifyMetadata",
    "ManageUsers",
    "ResetPasswords",
    "ManagePasswordPolicies",
    "ManageProfilesPermissionsets",
}


# ── ADO annotation helpers ────────────────────────────────────────────────────

def ado_warning(message: str) -> None:
    print(f"##vso[task.logissue type=warning]{message}", flush=True)


def ado_error(message: str) -> None:
    print(f"##vso[task.logissue type=error]{message}", flush=True)


def ado_section(title: str) -> None:
    print(f"##[section]{title}", flush=True)


# ── XML parsing ───────────────────────────────────────────────────────────────

def extract_section_keys(root: ET.Element, xml_tag: str, key_child: str) -> set[str]:
    """Return the set of key values for a section in the permission set XML."""
    keys = set()
    full_tag     = f"{SF_TAG}{xml_tag}"
    full_key_tag = f"{SF_TAG}{key_child}"
    for elem in root.findall(full_tag):
        text = (elem.findtext(full_key_tag) or "").strip()
        if text:
            keys.add(text)
    return keys


def parse_permset_xml(path: Path) -> dict[str, set[str]]:
    """
    Parse a permission set XML and return a dict of section → set of keys.
    Returns an empty dict for each section if the file does not exist
    (treats a missing old file as an empty baseline — first-run scenario).
    """
    if not path.exists():
        return {section: set() for section in SECTIONS}

    try:
        tree = ET.parse(str(path))
    except ET.ParseError as exc:
        print(f"ERROR: Could not parse XML at '{path}': {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"ERROR: Could not read '{path}': {exc}", file=sys.stderr)
        sys.exit(1)

    root = tree.getroot()
    return {
        section: extract_section_keys(root, section, key_child)
        for section, key_child in SECTIONS.items()
    }


# ── Report generation ─────────────────────────────────────────────────────────

def build_report(
    old_sections: dict[str, set[str]],
    new_sections: dict[str, set[str]],
) -> dict:
    """Compare old and new section key-sets and build the full report dict."""
    details: dict[str, dict] = {}
    summary: dict[str, dict] = {}
    drift_detected = False
    dangerous_added: list[str] = []

    for section in SECTIONS:
        old_keys = old_sections.get(section, set())
        new_keys = new_sections.get(section, set())

        added   = sorted(new_keys - old_keys)
        removed = sorted(old_keys - new_keys)

        details[section] = {"added": added, "removed": removed}
        summary[section] = {
            "total":   len(new_keys),
            "added":   len(added),
            "removed": len(removed),
            "net":     len(new_keys) - len(old_keys),
        }

        if added or removed:
            drift_detected = True

        # Flag dangerous userPermissions additions.
        if section == "userPermissions":
            for perm in added:
                if perm in DANGEROUS_PERMISSIONS:
                    dangerous_added.append(perm)

    return {
        "timestamp":                  datetime.now(tz=timezone.utc).isoformat(),
        "drift_detected":             drift_detected,
        "summary":                    summary,
        "details":                    details,
        "dangerous_permissions_added": dangerous_added,
    }


# ── Console output ────────────────────────────────────────────────────────────

def print_report(report: dict, ado: bool) -> None:
    """Print a human-readable summary to stdout."""
    if ado:
        ado_section("Permission Set Drift Report")

    if not report["drift_detected"]:
        print("No drift detected — permission set is unchanged.", flush=True)
        return

    print("Drift detected — permission set has changed:\n", flush=True)
    print(f"  {'Section':<28}  {'Total':>7}  {'Added':>7}  {'Removed':>9}  {'Net':>5}")
    print(f"  {'-'*28}  {'-'*7}  {'-'*7}  {'-'*9}  {'-'*5}")

    for section, s in report["summary"].items():
        changed_marker = " ◄" if s["added"] or s["removed"] else ""
        print(
            f"  {section:<28}  {s['total']:>7}  {s['added']:>7}  {s['removed']:>9}  "
            f"{s['net']:>+5}{changed_marker}",
            flush=True,
        )

    # Detail lines for changed sections.
    details = report["details"]
    for section, diff in details.items():
        if diff["added"] or diff["removed"]:
            print(f"\n  ── {section} ──", flush=True)
            for item in diff["added"][:20]:
                print(f"    + {item}", flush=True)
            if len(diff["added"]) > 20:
                print(f"    ... and {len(diff['added']) - 20} more added", flush=True)
            for item in diff["removed"][:20]:
                print(f"    - {item}", flush=True)
            if len(diff["removed"]) > 20:
                print(f"    ... and {len(diff['removed']) - 20} more removed", flush=True)

    # ADO annotations for significant changes.
    if ado:
        summary = report["summary"]
        parts = []
        for section, s in summary.items():
            if s["added"] or s["removed"]:
                parts.append(
                    f"{section}: +{s['added']}/-{s['removed']}"
                )
        ado_warning(
            "Permission set drift detected: " + ", ".join(parts)
        )

    # Dangerous permission warnings.
    dangerous = report.get("dangerous_permissions_added", [])
    if dangerous:
        msg = (
            f"SECURITY NOTICE: Dangerous permission(s) added in this run: "
            f"{', '.join(dangerous)}. Review before deploying."
        )
        print(f"\n  ⚠  {msg}", flush=True)
        if ado:
            ado_warning(msg)


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a structured diff report between two permission set XML files."
    )
    parser.add_argument(
        "old_xml",
        type=Path,
        help="Path to the old (previously committed) permission set XML.",
    )
    parser.add_argument(
        "new_xml",
        type=Path,
        help="Path to the newly generated permission set XML.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("drift-report.json"),
        metavar="PATH",
        help="Write JSON report to this path (default: drift-report.json).",
    )
    parser.add_argument(
        "--ado",
        action="store_true",
        help="Emit Azure DevOps ##vso[...] pipeline annotations.",
    )
    parser.add_argument(
        "--fail-on-dangerous",
        action="store_true",
        dest="fail_on_dangerous",
        help="Exit 2 if a dangerous permission (e.g. ModifyAllData) was newly added.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.new_xml.exists():
        print(f"ERROR: New XML file not found: '{args.new_xml}'", file=sys.stderr)
        return 1

    print(f"Comparing permission set XML files:", flush=True)
    print(f"  Old: {args.old_xml}  ({'exists' if args.old_xml.exists() else 'not found — treating as empty baseline'})", flush=True)
    print(f"  New: {args.new_xml}", flush=True)
    print(flush=True)

    old_sections = parse_permset_xml(args.old_xml)
    new_sections = parse_permset_xml(args.new_xml)

    report = build_report(old_sections, new_sections)

    # Write JSON report.
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"Drift report written: {args.output}", flush=True)
    except OSError as exc:
        print(f"ERROR: Could not write report to '{args.output}': {exc}", file=sys.stderr)
        return 1

    print(flush=True)
    print_report(report, ado=args.ado)

    if args.fail_on_dangerous and report["dangerous_permissions_added"]:
        print(
            f"\nExiting 2: dangerous permission(s) were added and --fail-on-dangerous is set.",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
