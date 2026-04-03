# Salesforce Permission Set Reconciliation

Automated pipeline that keeps a full-scope Salesforce permission set in sync with org metadata. Every run regenerates the permission set XML from the live org, detects drift, validates the output, commits it to version control, and deploys it back — with self-healing retries if deployment fails.

Built for GitHub Actions with multi-org support via matrix strategy. An Azure DevOps variant (`azure-pipelines.yml`) is also included.

---

## Table of contents

1. [How it works](#how-it-works)
2. [Repository structure](#repository-structure)
3. [Prerequisites](#prerequisites)
4. [Setup](#setup)
5. [Configuration](#configuration)
6. [Usage](#usage)
7. [Pipeline stages](#pipeline-stages)
8. [Script reference](#script-reference)
9. [Multi-org support](#multi-org-support)
10. [Rollback](#rollback)
11. [Notifications](#notifications)
12. [Troubleshooting](#troubleshooting)

---

## How it works

The pipeline follows a full-reconciliation strategy — no delta tracking, no incremental patches. On every run it:

1. **Checks token health** — verifies the Salesforce auth token is alive and warns if expiry is imminent.
2. **Regenerates the permission set** — queries the live org for all permissionable objects, fields, record types, and user permissions, then builds the complete XML from scratch.
3. **Detects drift** — diffs the newly generated XML against the last committed version and produces a structured JSON change report.
4. **Validates** — runs structural and cardinality checks on the XML. Optionally performs a Salesforce check-only (validate-only) deployment.
5. **Commits** — pushes the regenerated XML to the repository if anything changed.
6. **Deploys** — deploys the XML to the org. If deployment fails, the self-healing loop automatically patches the XML (removing stale references or injecting missing dependencies) and retries up to 5 times.
7. **Notifies** — sends a Teams or Slack message on success, failure, or heal events.

```
┌─────────────┐     ┌─────────────┐     ┌──────────────────────────────────────────────┐
│  Schedule    │     │   Manual    │     │              Rollback                        │
│  (cron)     │     │  (dispatch) │     │  (restore SHA → validate → deploy)           │
└──────┬──────┘     └──────┬──────┘     └──────────────────────────────────────────────┘
       │                   │
       └─────────┬─────────┘
                 ▼
        Token health check
         (check_token_expiry.py)
                 │
                 ▼
   ┌─────────────────────────────────┐
   │     Reconcile (per org)         │
   │                                 │
   │  1. Generate   (backuppermissionset.py) │
   │  2. Drift      (drift_report.py)│
   │  3. Validate   (validate_permset.py)│
   │  4. Commit     (git push)       │
   │  5. Deploy     (sf deploy)      │
   │       │                         │
   │       ├─ success ──► notify     │
   │       └─ failure ──► heal ──► retry (×5)│
   │           (heal_permset.py)     │
   └─────────────────────────────────┘
                 │
                 ▼
        Upload artifacts
   (deploy JSON, drift report, unhealed errors)
                 │
                 ▼
        Notify (Teams / Slack)
         (notify.py)
```

---

## Repository structure

```
.
├── .github/
│   └── workflows/
│       └── permset-reconciliation.yml    # GitHub Actions workflow
├── force-app/
│   └── main/
│       └── default/
│           └── permissionsets/
│               └── SalesforceBackup.permissionset-meta.xml  # Generated output
├── backuppermissionset.py                # Core generator — queries org, builds XML
├── validate_permset.py                   # Structural + cardinality validation
├── drift_report.py                       # Diff old vs new XML, produce change report
├── heal_permset.py                       # Self-healing — patch XML on deploy failure
├── notify.py                             # Teams / Slack webhook notifications
├── check_token_expiry.py                 # Token health checker
├── azure-pipelines.yml                   # Azure DevOps variant (optional)
├── sfdx-project.json                     # Salesforce DX project config
└── README.md
```

---

## Prerequisites

- **Python 3.12+**
- **Salesforce CLI (`sf`)** — [install guide](https://developer.salesforce.com/tools/salesforcecli)
- **Salesforce connected app** with an SFDX auth URL for each org
- **GitHub repository** with Actions enabled
- **sfdx-project.json** at the repository root with a valid `packageDirectories` entry covering the output path

---

## Setup

### 1. Generate an SFDX auth URL

Authenticate to your org locally, then export the auth URL:

```bash
sf org login web --alias prod-backup --set-default
sf org display --target-org prod-backup --verbose --json
```

Copy the `sfdxAuthUrl` value from the JSON output. It looks like `force://PlatformCLI::...@yourinstance.my.salesforce.com`.

### 2. Configure repository secrets

Go to **Settings → Secrets and variables → Actions → Secrets** and add:

| Secret | Description |
|--------|-------------|
| `SFDX_AUTH_URL` | SFDX auth URL for the primary org |
| `NOTIFY_WEBHOOK_URL` | *(Optional)* Teams or Slack incoming webhook URL |

For additional orgs, add their auth URLs as separate secrets (e.g. `SFDX_AUTH_URL_SANDBOX`).

### 3. Configure repository variables

Go to **Settings → Secrets and variables → Actions → Variables** and add:

| Variable | Default | Description |
|----------|---------|-------------|
| `SF_API_VERSION` | `66.0` | Salesforce API version for all metadata queries |
| `PERMSET_BRANCH` | `main` | Branch to commit regenerated XML to |
| `DEPLOY_ENABLED` | `true` | Set to `false` to skip deployment (generate + commit only) |
| `DEPLOY_WAIT_MINUTES` | `10` | Minutes to wait for deployment completion |

### 4. Verify sfdx-project.json

Ensure the output path is covered by a package directory:

```json
{
  "packageDirectories": [
    { "path": "force-app", "default": true }
  ],
  "sfdcLoginUrl": "https://login.salesforce.com",
  "sourceApiVersion": "66.0"
}
```

### 5. Enable the workflow

The workflow file at `.github/workflows/permset-reconciliation.yml` runs on a daily schedule by default. Push it to `main` and it will begin running automatically. You can also trigger it manually from the Actions tab.

---

## Configuration

### Workflow dispatch inputs

When triggering the workflow manually, these inputs are available:

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `skip_deploy` | boolean | `false` | Generate and commit only — skip deployment |
| `dry_run` | boolean | `false` | Generate only — no commit, no deploy |
| `run_validation` | boolean | `false` | Run a Salesforce check-only deploy before the real deploy |
| `fail_on_dangerous_permissions` | boolean | `false` | Halt if a dangerous permission (e.g. `ModifyAllData`) is newly added |
| `rollback` | boolean | `false` | Rollback mode — redeploy from a known-good commit |
| `rollback_sha` | string | `""` | Git SHA to roll back to (required when `rollback=true`) |
| `notify_on_success` | boolean | `true` | Send notification on success |
| `notify_on_failure` | boolean | `true` | Send notification on failure or heal events |

### Schedule

The default schedule is daily at 02:00 UTC. To change it, edit the `cron` expression in the workflow file:

```yaml
schedules:
  - cron: "0 2 * * *"   # Daily at 02:00 UTC
```

For business-hours runs, add additional entries:

```yaml
schedules:
  - cron: "0 2 * * *"
  - cron: "0 6,10,14,18 * * 1-5"   # Every 4 hours on weekdays
```

### Org matrix

Each org is defined in the `strategy.matrix.org` array. The matrix is repeated in the `token-check`, `reconcile`, and `rollback` jobs — keep them in sync when adding orgs.

```yaml
matrix:
  org:
    - alias: prod-backup
      auth_secret: SFDX_AUTH_URL          # Name of the GitHub secret
      permset_name: SalesforceBackup       # API name of the permission set
      permset_label: "Salesforce Backup"   # Display label
      output_path: "force-app/main/default/permissionsets/SalesforceBackup.permissionset-meta.xml"
```

---

## Usage

### Scheduled run (default)

No action needed. The workflow runs daily at 02:00 UTC.

### Manual run

1. Go to **Actions → Salesforce Permission Set Reconciliation → Run workflow**.
2. Select the branch and configure inputs.
3. Click **Run workflow**.

### Dry run (no side effects)

Trigger manually with `dry_run: true`. The pipeline generates the XML and runs validation but does not commit or deploy.

### Skip deployment

Trigger manually with `skip_deploy: true`. The pipeline generates, validates, and commits but does not deploy.

### Validate-only deployment

Trigger manually with `run_validation: true`. Runs a Salesforce check-only deploy (no metadata committed to the org) before the real deployment.

### Dangerous permission gate

Trigger manually with `fail_on_dangerous_permissions: true`. The pipeline halts with exit code 2 if any of these permissions are newly added in the current run:

- `ModifyAllData`
- `ViewAllData`
- `ModifyMetadata`
- `ManageUsers`
- `ResetPasswords`
- `ManagePasswordPolicies`
- `ManageProfilesPermissionsets`

### Rollback

See the [Rollback](#rollback) section below.

---

## Pipeline stages

### Token health check

**Job:** `token-check`
**Script:** `check_token_expiry.py`
**Timeout:** 5 minutes

Authenticates to each org and checks:
- Is the token still valid? (Hard fail if expired or invalid.)
- Is the token expiring within `TOKEN_WARN_DAYS` (default: 7)? (Warning annotation, non-blocking.)

Skipped entirely in rollback mode.

### Reconcile

**Job:** `reconcile`
**Timeout:** 45 minutes
**Depends on:** `token-check` (success or skipped)

Runs once per org in the matrix, in parallel. Each job walks through five steps:

#### Step 1 — Generate permission set XML

**Script:** `backuppermissionset.py`

Queries the live org via REST API, Tooling API, and Metadata API to discover:
- All permissionable objects (via `PicklistValueInfo` + `EntityDefinition`)
- All permissionable fields per object (via `EntityParticle` and `FieldDefinition`)
- All record types in scope (via Metadata API `listMetadata`)
- A fixed set of user permissions

Builds the full permission set XML and writes it to the output path.

**Key behaviors:**
- Excludes `ChangeEvent` and `__e` (platform event) objects automatically.
- Excludes compound component fields (e.g. `BillingStateCode`).
- Excludes `Account.*__pc` (Person Account alias fields).
- Handles `PersonAccount.PersonAccount` record type normalization.
- Falls back to `FieldDefinition` for objects absent from `EntityParticle` (e.g. `EmailMessage`).

#### Step 2 — Drift detection

**Script:** `drift_report.py`

Compares the previously committed XML against the newly generated one. Produces:
- A human-readable summary printed to the job log
- A machine-readable JSON report (uploaded as an artifact)
- Optional hard fail if dangerous permissions were newly added (`--fail-on-dangerous`)

#### Step 3 — Validate

**Script:** `validate_permset.py`

Runs structural and cardinality checks:
- File exists and is well-formed XML
- Correct Salesforce metadata namespace
- Root element is `PermissionSet`
- `<label>` is present and non-empty
- `objectPermissions` count ≥ 10 (configurable via `--min-objects`)
- `fieldPermissions` count ≥ 50 (configurable via `--min-fields`)
- `recordTypeVisibilities` count ≥ 0 (configurable via `--min-record-types`)
- No duplicate entries within any section
- Warns (non-blocking) if dangerous user permissions are present

Optionally runs a Salesforce check-only deploy (`--check-only`) to catch metadata resolution errors.

#### Step 4 — Commit

Stages the regenerated XML and pushes to the configured branch. Skips the commit if nothing changed (org state matches repo).

Commit message format:
```
chore: reconcile SalesforceBackup [2026-04-03 02:15 UTC] | org=prod-backup | run=42
```

#### Step 5 — Deploy (self-healing)

Deploys the XML via `sf project deploy start`. On failure, enters the self-healing loop:

1. `heal_permset.py` parses the deployment error JSON.
2. **Inject:** If errors indicate missing permission dependencies (e.g. `"CustomizeApplication depends on permission(s): ManageTerritories"`), the missing `<userPermissions>` block is added.
3. **Remove:** If errors indicate stale references (e.g. `"no RecordType named Account.X found"`), the corresponding XML block is removed.
4. Safety thresholds prevent catastrophic over-removal (e.g. `fieldPermissions` cannot drop below 20).
5. The patched XML is committed and the deploy is retried.
6. Repeats up to 5 attempts. If healing cannot resolve the errors, the pipeline fails and surfaces unhealed errors for manual review.

Heal commit message format:
```
fix: auto-heal SalesforceBackup attempt 2 [2026-04-03 02:20 UTC] | org=prod-backup | run=42
```

### Rollback

**Job:** `rollback`
**Timeout:** 30 minutes

See the [Rollback](#rollback-1) section below.

---

## Script reference

All scripts are standalone CLI tools with `--help` documentation. They have no third-party Python dependencies — only the standard library.

### backuppermissionset.py

```
python3 backuppermissionset.py \
  --target-org prod-backup \
  --output-file force-app/.../SalesforceBackup.permissionset-meta.xml \
  --api-version 66.0 \
  --name SalesforceBackup \
  --label "Salesforce Backup"
```

| Flag | Description |
|------|-------------|
| `--target-org`, `-o` | Salesforce org alias or username |
| `--output-file`, `-f` | Output XML path |
| `--api-version`, `-v` | Salesforce API version (default: `66.0`) |
| `--name`, `-n` | Permission set API name (default: `SalesforceBackup`) |
| `--label`, `-l` | Permission set label (default: `Salesforce Backup`) |
| `--description` | Description text embedded in the XML |
| `--deploy`, `-d` | Deploy immediately after generation |
| `--deploy-wait`, `-w` | Minutes to wait for deployment (default: `10`) |

### validate_permset.py

```
python3 validate_permset.py path/to/permset.xml \
  --min-objects 10 \
  --min-fields 50 \
  --check-only --target-org prod-backup --wait 10
```

| Flag | Description |
|------|-------------|
| `--min-objects N` | Minimum `objectPermissions` count (default: `10`) |
| `--min-fields N` | Minimum `fieldPermissions` count (default: `50`) |
| `--min-record-types N` | Minimum `recordTypeVisibilities` count (default: `0`) |
| `--check-only` | Run a Salesforce validate-only deploy |
| `--target-org` | Org alias for check-only deploy |
| `--wait` | Minutes to wait for check-only deploy (default: `10`) |
| `--ado` | Emit Azure DevOps annotations |

**Exit codes:** `0` = pass, `1` = fail.

### drift_report.py

```
python3 drift_report.py old.xml new.xml \
  --output drift-report.json \
  --fail-on-dangerous
```

| Flag | Description |
|------|-------------|
| `--output PATH` | JSON report output path (default: `drift-report.json`) |
| `--ado` | Emit Azure DevOps annotations |
| `--fail-on-dangerous` | Exit `2` if dangerous permissions were newly added |

**Exit codes:** `0` = report generated, `1` = error, `2` = dangerous permission gate tripped.

**JSON report schema:**

```json
{
  "timestamp": "2026-04-03T02:15:00+00:00",
  "drift_detected": true,
  "summary": {
    "fieldPermissions":       { "total": 1200, "added": 5, "removed": 2, "net": 3 },
    "objectPermissions":      { "total": 85,   "added": 1, "removed": 0, "net": 1 },
    "recordTypeVisibilities": { "total": 30,   "added": 0, "removed": 1, "net": -1 },
    "userPermissions":        { "total": 45,   "added": 0, "removed": 0, "net": 0 }
  },
  "details": {
    "fieldPermissions":       { "added": ["Account.NewField__c"], "removed": ["Lead.OldField__c"] },
    "objectPermissions":      { "added": ["NewObject__c"],        "removed": [] },
    "recordTypeVisibilities": { "added": [],                       "removed": ["Case.Archived"] },
    "userPermissions":        { "added": [],                       "removed": [] }
  },
  "dangerous_permissions_added": []
}
```

### heal_permset.py

```
python3 heal_permset.py deploy-result.json permset.xml [unhealed-errors.json]
```

| Argument | Description |
|----------|-------------|
| `deploy_json_file` | Path to the `sf deploy` JSON output |
| `permset_xml_file` | Path to the permission set XML to patch |
| `unhealed_errors_output.json` | *(Optional)* Output path for unmatched errors (default: `unhealed-errors.json`) |

**Exit codes:** `0` = patched successfully, `1` = unexpected error, `2` = no healable errors found.

**Supported heal patterns:**

| Category | Error pattern | Action |
|----------|---------------|--------|
| Inject | `depends on permission(s): X` | Add `<userPermissions>` for `X` |
| Remove | `no RecordType named X found` | Remove `<recordTypeVisibilities>` |
| Remove | `no CustomField named X found` | Remove `<fieldPermissions>` |
| Remove | `no CustomObject named X found` | Remove `<objectPermissions>` |
| Remove | `no UserPermission named X found` | Remove `<userPermissions>` |
| Remove | `no ApexPage named X found` | Remove `<pageAccesses>` |
| Remove | `no ApexClass named X found` | Remove `<classAccesses>` |
| Remove | `no CustomTab named X found` | Remove `<tabSettings>` |
| Remove | `no CustomApplication named X found` | Remove `<applicationVisibilities>` |
| Remove | `no Flow named X found` | Remove `<flowAccesses>` |
| Remove | `no CustomPermission named X found` | Remove `<customPermissions>` |
| Remove | `no CustomMetadataType named X found` | Remove `<customMetadataTypeAccesses>` |
| Remove | `no ConnectedApplication named X found` | Remove `<connectedAppAccesses>` |
| Remove | `no CustomSetting named X found` | Remove `<customSettingAccesses>` |
| Remove | `no ExternalDataSource named X found` | Remove `<externalDataSourceAccesses>` |

**Safety thresholds** (prevent catastrophic over-removal):

| Section | Minimum after removal |
|---------|----------------------|
| `fieldPermissions` | 20 |
| `objectPermissions` | 5 |
| `recordTypeVisibilities` | 0 (no limit) |
| `userPermissions` | 0 (no limit) |

### check_token_expiry.py

```
sf org display --target-org prod-backup --json | python3 check_token_expiry.py \
  --warn-days 7 \
  --org-alias prod-backup
```

| Flag | Description |
|------|-------------|
| `--warn-days N` | Warn if token expires within N days (default: `7`) |
| `--org-alias` | Label for log messages |

**Exit codes:** `0` = healthy, `1` = expired or unreachable.

### notify.py

```
python3 notify.py \
  --status success \
  --org-alias prod-backup \
  --build-number 42 \
  --build-url https://github.com/org/repo/actions/runs/123 \
  --permset-path force-app/.../SalesforceBackup.permissionset-meta.xml \
  --drift-report drift-report.json
```

| Flag | Description |
|------|-------------|
| `--webhook-url` | Webhook URL (falls back to `NOTIFY_WEBHOOK_URL` env var) |
| `--status` | One of: `success`, `failure`, `heal`, `drift`, `warning` |
| `--build-number` | CI run number |
| `--build-url` | Link to the CI run |
| `--permset-path` | Permission set file path (for context) |
| `--org-alias` | Org alias (for context) |
| `--drift-report` | Path to drift report JSON (included in notification) |
| `--healed-count` | Number of items auto-healed |
| `--attempt` | Current deploy attempt number |
| `--channel` | `teams`, `slack`, or `auto` (default: auto-detect from URL) |

Auto-detects Teams vs Slack from the webhook URL. Sends Adaptive Cards to Teams and Block Kit messages to Slack. Silently skips if no webhook URL is configured.

---

## Multi-org support

To add a second org:

1. **Add the auth URL secret** — e.g. `SFDX_AUTH_URL_SANDBOX`.

2. **Add a matrix entry** in all three jobs (`token-check`, `reconcile`, `rollback`):

```yaml
matrix:
  org:
    - alias: prod-backup
      auth_secret: SFDX_AUTH_URL
      permset_name: SalesforceBackup
      permset_label: "Salesforce Backup"
      output_path: "force-app/main/default/permissionsets/SalesforceBackup.permissionset-meta.xml"
    - alias: sandbox-backup
      auth_secret: SFDX_AUTH_URL_SANDBOX
      permset_name: SalesforceBackup
      permset_label: "Salesforce Backup"
      output_path: "force-app/main/default/permissionsets/SalesforceBackup.permissionset-meta.xml"
```

Each org runs as a parallel job. Jobs are independent — one org failing does not block others (`fail-fast: false` on the reconcile and rollback jobs).

---

## Rollback

Rollback restores a permission set from a specific git commit and deploys it. No regeneration, no drift detection, no healing — the git history is the rollback catalogue.

### How to roll back

1. Find the SHA of the known-good commit:
   ```bash
   git log --oneline -- force-app/main/default/permissionsets/SalesforceBackup.permissionset-meta.xml
   ```

2. Trigger the workflow manually with:
   - `rollback`: `true`
   - `rollback_sha`: the SHA from step 1

3. The rollback job will:
   - Restore the XML from the specified SHA
   - Validate it
   - Authenticate and deploy it
   - Commit the restored XML to the current branch
   - Send a notification

### Rollback commit format

```
rollback: prod-backup → abc1234 [2026-04-03 03:00 UTC] | run=43
```

---

## Notifications

Notifications are sent via incoming webhooks to Microsoft Teams or Slack. The channel is auto-detected from the webhook URL.

### Notification types

| Status | When | Emoji |
|--------|------|-------|
| `success` | Reconciliation completed and deployed | ✅ |
| `failure` | Deploy failed after all heal attempts | ❌ |
| `heal` | Self-healing triggered (first attempt) | 🔧 |
| `drift` | Drift detected (informational) | 📊 |

### What's included

- Org alias, build number, timestamp, permset file path
- Link to the GitHub Actions run
- Drift summary (if drift was detected): added/removed counts per section
- Security alert if dangerous permissions were newly added
- Heal attempt number (for heal notifications)

### Disabling notifications

- Remove the `NOTIFY_WEBHOOK_URL` secret — notifications are silently skipped
- Set `notify_on_success: false` or `notify_on_failure: false` on manual runs
- Scheduled runs default to notifying on both success and failure

---

## Troubleshooting

### Token expired or invalid

```
ERROR: [prod-backup] Token invalid — connectedStatus: 'RefreshTokenAuthError'.
```

Rotate the `SFDX_AUTH_URL` secret. Re-authenticate locally with `sf org login web`, export the new auth URL, and update the secret in GitHub.

### Deployment fails with stale references

```
In field: recordType - no RecordType named Account.OldType found
```

This is handled automatically by the self-healing loop. If it persists after 5 attempts, check the unhealed errors artifact and resolve manually.

### Safety threshold abort

```
ABORT: Removing 25 <fieldPermissions> entries would leave 18 (minimum safe threshold: 20).
```

The heal aborted because too many removals would produce a dangerously sparse permission set. Investigate why so many fields were removed — likely a bulk metadata deletion in the org.

### No permissionable fields found

```
Error: No permissionable fields found. Check org permissions.
```

The connected user lacks permission to query `EntityParticle` or `FieldDefinition`. Ensure the auth user has `ViewSetup` and `ModifyAllData` (or equivalent).

### sf CLI not found

```
Error: Salesforce CLI (sf) was not runnable from this environment.
```

The `sf` command is not on PATH. In CI this is handled by the `npm install --global @salesforce/cli` step. Locally, install from [developer.salesforce.com/tools/salesforcecli](https://developer.salesforce.com/tools/salesforcecli).

### Commit fails with permission error

Ensure the workflow has write permissions. The `actions/checkout@v4` step uses `token: ${{ secrets.GITHUB_TOKEN }}` which must have `contents: write` permission. If using branch protection rules, you may need to allow the `github-actions[bot]` user to push.

### Concurrency conflicts

The workflow uses a concurrency group per branch to prevent overlapping runs:

```yaml
concurrency:
  group: permset-reconciliation-${{ github.ref }}
  cancel-in-progress: false
```

If a run is already in progress, subsequent triggers queue rather than cancel the active run.

---

## Pipeline artifacts

Every run uploads three artifacts (retained for 30 days):

| Artifact | Contents |
|----------|----------|
| `deploy-result-{org}-{run_id}` | Raw JSON from `sf project deploy start` |
| `drift-report-{org}-{run_id}` | Structured drift report JSON |
| `unhealed-errors-{org}-{run_id}` | Errors that `heal_permset.py` could not resolve |

Download from the **Actions → Run → Artifacts** section in GitHub.

---

## Local development

Run any script locally against a connected org:

```bash
# Authenticate
sf org login web --alias prod-backup

# Generate
python3 backuppermissionset.py --target-org prod-backup

# Validate
python3 validate_permset.py force-app/main/default/permissionsets/SalesforceBackup.permissionset-meta.xml

# Drift report
python3 drift_report.py old.xml new.xml --output drift.json

# Deploy
sf project deploy start --ignore-conflicts \
  --source-dir force-app/main/default/permissionsets/SalesforceBackup.permissionset-meta.xml \
  --target-org prod-backup --wait 10
```

No `pip install` required — all scripts use the Python standard library only.
