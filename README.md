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
unknown statuses, API/authentication failures, changed configuration, exhausted
budgets, and inconsistent state stop automation. Raw Oracle logs and state are
never uploaded. Safe status, error class, Oracle error code and request ID are
saved in the run summary and diagnostic artifact.

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
LaunchAgent: `~/Library/LaunchAgents/dev.gendi.oracle-vps-autoprovision.plist`.
To stop the local timer:

```sh
launchctl bootout gui/$(id -u)/dev.gendi.oracle-vps-autoprovision
```
