"""Optional macOS fallback: dispatch the cloud workflow only when it is overdue."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import re
from diagnostics import error_fields, report, safe_text

REPO = 'gennadygennady/oracle-vps-autoprovision'
WORKFLOW = 'provision.yml'
INTERVAL = 180
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
    report('timer', message=message)


class GitHubAPIError(RuntimeError):
    def __init__(self, operation, result):
        self.operation_name = operation
        self.status = int(re.search(r'HTTP (\d{3})',result.stderr).group(1)) if re.search(r'HTTP (\d{3})',result.stderr) else None
        self.exit_code = result.returncode
        super().__init__(safe_text(result.stderr.strip() or result.stdout.strip() or 'GitHub CLI returned no error text'))


def describe(choice, workflow, runs, now):
    latest = runs[0] if runs else {}
    result={'decision':choice,'workflow_state':workflow['state'],
            'latest_run_url':latest.get('html_url'), 'latest_result':latest.get('conclusion'),
            'latest_event':latest.get('event')}
    explanations={'disabled':'Workflow disabled: no Apply will be dispatched; see safety/success Issue below',
        'busy':'A workflow is queued or running; waiting to avoid overlap',
        'review':'Last workflow failed or was canceled; review its diagnostic before resuming',
        'recent':'Last run is newer than 180 seconds; waiting',
        'dispatch':'No recent or active workflow; submitting a cloud attempt'}
    result['message']=explanations[choice]
    if latest.get('created_at'):
        created=datetime.datetime.fromisoformat(latest['created_at'].replace('Z','+00:00'))
        result['latest_run_age_seconds']=round((now-created).total_seconds())
        result['wait_seconds']=max(0,INTERVAL-result['latest_run_age_seconds']) if choice=='recent' else 0
    return result


def disabled_reason(gh):
    issues=api(gh,'GET','issues?state=open&per_page=100')
    for issue in issues:
        if issue['title'] in {'[autoprovision] Safety stop','[autoprovision] VPS created'}:
            return {'stop_issue_url':issue['html_url'],'stop_title':issue['title'],
                    'stop_reason':safe_text(issue.get('body',''))}
    return {'stop_reason':'No open safety/success Issue found; inspect GitHub workflow settings'}


def api(gh, method, suffix, data=None):
    command = [gh, 'api', '--method', method, f'repos/{REPO}/{suffix}']
    if data is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                            capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise GitHubAPIError(method+' '+suffix.split('?')[0],result)
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
            report('timer_skip',decision='locked',message='Another local timer process holds the lock')
            return 0
        try:
            marker=RUNTIME/'last_tick.json'
            now=datetime.datetime.now(datetime.timezone.utc)
            previous=json.loads(marker.read_text()) if marker.exists() else {}
            gap=(now-datetime.datetime.fromisoformat(previous['tick_at'])).total_seconds() if previous else None
            marker.write_text(json.dumps({'tick_at':now.isoformat()}))
            marker.chmod(0o600)
            report('timer_tick',interval_seconds=INTERVAL,gap_since_last_tick_seconds=round(gap) if gap else None,
                   message='Late tick: Mac sleep, suspension or scheduler delay; missed attempts are not replayed' if gap and gap>INTERVAL+30 else 'Checking GitHub')
            workflow = api(gh, 'GET', f'actions/workflows/{WORKFLOW}')
            # Fetch all recent runs so an older still-active run also blocks dispatch.
            runs = api(gh, 'GET', f'actions/workflows/{WORKFLOW}/runs?per_page=30')['workflow_runs']
            choice = decision(workflow, runs, datetime.datetime.now(datetime.timezone.utc))
            # A tick can arrive a few seconds early relative to the previous POST.
            # Wait the small remainder instead of silently doubling the interval.
            if choice=='recent':
                details=describe(choice,workflow,runs,datetime.datetime.now(datetime.timezone.utc))
                if 0 < details['wait_seconds'] <= 15:
                    report('timer_align',wait_seconds=details['wait_seconds'])
                    time.sleep(details['wait_seconds']+1)
                    workflow=api(gh,'GET',f'actions/workflows/{WORKFLOW}')
                    runs=api(gh,'GET',f'actions/workflows/{WORKFLOW}/runs?per_page=30')['workflow_runs']
                    choice=decision(workflow,runs,datetime.datetime.now(datetime.timezone.utc))
            if choice != 'dispatch':
                details=describe(choice,workflow,runs,datetime.datetime.now(datetime.timezone.utc))
                if choice=='disabled':
                    details.update(disabled_reason(gh))
                report('timer_skip',**details)
                return 0
            # Recheck disabled status immediately before dispatch. The cloud controller
            # independently checks persistent latches, Oracle jobs and Terraform state.
            if api(gh, 'GET', f'actions/workflows/{WORKFLOW}')['state'] != 'active':
                report('timer_skip',decision='disabled',**disabled_reason(gh))
                return 0
            api(gh, 'POST', f'actions/workflows/{WORKFLOW}/dispatches',
                {'ref':'main', 'inputs':{'dry_run':False}})
            report('dispatch_accepted',repo=REPO,ref='main',interval_seconds=INTERVAL,
                   workflow_url=f'https://github.com/{REPO}/actions/workflows/{WORKFLOW}',
                   message='Cloud workflow accepted; Oracle jobs/state are checked there before Apply')
            return 0
        except Exception as error:
            report('timer_error',**error_fields(error),cli_exit_code=getattr(error,'exit_code',None),
                   next_check_seconds=INTERVAL,action='No additional dispatch in this tick; retry at next timer tick')
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
