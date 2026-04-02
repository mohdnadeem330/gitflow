#!/usr/bin/env python3
"""
heal_permset.py — Self-healing utility for Salesforce permission set deployments.

Usage:
    python3 heal_permset.py <deploy_json_file> <permset_xml_file>

Exit codes:
    0  — XML was patched (permissions injected and/or stale entries removed)
    2  — No healable errors found — manual intervention required
    1  — Unexpected error (bad args, unreadable files, parse failure)

Heals two categories of deployment error:
─────────────────────────────────────────────────────────────────────────────
INJECT — Missing permission dependencies
    Error:  "Permission CustomizeApplication depends on permission(s): ManageTerritories"
    Fix:    Add <userPermissions> block for the missing dependency.

REMOVE — Stale references to metadata that no longer exists in the org
    Error:  "In field: recordType - no RecordType named Account.X found"
    Error:  "In field: field - no CustomField named Obj__c.Field__c found"
    Error:  "In field: object - no CustomObject named Obj__c found"
    Error:  "In field: name - no UserPermission named X found"
    Fix:    Remove the corresponding XML block from the permission set.
─────────────────────────────────────────────────────────────────────────────
"""

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

# ── Salesforce metadata namespace ────────────────────────────────────────────

SF_NS  = "http://soap.sforce.com/2006/04/metadata"
SF_TAG = f"{{{SF_NS}}}"


# ── Error patterns → XML mapping ─────────────────────────────────────────────

@dataclass
class RemovalRule:
    pattern:   re.Pattern
    xml_tag:   str   # parent element to remove, e.g. "recordTypeVisibilities"
    key_child: str   # child whose text == the offending API name
    label:     str   # for log output


REMOVAL_RULES: list[RemovalRule] = [
    RemovalRule(
        pattern   = re.compile(r"no RecordType named ([^\s]+) found", re.IGNORECASE),
        xml_tag   = "recordTypeVisibilities",
        key_child = "recordType",
        label     = "RecordType visibility",
    ),
    RemovalRule(
        pattern   = re.compile(r"no CustomField named ([^\s]+) found", re.IGNORECASE),
        xml_tag   = "fieldPermissions",
        key_child = "field",
        label     = "Field permission",
    ),
    RemovalRule(
        pattern   = re.compile(r"no CustomObject named ([^\s]+) found", re.IGNORECASE),
        xml_tag   = "objectPermissions",
        key_child = "object",
        label     = "Object permission",
    ),
    RemovalRule(
        pattern   = re.compile(r"no UserPermission named ([^\s]+) found", re.IGNORECASE),
        xml_tag   = "userPermissions",
        key_child = "name",
        label     = "User permission",
    ),
    RemovalRule(
        pattern   = re.compile(r"no ApexPage named ([^\s]+) found", re.IGNORECASE),
        xml_tag   = "pageAccesses",
        key_child = "apexPage",
        label     = "Apex page access",
    ),
    RemovalRule(
        pattern   = re.compile(r"no ApexClass named ([^\s]+) found", re.IGNORECASE),
        xml_tag   = "classAccesses",
        key_child = "apexClass",
        label     = "Apex class access",
    ),
    RemovalRule(
        pattern   = re.compile(r"no CustomTab named ([^\s]+) found", re.IGNORECASE),
        xml_tag   = "tabSettings",
        key_child = "tab",
        label     = "Tab setting",
    ),
    RemovalRule(
        pattern   = re.compile(r"no CustomApplication named ([^\s]+) found", re.IGNORECASE),
        xml_tag   = "applicationVisibilities",
        key_child = "application",
        label     = "Application visibility",
    ),
    # ── Extended rules (added in v2) ──────────────────────────────────────────
    RemovalRule(
        pattern   = re.compile(r"no Flow named ([^\s]+) found", re.IGNORECASE),
        xml_tag   = "flowAccesses",
        key_child = "flow",
        label     = "Flow access",
    ),
    RemovalRule(
        pattern   = re.compile(r"no CustomPermission named ([^\s]+) found", re.IGNORECASE),
        xml_tag   = "customPermissions",
        key_child = "name",
        label     = "Custom permission",
    ),
    RemovalRule(
        pattern   = re.compile(
            r"no CustomMetadataType(?:Record)? named ([^\s]+) found", re.IGNORECASE
        ),
        xml_tag   = "customMetadataTypeAccesses",
        key_child = "name",
        label     = "Custom metadata type access",
    ),
    RemovalRule(
        pattern   = re.compile(r"no ConnectedApplication named ([^\s]+) found", re.IGNORECASE),
        xml_tag   = "connectedAppAccesses",
        key_child = "connectedApp",
        label     = "Connected app access",
    ),
    RemovalRule(
        pattern   = re.compile(r"no CustomSetting named ([^\s]+) found", re.IGNORECASE),
        xml_tag   = "customSettingAccesses",
        key_child = "name",
        label     = "Custom setting access",
    ),
    RemovalRule(
        pattern   = re.compile(r"no ExternalDataSource named ([^\s]+) found", re.IGNORECASE),
        xml_tag   = "externalDataSourceAccesses",
        key_child = "externalDataSource",
        label     = "External data source access",
    ),
]

# Missing dependency pattern (injection)
DEPENDENCY_PATTERN = re.compile(
    r"depends on permission\(s\):\s*(.+)", re.IGNORECASE
)

# ── Safety thresholds — prevent catastrophic heal-loops ──────────────────────
# If removals would reduce any section below these counts the heal is aborted.
# Set to 0 to disable a threshold.

SAFETY_THRESHOLDS: dict[str, int] = {
    "fieldPermissions":       20,
    "objectPermissions":      5,
    "recordTypeVisibilities": 0,   # no lower bound — record types can legitimately be 0
    "userPermissions":        0,
}

# Path written by heal_permset when exit=2 (unrecoverable failures found)
UNHEALED_ERRORS_DEFAULT = "unhealed-errors.json"


# ── Result container ─────────────────────────────────────────────────────────

@dataclass
class HealPlan:
    to_inject: set[str]                         = field(default_factory=set)
    # to_remove: { xml_tag -> [(key_child, api_name), ...] }
    to_remove: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    @property
    def has_work(self) -> bool:
        return bool(self.to_inject) or bool(self.to_remove)


# ── Parse component failures ─────────────────────────────────────────────────

def extract_failures(deploy_json: dict) -> list[dict]:
    """Return componentFailures as a list (normalises single-dict case)."""
    details  = (deploy_json.get("result") or {}).get("details") or {}
    failures = details.get("componentFailures") or []
    if isinstance(failures, dict):
        failures = [failures]
    return failures


def build_heal_plan(failures: list[dict]) -> tuple["HealPlan", list[dict]]:
    """
    Scan every failure and decide what to inject and what to remove.
    Returns (plan, unmatched_failures) where unmatched_failures are those
    that no rule could handle — these require manual intervention.
    """
    plan      = HealPlan()
    unmatched: list[dict] = []

    for failure in failures:
        problem   = failure.get("problem") or ""
        component = failure.get("fullName") or failure.get("componentName") or ""

        # INJECT: missing permission dependency
        dep_match = DEPENDENCY_PATTERN.search(problem)
        if dep_match:
            for perm in re.split(r"[,\s]+", dep_match.group(1).strip()):
                perm = perm.strip()
                if perm:
                    plan.to_inject.add(perm)
                    print(f"  [inject] Dependency missing: '{perm}'")
            continue  # dependency errors don't also fire removal rules

        # REMOVE: stale reference to non-existent metadata
        matched = False
        for rule in REMOVAL_RULES:
            match = rule.pattern.search(problem)
            if match:
                api_name = match.group(1).strip()
                bucket   = plan.to_remove.setdefault(rule.xml_tag, [])
                entry    = (rule.key_child, api_name)
                if entry not in bucket:
                    bucket.append(entry)
                    print(f"  [remove] Stale {rule.label}: '{api_name}' not found in org")
                matched = True
                break

        if not matched:
            unmatched.append({
                "component": component,
                "problem":   problem,
            })
            print(f"  [unmatched] No rule for: {problem[:120]!r}")

    return plan, unmatched


# ── XML patching ─────────────────────────────────────────────────────────────

def inject_permissions(root: ET.Element, permissions: set[str]) -> list[str]:
    """Add <userPermissions> for any name not already in the XML."""
    existing = {
        el.findtext(f"{SF_TAG}name") or ""
        for el in root.findall(f"{SF_TAG}userPermissions")
    }
    added = []
    for perm in sorted(permissions):
        if perm in existing:
            print(f"  [inject] '{perm}' already present — skipping")
            continue
        block = ET.SubElement(root, f"{SF_TAG}userPermissions")
        ET.SubElement(block, f"{SF_TAG}enabled").text = "true"
        ET.SubElement(block, f"{SF_TAG}name").text    = perm
        added.append(perm)
        print(f"  [inject] Added userPermission: '{perm}'")
    return added


def check_safety_thresholds(
    root: ET.Element,
    to_remove: dict[str, list[tuple[str, str]]],
) -> list[str]:
    """
    Before applying removals, verify no section would fall below its minimum
    safe count.  Returns a list of violation messages (empty = safe to proceed).
    """
    SF_TAG_LOCAL = SF_TAG  # bring into local scope for clarity
    violations: list[str] = []

    for xml_tag, entries in to_remove.items():
        minimum = SAFETY_THRESHOLDS.get(xml_tag, 0)
        if minimum == 0:
            continue

        full_tag    = f"{SF_TAG_LOCAL}{xml_tag}"
        current     = len(root.findall(full_tag))
        after_count = current - len(entries)

        if after_count < minimum:
            violations.append(
                f"Removing {len(entries)} <{xml_tag}> entries would leave {after_count} "
                f"(minimum safe threshold: {minimum}). Aborting heal to prevent data loss."
            )

    return violations


def remove_stale_entries(
    root: ET.Element,
    to_remove: dict[str, list[tuple[str, str]]],
) -> list[str]:
    """Remove every XML block that references a non-existent metadata item."""
    removed = []
    for xml_tag, entries in to_remove.items():
        full_tag = f"{SF_TAG}{xml_tag}"
        for key_child, api_name in entries:
            key_tag = f"{SF_TAG}{key_child}"
            for elem in root.findall(full_tag):
                if (elem.findtext(key_tag) or "").strip() == api_name:
                    root.remove(elem)
                    desc = f"<{xml_tag}> '{api_name}'"
                    removed.append(desc)
                    print(f"  [remove] Removed stale {desc}")
    return removed


def write_xml(tree: ET.ElementTree, path: Path) -> None:
    """Pretty-print and write the XML with the Salesforce namespace."""
    ET.register_namespace("", SF_NS)
    ET.indent(tree, space="    ")
    tree.write(str(path), encoding="utf-8", xml_declaration=True)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print(
            "Usage: python3 heal_permset.py <deploy_json_file> <permset_xml_file> "
            "[unhealed_errors_output.json]",
            file=sys.stderr,
        )
        return 1

    json_path       = Path(sys.argv[1])
    permset_path    = Path(sys.argv[2])
    unhealed_path   = Path(sys.argv[3]) if len(sys.argv) == 4 else Path(UNHEALED_ERRORS_DEFAULT)

    # Load deploy JSON
    try:
        deploy_json = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: Could not read deploy JSON from '{json_path}': {exc}",
              file=sys.stderr)
        return 1

    # Build heal plan
    failures = extract_failures(deploy_json)
    if not failures:
        print("No component failures found in deploy JSON.")
        return 2

    print(f"Analysing {len(failures)} component failure(s)...\n")
    plan, unmatched = build_heal_plan(failures)

    # Write unhealed errors to JSON for downstream consumption.
    if unmatched:
        try:
            unhealed_path.parent.mkdir(parents=True, exist_ok=True)
            unhealed_path.write_text(
                json.dumps({"unhealed_failures": unmatched}, indent=2),
                encoding="utf-8",
            )
            print(
                f"\n  [{len(unmatched)} unmatched failure(s) written to '{unhealed_path}' "
                "— manual review required]"
            )
        except OSError as exc:
            print(f"WARNING: Could not write unhealed errors file: {exc}", file=sys.stderr)

    if not plan.has_work:
        print("\nNo healable errors detected.")
        print("Failure is not a dependency or 'not found' error — manual intervention required.")
        return 2

    # Load permission set XML
    try:
        tree = ET.parse(str(permset_path))
    except (OSError, ET.ParseError) as exc:
        print(f"ERROR: Could not parse XML at '{permset_path}': {exc}",
              file=sys.stderr)
        return 1

    root = tree.getroot()

    # Safety threshold check before applying removals.
    if plan.to_remove:
        violations = check_safety_thresholds(root, plan.to_remove)
        if violations:
            print("\n── Safety threshold violations " + "─" * 36, flush=True)
            for v in violations:
                print(f"  ABORT: {v}", flush=True)
            print(
                "\nHeal aborted to prevent deploying a dangerously sparse permission set.\n"
                "Investigate the deployment errors manually.",
                file=sys.stderr,
            )
            return 1

    # Apply heal plan
    print()
    added   = inject_permissions(root, plan.to_inject)   if plan.to_inject  else []
    removed = remove_stale_entries(root, plan.to_remove) if plan.to_remove  else []

    if not added and not removed:
        print("\nXML already reflects expected state — nothing changed.")
        return 2

    # Write patched XML
    try:
        write_xml(tree, permset_path)
    except OSError as exc:
        print(f"ERROR: Could not write patched XML to '{permset_path}': {exc}",
              file=sys.stderr)
        return 1

    # Summary
    print("\n── Heal summary " + "─" * 50)
    if added:
        print(f"  Injected  ({len(added)}):  {', '.join(added)}")
    if removed:
        print(f"  Removed   ({len(removed)}):  {', '.join(removed)}")
    if unmatched:
        print(f"  Unmatched ({len(unmatched)}):  see '{unhealed_path}' for details")
    print(f"\n  Updated: {permset_path}")
    return 0   # pipeline: healed, safe to commit + retry


if __name__ == "__main__":
    sys.exit(main())