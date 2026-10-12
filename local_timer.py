"""Optional macOS fallback: dispatch the cloud workflow only when it is overdue."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess

REPO = 'gennadygennady/oracle-vps-autoprovision'
WORKFLOW = 'provision.yml'
INTERVAL = 300
RUNTIME = Path.home() / 'Library/Application Support/OracleAutoProvision'


def decision(workflow, runs, now):
    if workflow['state'] != 'active':
        return 'disabled'
    if any(r['status'] != 'completed' for r in runs):
        return 'busy'
    if runs:
        latest = runs[0]
        if latest['conclusion'] != 'success':
            return 'review'
        created = datetime.datetime.fromisoformat(latest['created_at'].replace('Z', '+00:00'))
        if (now - created).total_seconds() < INTERVAL:
            return 'recent'
    return 'dispatch'


def log(message):
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    print(f'{stamp} {message}', flush=True)


def api(gh, method, suffix, data=None):
    command = [gh, 'api', '--method', method, f'repos/{REPO}/{suffix}']
    if data is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                            capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError('GitHub API request failed; no credential data logged')
    return json.loads(result.stdout) if result.stdout.strip() else None


def main():
    gh = shutil.which('gh')
    if not gh:
        log('GitHub CLI missing; no dispatch')
        return 1
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = RUNTIME / 'timer.lock'
    with lock.open('a') as handle:
        lock.chmod(0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            workflow = api(gh, 'GET', f'actions/workflows/{WORKFLOW}')
            # Fetch all recent runs so an older still-active run also blocks dispatch.
            runs = api(gh, 'GET', f'actions/workflows/{WORKFLOW}/runs?per_page=30')['workflow_runs']
            choice = decision(workflow, runs, datetime.datetime.now(datetime.timezone.utc))
            if choice != 'dispatch':
                log(f'{choice}; no dispatch')
                return 0
            # Recheck disabled status immediately before dispatch. The cloud controller
            # independently checks persistent latches, Oracle jobs and Terraform state.
            if api(gh, 'GET', f'actions/workflows/{WORKFLOW}')['state'] != 'active':
                log('disabled; no dispatch')
                return 0
            api(gh, 'POST', f'actions/workflows/{WORKFLOW}/dispatches',
                {'ref':'main', 'inputs':{'dry_run':False}})
            log('Overdue workflow dispatched; all Oracle checks run in GitHub')
            return 0
        except Exception as error:
            log(f'{type(error).__name__}; no further dispatch in this tick')
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
