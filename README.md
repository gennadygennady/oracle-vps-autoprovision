# Oracle Always Free VPS Auto-Provisioner

Retries one existing Oracle Resource Manager Stack named `aboba-instance` in
`ap-singapore-2`. Its audited Terraform configuration creates only one
`VM.Standard.A1.Flex` instance (1 OCPU, 6 GB RAM), with an existing subnet and
Ubuntu ARM image. This repository contains no Terraform source, state or keys.

## Operation

The schedule is `2-59/5 * * * *` UTC (minutes 2, 7, 12, ..., 57), with a manual `workflow_dispatch` trigger.
GitHub schedules can be delayed or dropped; this is not a guaranteed five-minute
timer. Public-repository schedules are disabled by GitHub after 60 days without
repository activity. No automatic commits are made to circumvent this behavior.

Each run verifies the home region, Stack identity, Terraform version and variables,
and the SHA-256 of the reviewed configuration. Changed configuration stops the
automation. Terraform state and actual instance status are checked, and active
Resource Manager jobs are skipped. Completed VPS creation disables the workflow
and opens a GitHub Issue. An instance outside state with the expected name causes
a safety stop instead of another creation.

Before creation, the controller inventories all compartments (read access to all
is required) and verifies conservative combined Always Free ceilings: 2 A1 OCPUs,
12 GB RAM, and 200 GB of boot/block storage, including the new instance's default
boot volume. The configured region must be the tenancy's home region. The exact
reviewed image and default disk settings are protected by the configuration hash.
No networking resources, Stack settings or instance parameters are changed.

GitHub concurrency serializes this workflow. Resource Manager job checks and its
Terraform state lock prevent overlapping jobs. Each submission uses a per-run
idempotency token, and SDK request retries are disabled. Do not run separate
provisioners or manually Apply/edit the Stack while this automation is active;
GitHub concurrency cannot lock another operator's external actions.

Only a FAILED Apply whose every Terraform `Error:` heading contains
`Out of host capacity` can be retried. Generic HTTP 500 errors, canceled jobs,
unknown statuses, API/authentication failures (except Oracle throttling), changed configuration, exhausted
budgets, and inconsistent state stop automation. Raw Oracle logs and state are
never uploaded. Safe status, error class, Oracle error code and request ID are
saved in the run summary and diagnostic artifact.

Oracle API HTTP 429 is temporary: the attempt ends without immediately repeating
Apply, and the workflow remains active. The next retry deadline is saved in the
safe diagnostic artifact, and every following workflow reads it before making
Oracle calls. Throttling doubles the current learned attempt interval, starting
with 360 seconds from the default 180, up to 1800 seconds (a longer server
`Retry-After` is respected). After three submitted attempts without throttling
(capacity failures count), the interval decreases by 10%, with a floor of 180
seconds. It does not jump back to 180 after one successful API submission.
Both rate-limit cooldowns and normal adaptive waits are persisted across runs;
no Oracle calls or Apply occur before their deadline. An interrupted
Apply is always discovered through jobs/state on the next actual inspection.
Missing or expired prior diagnostic artifacts cause a clearly logged safety stop.
Response/error diagnostics retain only `Retry-After`, `Date`, `RateLimit-*`,
`X-RateLimit-*` and `RequestLimit-*` headers when supplied. Cookies and authorization
headers are excluded. Submission headers are also saved in `diagnostic.json`.
An empty set (or Date alone) means no limit/reset information was exposed.
The learned `attempt_interval_seconds`, successful-attempt counter and
`next_apply_at` deadline are visible in GitHub diagnostics and Mac logs.

An open safety/success Issue is a durable latch even if workflow disabling fails.
A previous unsuccessful GitHub run is an additional barrier. Do not delete the
Issues or re-enable the workflow without reviewing the reason.

## Credentials and security

Repository Secrets: `OCI_USER_OCID`, `OCI_TENANCY_OCID`, `OCI_FINGERPRINT`,
`OCI_PRIVATE_KEY` (PEM signing key), and `OCI_STACK_ID`.
The region is fixed in code. The API signer consumes the private key in memory.
No SSH private key is required. GitHub permissions are restricted to reading code,
managing this workflow, and creating Issues; repository security settings are not
relaxed. Third-party Actions are pinned to commit hashes. Pull requests and pushes
do not trigger this workflow; only the main branch of the designated repository
can run it. Public Issues must never contain credentials or raw Terraform data.

For stronger IAM isolation, use a dedicated Oracle API user with sufficient
read access to tenancy-wide inventory and job creation on this Stack compartment.
The uploaded key inherits its Oracle user's permissions. Revoke it in Oracle
when the automation is no longer needed; disabling Actions does not revoke keys.

## Checks and manual operation

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
OCI_STACK_ID=<existing-stack-ocid> python provision.py --local --dry-run
gh workflow run provision.yml -R gennadygennady/oracle-vps-autoprovision -f dry_run=true
gh workflow run provision.yml -R gennadygennady/oracle-vps-autoprovision -f dry_run=false
```

Local mode always requires `--dry-run` and reads `~/.oci/config`.
Dry runs submit no Apply and do not disable the workflow or create Issues.
Capacity failure is an expected successful workflow result (`capacity`).
If a job is still running after the observation window, result is `busy`; the
next run inspects that same job before deciding anything else.

To resume after a safety stop: resolve the Oracle/Stack problem first, close the
safety Issue, and enable the workflow. Immediately dispatch a dry run; read-only
runs bypass the latches so the corrected configuration can be checked. A successful
dry run becomes the latest successful run and clears the failed-run barrier.
If a schedule arrives before that dry run, the controller will stop again safely.
Do not change the configuration hash without auditing the new Terraform source.

References:
- https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
- https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows

## Optional Mac fallback timer

Install or update from the local clone (requires Python 3 and an authenticated
GitHub CLI):

```sh
./install.sh
./check.sh
```

`install.sh` restarts this user's timer and installs two code symlinks:

| Installed path | Repository target |
| --- | --- |
| `~/Library/Application Support/OracleAutoProvision/local_timer.py` | `local_timer.py` |
| `~/Library/Application Support/OracleAutoProvision/diagnostics.py` | `diagnostics.py` |

The plist is generated from `macos/launchagent.plist.template` directly into
`~/Library/LaunchAgents/dev.gendi.oracle-vps-autoprovision.plist` as a regular
file with permissions 600, using discovered Python/gh paths. It contains no
credentials. No machine-specific plist is stored in the clone. Keep the clone in place while the timer is
installed. Python source changes are immediately visible through the links;
rerun `install.sh` after changing the interval or plist template. Moving the clone
requires reviewing/replacing its old installation links first.
The installer migrates identical legacy source copies, preserving logs/cache,
and refuses to overwrite locally edited source or unrelated links.

`check.sh` verifies both code symlink targets, the regular LaunchAgents plist, private
directory/file permissions, Python/gh availability, and the loaded LaunchAgent
interval. It is read-only and makes no Oracle requests or workflow dispatches.
It prints the last timer exit code for diagnosis; inspect `timer.log` for failures.

Only code, template, installer, checks and tests are tracked. Logs, lock, caches
stay in Application Support; the machine-specific plist stays in LaunchAgents. Oracle
credentials remain in `~/.oci/` and GitHub Secrets, and are never read by these
installation/check scripts. No installation depends on `/private/tmp` scripts.
Existing installs with a plist symlink are migrated automatically; the obsolete
generated plist and its empty `.local/` directory are removed.

`local_timer.py` uses the existing `gh` login to check the cloud workflow every
180 seconds via a macOS LaunchAgent. It dispatches the same workflow only when it
is active, has no active runs, the last run succeeded, and no run was created
within three minutes. Oracle credentials remain in GitHub Secrets; this timer does
not call Oracle. Cloud concurrency and all safety checks still apply. A simultaneous
GitHub cron event can create a second serialized workflow run, so this does not
guarantee exact three-minute spacing. On workflow success or a safety stop, the
disabled workflow prevents further timer dispatches.

The Mac must be awake and the user logged in. Sleeping/offline time is not replayed.
Logs: `~/Library/Application Support/OracleAutoProvision/timer.log`.
Each tick includes local time with UTC offset, time since the previous tick,
the last workflow event/result/link, and the reason and remaining wait when
skipped. Disabled workflows include the safety/success Issue and its reason.
API errors include the operation, HTTP status if available, CLI exit code,
redacted error text and the planned next check. Long tick gaps are reported;
the Mac's sleep history (`pmset -g log`) can distinguish sleep from other delays.
Oracle logs include each API operation, response counts, Apply status,
redacted error message, request ID, retry deadline and code location.
`diagnostic.json` also carries a `context` object: run ID/attempt/link, commit SHA,
Python and OCI SDK versions, trigger, mode, UTC start/end times, duration and
Oracle job Console link. Resource Manager job OCIDs are resource identifiers and
are preserved in links; authentication credentials and other OCIDs remain masked.
Underlying failures disabling the workflow or creating its stop Issue are logged
separately and retained in `stop_errors` in the same artifact.
The Mac reads and caches each completed run's artifact to show the actual
`capacity`, `throttled`, `cooldown`, `waiting`, `success` or `halted` outcome, rather than
equating GitHub's `success` conclusion with a created VPS. Diagnostic-download
failures are explicitly logged and do not change dispatch safety decisions.
The last outcome is a historical observation. For cached cooldown results,
`oracle_wait_seconds_at_run` preserves the original wait, while
`oracle_wait_seconds` and `oracle_cooldown_active` are recomputed at the current tick.
LaunchAgent: `~/Library/LaunchAgents/dev.gendi.oracle-vps-autoprovision.plist`.
To stop the local timer:

```sh
launchctl bootout gui/$(id -u)/dev.gendi.oracle-vps-autoprovision
```
