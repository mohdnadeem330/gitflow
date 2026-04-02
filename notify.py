#!/usr/bin/env python3
"""
notify.py — Send pipeline notifications to Microsoft Teams or Slack.

Reads optional drift-report.json and heal-summary context to build rich,
contextual notifications.  Auto-detects channel format from the webhook URL.

Usage:
    python3 notify.py \\
        --webhook-url URL \\
        --status success|failure|heal|drift \\
        [--build-number N] \\
        [--build-url URL] \\
        [--permset-path PATH] \\
        [--org-alias ALIAS] \\
        [--drift-report PATH] \\
        [--healed-count N] \\
        [--attempt N] \\
        [--channel teams|slack|auto]

Environment variable fallback:
    NOTIFY_WEBHOOK_URL   Used when --webhook-url is not provided.

Exit codes:
    0  — Notification sent successfully (or --webhook-url not set — silently skipped).
    1  — Delivery failed.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Emoji / icon mappings ─────────────────────────────────────────────────────

STATUS_EMOJI = {
    "success": "✅",
    "failure": "❌",
    "heal":    "🔧",
    "drift":   "📊",
    "warning": "⚠️",
}

STATUS_COLOR = {
    "success": "Good",      # Teams: green
    "failure": "Attention", # Teams: red
    "heal":    "Warning",   # Teams: yellow
    "drift":   "Accent",    # Teams: blue
    "warning": "Warning",
}

STATUS_COLOR_HEX = {
    "success": "#28a745",
    "failure": "#dc3545",
    "heal":    "#ffc107",
    "drift":   "#17a2b8",
    "warning": "#ffc107",
}

STATUS_LABEL = {
    "success": "Reconciliation succeeded",
    "failure": "Reconciliation FAILED",
    "heal":    "Auto-healed and retrying",
    "drift":   "Org drift detected",
    "warning": "Warning",
}


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    import os
    parser = argparse.ArgumentParser(
        description="Send a Salesforce permission set pipeline notification to Teams or Slack."
    )
    parser.add_argument(
        "--webhook-url",
        default=os.environ.get("NOTIFY_WEBHOOK_URL", ""),
        metavar="URL",
        help="Webhook URL. Defaults to NOTIFY_WEBHOOK_URL env var.",
    )
    parser.add_argument(
        "--status",
        required=True,
        choices=["success", "failure", "heal", "drift", "warning"],
        help="Pipeline outcome status.",
    )
    parser.add_argument(
        "--build-number",
        default="",
        metavar="N",
        help="Azure DevOps build number.",
    )
    parser.add_argument(
        "--build-url",
        default="",
        metavar="URL",
        help="URL to the Azure DevOps build.",
    )
    parser.add_argument(
        "--permset-path",
        default="",
        metavar="PATH",
        help="Output path of the permission set file.",
    )
    parser.add_argument(
        "--org-alias",
        default="",
        metavar="ALIAS",
        help="Salesforce org alias for context.",
    )
    parser.add_argument(
        "--drift-report",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to drift-report.json to include in the notification.",
    )
    parser.add_argument(
        "--healed-count",
        type=int,
        default=0,
        metavar="N",
        help="Number of items auto-healed (injected + removed).",
    )
    parser.add_argument(
        "--attempt",
        type=int,
        default=1,
        metavar="N",
        help="Current deploy attempt number (for heal notifications).",
    )
    parser.add_argument(
        "--channel",
        choices=["teams", "slack", "auto"],
        default="auto",
        help="Notification channel format (default: auto-detect from URL).",
    )
    return parser.parse_args()


# ── Drift report loading ──────────────────────────────────────────────────────

def load_drift_report(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def drift_summary_lines(report: dict) -> list[str]:
    """Return compact change-count lines from a drift report."""
    lines = []
    for section, s in report.get("summary", {}).items():
        if s.get("added") or s.get("removed"):
            lines.append(
                f"`{section}`: +{s['added']} / -{s['removed']} (total: {s['total']})"
            )
    return lines


# ── Channel detection ─────────────────────────────────────────────────────────

def detect_channel(webhook_url: str) -> str:
    lower = webhook_url.lower()
    if "hooks.slack.com" in lower or "slack.com/services" in lower:
        return "slack"
    if (
        "webhook.office.com" in lower
        or "outlook.office.com" in lower
        or "teams.microsoft.com" in lower
        # Power Automate workflow webhooks (replacement for retired O365 connector)
        or "powerplatform.com" in lower
        or "powerautomate" in lower
        or "/workflows/" in lower
    ):
        return "teams"
    return "slack"   # default to Slack if unrecognised


# ── Teams payload (Adaptive Card via Incoming Webhook / Power Automate) ───────

def build_teams_payload(args: argparse.Namespace, drift: dict | None) -> dict:
    status   = args.status
    emoji    = STATUS_EMOJI.get(status, "ℹ️")
    label    = STATUS_LABEL.get(status, status.title())
    color    = STATUS_COLOR.get(status, "Default")
    now_utc  = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    title = f"{emoji} SalesforceBackup Permset — {label}"

    facts = []
    if args.org_alias:
        facts.append({"title": "Org",   "value": args.org_alias})
    if args.build_number:
        facts.append({"title": "Build", "value": args.build_number})
    facts.append({"title": "Time", "value": now_utc})
    if args.permset_path:
        facts.append({"title": "File",  "value": args.permset_path})
    if args.status == "heal" and args.attempt:
        facts.append({"title": "Heal attempt", "value": str(args.attempt)})
    if args.healed_count:
        facts.append({"title": "Items healed", "value": str(args.healed_count)})

    body_blocks = [
        {
            "type": "TextBlock",
            "text": title,
            "size": "Medium",
            "weight": "Bolder",
            "color": color,
        },
        {
            "type": "FactSet",
            "facts": [{"title": f["title"], "value": f["value"]} for f in facts],
        },
    ]

    # Drift detail block.
    if drift and drift.get("drift_detected"):
        drift_lines = drift_summary_lines(drift)
        if drift_lines:
            body_blocks.append({
                "type": "TextBlock",
                "text": "**Drift detected:**",
                "weight": "Bolder",
                "spacing": "Medium",
            })
            body_blocks.append({
                "type": "TextBlock",
                "text": "\n".join(f"• {line}" for line in drift_lines),
                "wrap": True,
            })

        # Dangerous permissions warning.
        dangerous = drift.get("dangerous_permissions_added", [])
        if dangerous:
            body_blocks.append({
                "type": "TextBlock",
                "text": f"⚠️ **Security alert:** new elevated permission(s): {', '.join(dangerous)}",
                "color": "Attention",
                "wrap": True,
                "spacing": "Small",
            })

    actions = []
    if args.build_url:
        actions.append({
            "type": "Action.OpenUrl",
            "title": "View Build",
            "url": args.build_url,
        })

    card: dict = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type":    "AdaptiveCard",
        "version": "1.4",
        "body":    body_blocks,
    }
    if actions:
        card["actions"] = actions

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content":     card,
            }
        ],
    }


# ── Slack payload (Block Kit) ─────────────────────────────────────────────────

def build_slack_payload(args: argparse.Namespace, drift: dict | None) -> dict:
    status   = args.status
    emoji    = STATUS_EMOJI.get(status, "ℹ️")
    label    = STATUS_LABEL.get(status, status.title())
    color    = STATUS_COLOR_HEX.get(status, "#aaaaaa")
    now_utc  = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    header_text = f"{emoji} *SalesforceBackup Permset — {label}*"

    meta_parts = []
    if args.org_alias:
        meta_parts.append(f"*Org:* {args.org_alias}")
    if args.build_number:
        meta_parts.append(f"*Build:* {args.build_number}")
    meta_parts.append(f"*Time:* {now_utc}")
    if args.permset_path:
        meta_parts.append(f"*File:* `{args.permset_path}`")
    if args.status == "heal" and args.attempt:
        meta_parts.append(f"*Heal attempt:* {args.attempt}")
    if args.healed_count:
        meta_parts.append(f"*Items healed:* {args.healed_count}")

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(meta_parts)}},
        {"type": "divider"},
    ]

    # Drift detail.
    if drift and drift.get("drift_detected"):
        drift_lines = drift_summary_lines(drift)
        if drift_lines:
            drift_text = "*Drift detected:*\n" + "\n".join(f"• {l}" for l in drift_lines)
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": drift_text},
            })

        dangerous = drift.get("dangerous_permissions_added", [])
        if dangerous:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⚠️ *Security alert:* elevated permission(s) added: `{', '.join(dangerous)}`",
                },
            })

    if args.build_url:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View Build"},
                    "url":  args.build_url,
                    "style": "primary" if status == "success" else "danger" if status == "failure" else None,
                }
            ],
        })
        # Remove None style entries.
        blocks[-1]["elements"][0] = {
            k: v for k, v in blocks[-1]["elements"][0].items() if v is not None
        }

    # Slack attachment for sidebar color.
    return {
        "attachments": [
            {
                "color": color,
                "blocks": blocks,
            }
        ]
    }


# ── HTTP delivery ─────────────────────────────────────────────────────────────

def post_webhook(url: str, payload: dict) -> bool:
    body = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status_code = resp.getcode()
            if status_code in (200, 202):
                return True
            print(f"WARNING: Webhook returned HTTP {status_code}.", file=sys.stderr)
            return False
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        print(
            f"ERROR: Webhook delivery failed. HTTP {exc.code}: {body_text[:300]}",
            file=sys.stderr,
        )
        return False
    except urllib.error.URLError as exc:
        print(f"ERROR: Could not reach webhook URL: {exc}", file=sys.stderr)
        return False


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    args = parse_args()

    if not args.webhook_url:
        print(
            "No webhook URL configured (--webhook-url / NOTIFY_WEBHOOK_URL). "
            "Notification skipped.",
            flush=True,
        )
        return 0

    drift = load_drift_report(args.drift_report)

    channel = args.channel
    if channel == "auto":
        channel = detect_channel(args.webhook_url)

    print(f"Sending {args.status!r} notification via {channel.title()}...", flush=True)

    if channel == "teams":
        payload = build_teams_payload(args, drift)
    else:
        payload = build_slack_payload(args, drift)

    ok = post_webhook(args.webhook_url, payload)

    if ok:
        print("Notification sent.", flush=True)
        return 0
    else:
        print("Notification delivery failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())