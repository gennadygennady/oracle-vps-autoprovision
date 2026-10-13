"""Fail-closed controller for one pre-existing, audited Oracle RM stack."""
import argparse
import datetime
from email.utils import parsedate_to_datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import time
import platform
import subprocess
import urllib.request
import urllib.error
import zipfile

import oci
from diagnostics import error_fields, report, safe_text, read_diagnostic_zip, rate_limit_headers

REGION = 'ap-singapore-2'
CONFIG_SHA256 = '5266612964d57c18270e5cc78917639a487de10b33b36ae61f7a189591614733'
WORKFLOW = 'provision.yml'
STOP_TITLE = '[autoprovision] Safety stop'
SUCCESS_TITLE = '[autoprovision] VPS created'
ACTIVE_JOBS = {'ACCEPTED', 'IN_PROGRESS', 'CANCELING'}


class SafetyStop(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise SafetyStop(reason)


def capacity_only(messages):
    text = re.sub(r'\x1b\[[0-9;]*m', '', '\n'.join(messages))
    errors = re.findall(r'(?m)^\s*(?:│\s*)?Error:\s*([^\n]+)', text)
    return bool(errors) and all('out of host capacity' in e.lower() for e in errors)


def config_digest(content):
    archive = zipfile.ZipFile(io.BytesIO(content))
    h = hashlib.sha256()
    for name in sorted(archive.namelist()):
        if not name.endswith('/'):
            h.update(name.encode() + b'\0' + archive.read(name) + b'\0')
    return h.hexdigest()


def state_instances(state):
    result = []
    for resource in state.get('resources', []):
        if resource.get('mode') == 'managed':
            require(resource['type'] == 'oci_core_instance', 'Unexpected managed state resource')
            for item in resource.get('instances', []):
                require(not item.get('deposed'), 'Deposed instance requires manual review')
                require(item.get('attributes', {}).get('id'), 'State instance has no ID')
                result.append(item['attributes']['id'])
    require(len(result) <= 1, 'Multiple state instances')
    return result


def check_free_budget(instances, boot, volumes, boot_gb):
    live = [i for i in instances if i.lifecycle_state != 'TERMINATED']
    a1 = [i for i in live if i.shape == 'VM.Standard.A1.Flex']
    require(sum(i.shape_config.ocpus for i in a1) + 1 <= 2, 'A1 OCPU Always Free budget exceeded')
    require(sum(i.shape_config.memory_in_gbs for i in a1) + 6 <= 12, 'A1 memory Always Free budget exceeded')
    disks = [v for v in boot + volumes if v.lifecycle_state != 'TERMINATED']
    require(sum(v.size_in_gbs for v in disks) + boot_gb <= 200, 'Always Free disk budget exceeded')


class GitHub:
    def __init__(self):
        self.repo = os.environ['GITHUB_REPOSITORY']
        self.token = os.environ['GH_TOKEN']

    def request(self, method, path, body=None):
        self.operation = method + ' ' + path.split('?')[0]
        req = urllib.request.Request('https://api.github.com/repos/' + self.repo + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                content = response.read()
                return json.loads(content) if content else None
        except urllib.error.HTTPError as error:
            error.operation_name=self.operation
            error.request_id=error.headers.get('X-GitHub-Request-Id')
            try:
                body=json.loads(error.read(65536))
                error.message=safe_text(body.get('message',str(error)))
            except (ValueError,TypeError):
                error.message=safe_text(str(error))
            finally:
                error.close()
            raise

    def issues(self):
        page = 1
        while True:
            items = self.request('GET', f'/issues?state=open&per_page=100&page={page}')
            yield from items
            if len(items) < 100:
                return
            page += 1

    def guard(self, read_only=False):
        issues = list(self.issues())
        if read_only:
            return None
        require(not any(i['title'] == STOP_TITLE for i in issues), 'Persistent safety stop issue is open')
        require(not any(i['title'] == SUCCESS_TITLE for i in issues), 'Persistent success latch is open')
        runs = self.request('GET', f'/actions/workflows/{WORKFLOW}/runs?status=completed&per_page=1')['workflow_runs']
        require(not runs or runs[0]['conclusion'] == 'success', 'Previous workflow did not finish successfully')
        self.previous_diagnostic = self.read_diagnostic(runs[0]) if runs else {}
        return pending_cooldown(self.previous_diagnostic)

    def read_diagnostic(self, run):
        self.operation = 'Read prior safe-diagnostic artifact'
        artifacts = self.request('GET', f'/actions/runs/{run["id"]}/artifacts')['artifacts']
        matches = [a for a in artifacts if a['name'] == f'safe-diagnostic-{run["id"]}' and not a['expired']]
        require(len(matches) == 1, 'Previous run has no unique readable diagnostic artifact')
        # Obtain the signed artifact URL without forwarding the GitHub token to storage.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        url = 'https://api.github.com/repos/' + self.repo + f'/actions/artifacts/{matches[0]["id"]}/zip'
        req = urllib.request.Request(url, headers={'Authorization':'Bearer '+self.token,
            'Accept':'application/vnd.github+json'})
        try:
            response = urllib.request.build_opener(NoRedirect()).open(req, timeout=25)
        except urllib.error.HTTPError as error:
            require(error.code == 302, 'Could not obtain previous diagnostic artifact')
            location = error.headers['Location']
            error.close()
            require(location.startswith('https://'), 'Unsafe artifact redirect')
            response = urllib.request.urlopen(location, timeout=25)
        with response:
            content = response.read(1024*1024+1)
        return read_diagnostic_zip(content)

    def stop(self, success, reason):
        # Independent durable barriers: disabled workflow AND an open issue.
        errors = []
        if not hasattr(self,'stop_errors'):
            self.stop_errors=[]
        try:
            self.request('PUT', f'/actions/workflows/{WORKFLOW}/disable')
        except Exception as error:
            detail={'operation':'disable_workflow',**error_fields(error)}
            self.stop_errors.append(detail)
            report('workflow_disable_failed',**detail)
            errors.append('Could not disable workflow')
        title = SUCCESS_TITLE if success else STOP_TITLE
        try:
            if not any(i['title'] == title for i in self.issues()):
                run = f'https://github.com/{self.repo}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'
                self.request('POST', '/issues', {'title': title, 'body':
                    f'{reason}\n\nRun: {run}\n\nAutomatic Apply is stopped. '
                    'Review Oracle privately before resuming. Do not paste credentials, Terraform state or raw Oracle logs here.'})
        except Exception as error:
            detail={'operation':'create_stop_issue',**error_fields(error)}
            self.stop_errors.append(detail)
            report('stop_issue_failed',**detail)
            errors.append('Could not create persistent issue')
        require(not errors, '; '.join(errors))


class Controller:
    def __init__(self, local=False):
        self.stack_id = os.environ['OCI_STACK_ID']
        if local:
            config = oci.config.from_file()
            self.tenancy = config['tenancy']
            options = {'config': config}
        else:
            self.tenancy = os.environ['OCI_TENANCY_OCID']
            signer = oci.signer.Signer(tenancy=self.tenancy, user=os.environ['OCI_USER_OCID'],
                fingerprint=os.environ['OCI_FINGERPRINT'], private_key_file_location=None,
                private_key_content=os.environ['OCI_PRIVATE_KEY'])
            options = {'config': {'region': REGION, 'tenancy':self.tenancy,
                'user':os.environ['OCI_USER_OCID'], 'fingerprint':os.environ['OCI_FINGERPRINT'],
                'key_content':os.environ['OCI_PRIVATE_KEY']}, 'signer': signer}
        options.update(timeout=(10, 25), retry_strategy=oci.retry.NoneRetryStrategy())
        self.rm = oci.resource_manager.ResourceManagerClient(**options)
        self.identity = oci.identity.IdentityClient(**options)
        self.compute = oci.core.ComputeClient(**options)
        self.block = oci.core.BlockstorageClient(**options)

    def call(self, method, *args, **kwargs):
        self.operation = getattr(method, '__name__', type(method).__name__)
        report('oracle_request', operation=self.operation)
        started = time.monotonic()
        response = method(*args, **kwargs)
        headers=rate_limit_headers(getattr(response,'headers',{}))
        if self.operation=='create_job':
            self.submission_rate_headers=headers
        report('oracle_response', operation=self.operation, status=getattr(response,'status',None),
               elapsed_seconds=round(time.monotonic()-started,2),
               rate_limit_headers=headers)
        return response

    def all(self, method, *args, **kwargs):
        self.operation = getattr(method, '__name__', type(method).__name__)
        report('oracle_request', operation=self.operation, paginated=True)
        kwargs['retry_strategy'] = oci.retry.NoneRetryStrategy()
        response = oci.pagination.list_call_get_all_results(method, *args, **kwargs)
        report('oracle_response', operation=self.operation, count=len(response.data),
               rate_limit_headers=rate_limit_headers(getattr(response,'headers',{})))
        return response.data

    def jobs(self):
        return self.all(self.rm.list_jobs, compartment_id=self.tenancy, stack_id=self.stack_id,
                        sort_by='TIMECREATED', sort_order='DESC')

    def check_job(self, job):
        require(job.operation == 'APPLY', 'Latest job is not Apply')
        if job.lifecycle_state == 'SUCCEEDED':
            raise SafetyStop('Successful Apply has no confirmed state instance')
        require(job.lifecycle_state == 'FAILED', 'Unexpected terminal job status')
        logs = self.all(self.rm.get_job_logs, job.id)
        require(capacity_only([entry.message or '' for entry in logs]), 'Apply failure is not exclusively host capacity')

    def inspect(self):
        subscriptions = self.all(self.identity.list_region_subscriptions, self.tenancy)
        require(any(s.region_name == REGION and s.is_home_region and s.status == 'READY' for s in subscriptions),
                'Configured region is not the ready home region')
        stack = self.call(self.rm.get_stack, self.stack_id).data
        require(stack.display_name == 'aboba-instance' and stack.lifecycle_state == 'ACTIVE', 'Stack identity/status changed')
        require(stack.compartment_id == self.tenancy, 'Stack compartment changed')
        require(stack.config_source.config_source_type == 'ZIP_UPLOAD', 'Stack config source changed')
        require(not stack.config_source.working_directory, 'Stack working directory changed')
        require(stack.terraform_version == '1.5.x', 'Terraform version changed')
        require(stack.variables == {'region':REGION, 'tenancy_ocid':self.tenancy, 'compartment_ocid':self.tenancy},
                'Stack variables changed')
        content = self.call(self.rm.get_stack_tf_config, self.stack_id).data.content
        require(config_digest(content) == CONFIG_SHA256, 'Audited Terraform configuration changed')
        text = zipfile.ZipFile(io.BytesIO(content)).read('main.tf').decode()
        image_id = re.search(r'source_id\s*=\s*"([^"]+)"', text).group(1)
        expected_name = re.search(r'\n\s*display_name\s*=\s*"(instance-[^"]+)"', text).group(1)
        ad = re.search(r'availability_domain\s*=\s*"([^"]+)"', text).group(1)
        jobs = self.jobs()
        if jobs:
            selected=next((j for j in jobs if j.lifecycle_state in ACTIVE_JOBS),jobs[0])
            self.job_id=getattr(selected,'id',None)
        report('stack_jobs', total=len(jobs), active=sum(j.lifecycle_state in ACTIVE_JOBS for j in jobs),
               latest_status=jobs[0].lifecycle_state if jobs else None)
        if any(j.lifecycle_state in ACTIVE_JOBS for j in jobs):
            return 'busy'
        require(not any(j.lifecycle_state not in ACTIVE_JOBS | {'SUCCEEDED','FAILED','CANCELED'} for j in jobs),
                'Unknown job lifecycle state')
        state = json.loads(self.call(self.rm.get_stack_tf_state, self.stack_id).data.content)
        ids = state_instances(state)
        if ids:
            instance = self.call(self.compute.get_instance, ids[0]).data
            require(instance.shape == 'VM.Standard.A1.Flex' and instance.shape_config.ocpus == 1 and
                    instance.shape_config.memory_in_gbs == 6 and instance.availability_domain == ad and
                    instance.compartment_id == self.tenancy and instance.image_id == image_id, 'State instance parameters changed')
            if instance.lifecycle_state in {'PROVISIONING','STARTING','STOPPING'}:
                return 'busy'
            require(instance.lifecycle_state in {'RUNNING','STOPPED'}, 'State instance is not usable')
            require(jobs and jobs[0].operation == 'APPLY' and jobs[0].lifecycle_state == 'SUCCEEDED',
                    'Existing instance needs review after incomplete Apply')
            return 'success'
        if jobs:
            self.check_job(jobs[0])
        compartments = self.all(self.identity.list_compartments, self.tenancy,
                                compartment_id_in_subtree=True, access_level='ANY')
        cids = [self.tenancy] + [c.id for c in compartments if c.lifecycle_state == 'ACTIVE']
        instances, boots, volumes = [], [], []
        for cid in cids:
            instances.extend(self.all(self.compute.list_instances, cid))
            boots.extend(self.all(self.block.list_boot_volumes, availability_domain=ad, compartment_id=cid))
            volumes.extend(self.all(self.block.list_volumes, compartment_id=cid))
        require(not any(i.display_name == expected_name and i.lifecycle_state != 'TERMINATED' for i in instances),
                'Matching instance exists outside Terraform state; refusing duplicate')
        image = self.call(self.compute.get_image, image_id).data
        require(image.operating_system == 'Canonical Ubuntu', 'Image eligibility changed')
        boot_gb = max(50, math.ceil(image.size_in_mbs / 1024))
        check_free_budget(instances, boots, volumes, boot_gb)
        report('free_budget_checked', compartments=len(cids), instances=len(instances), boot_volumes=len(boots),
               block_volumes=len(volumes), new_boot_gb=boot_gb, ocpus=1, ram_gb=6)
        return 'ready'

    def apply(self):
        # Recheck the service immediately before submitting. RM also locks state.
        require(not any(j.lifecycle_state in ACTIVE_JOBS for j in self.jobs()), 'Job appeared before Apply')
        details = oci.resource_manager.models.CreateJobDetails(stack_id=self.stack_id,
            display_name='Always-Free capacity attempt',
            job_operation_details=oci.resource_manager.models.CreateApplyJobOperationDetails(
                execution_plan_strategy='AUTO_APPROVED', is_provider_upgrade_required=False))
        # Single submission, no automatic SDK retries on an ambiguous POST.
        token = hashlib.sha256((os.environ['GITHUB_REPOSITORY'] + ':' + os.environ['GITHUB_RUN_ID']).encode()).hexdigest()
        job = self.call(self.rm.create_job, details, opc_retry_token=token).data
        self.job_id = job.id
        report('apply_submitted', job_id=safe_text(job.id), job_reference=hashlib.sha256(job.id.encode()).hexdigest()[:12])
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            job = self.call(self.rm.get_job, job.id).data
            report('apply_status', state=job.lifecycle_state)
            if job.lifecycle_state not in ACTIVE_JOBS:
                if job.lifecycle_state == 'SUCCEEDED':
                    return self.inspect()
                self.check_job(job)
                return 'capacity'
            time.sleep(15)
        return 'busy'


def pending_cooldown(diagnostic, now=None):
    retry_at = diagnostic.get('retry_at') or diagnostic.get('next_apply_at')
    if retry_at:
        deadline = datetime.datetime.fromisoformat(retry_at)
        now = now or datetime.datetime.now(datetime.timezone.utc)
        if deadline > now:
            return {**diagnostic, 'outcome':'cooldown' if diagnostic.get('retry_at') else 'waiting',
                    'wait_reason':'oracle_429' if diagnostic.get('retry_at') else 'adaptive_interval',
                    'wait_seconds':math.ceil((deadline-now).total_seconds())}
    return None


def throttle_diagnostic(error, previous=None, now=None):
    previous = previous or {}
    count = previous.get('consecutive_throttles',0) + 1
    base=previous.get('attempt_interval_seconds',180)
    delay = min(1800, max(base*2, 180 * 2**min(count-1,4)))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    retry_after = next((str(v) for k,v in getattr(error,'headers',{}).items() if k.lower()=='retry-after'),'')
    if retry_after.isdigit():
        delay = max(delay,int(retry_after))
    elif retry_after:
        try:
            delay = max(delay, math.ceil((parsedate_to_datetime(retry_after)-now).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            pass
    return {'outcome':'throttled', **error_fields(error), 'consecutive_throttles':count,
            'attempt_interval_seconds':delay,'successful_attempts_at_interval':0,
            'backoff_seconds':delay, 'retry_at':(now+datetime.timedelta(seconds=delay)).isoformat()}


def adaptive_interval(outcome, previous=None, now=None):
    previous=previous or {}
    interval=previous.get('attempt_interval_seconds',previous.get('backoff_seconds',180))
    stable=previous.get('successful_attempts_at_interval',0)
    result={}
    if outcome=='capacity':
        stable+=1
        if stable>=3:
            interval=max(180,int(interval*0.9))
            stable=0
        now=now or datetime.datetime.now(datetime.timezone.utc)
        result['next_apply_at']=(now+datetime.timedelta(seconds=interval)).isoformat()
    result.update(attempt_interval_seconds=interval,successful_attempts_at_interval=stable)
    return result


def run_context(args):
    repo=os.environ.get('GITHUB_REPOSITORY')
    run_id=os.environ.get('GITHUB_RUN_ID')
    sha=os.environ.get('GITHUB_SHA')
    if not sha:
        try:
            result=subprocess.run(['git','rev-parse','HEAD'],cwd=Path(__file__).parent,
                                  capture_output=True,text=True,timeout=3)
            sha=result.stdout.strip() if result.returncode==0 else None
        except (OSError,subprocess.TimeoutExpired):
            sha=None
    return {'schema_version':1,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'run_id':run_id,'run_attempt':os.environ.get('GITHUB_RUN_ATTEMPT'),
            'run_url':f'https://github.com/{repo}/actions/runs/{run_id}' if repo and run_id else None,
            'commit_sha':sha,'python_version':platform.python_version(),'oci_sdk_version':oci.__version__,
            'trigger':os.environ.get('GITHUB_EVENT_NAME','local'),
            'mode':'local-dry-run' if args.local else 'cloud-dry-run' if args.dry_run else 'cloud-apply',
            'region':REGION,'stack_name':'aboba-instance'}


def oracle_job_url(job_id):
    if isinstance(job_id,str) and re.fullmatch(r'ocid1\.ormjob\.oc1\.ap-singapore-2\.[A-Za-z0-9_.-]+',job_id):
        return f'https://cloud.oracle.com/resourcemanager/jobs/{job_id}?region={REGION}'
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--local', action='store_true')
    args = parser.parse_args()
    context=run_context(args)
    started=time.monotonic()
    require(not args.local or args.dry_run, 'Local mode must be read-only')
    gh = None
    controller = None
    diagnostic = {}
    report('attempt_started',**context)
    try:
        if not args.local:
            gh = GitHub()
            cooldown = gh.guard(read_only=args.dry_run)
            if cooldown and not args.dry_run:
                diagnostic = cooldown
                report('attempt_waiting', **diagnostic)
                return 0
        controller = Controller(args.local)
        outcome = controller.inspect()
        if outcome == 'ready' and not args.dry_run:
            outcome = controller.apply()
        diagnostic = {'outcome': outcome, 'dry_run':args.dry_run}
        submission_headers=getattr(controller,'submission_rate_headers',None)
        if isinstance(submission_headers,dict):
            diagnostic['rate_limit_headers']=submission_headers
        if not args.dry_run:
            diagnostic.update(adaptive_interval(outcome,getattr(gh,'previous_diagnostic',{})))
        report('attempt_result', **diagnostic)
        if outcome == 'success' and gh and not args.dry_run:
            gh.stop(True, 'Existing Stack successfully created the audited Always Free VPS (1 OCPU / 6 GB).')
    except Exception as error:
        diagnostic = {'outcome':'halted', **error_fields(error),
            'operation':getattr(error,'operation_name',None) or getattr(controller,'operation',None) or getattr(gh,'operation',None)}
        if isinstance(error, oci.exceptions.ServiceError) and error.status == 429:
            diagnostic = {**diagnostic, **throttle_diagnostic(error,getattr(gh,'previous_diagnostic',{}))}
            report('oracle_rate_limited', **diagnostic)
            return 1 if args.dry_run else 0
        report('attempt_halted', **diagnostic)
        if gh and not args.dry_run:
            try:
                gh.stop(False, json.dumps(diagnostic))
            except Exception as stop_error:
                report('persist_stop_failed', **error_fields(stop_error))
        return 1
    finally:
        previous_context=diagnostic.get('context',{}) if diagnostic.get('outcome') in {'cooldown','waiting'} else {}
        context.update(finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       duration_seconds=round(time.monotonic()-started,2),
                       oracle_job_url=oracle_job_url(getattr(controller,'job_id',None)) or previous_context.get('oracle_job_url'),
                       previous_run_url=previous_context.get('run_url'))
        diagnostic['context']=context
        stop_errors=getattr(gh,'stop_errors',None)
        if isinstance(stop_errors,list) and stop_errors:
            diagnostic['stop_errors']=stop_errors
        report('attempt_finished',outcome=diagnostic.get('outcome'),**context)
        Path('diagnostic.json').write_text(json.dumps(diagnostic, indent=2))
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
                summary.write('```json\n' + json.dumps(diagnostic, indent=2) + '\n```\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
