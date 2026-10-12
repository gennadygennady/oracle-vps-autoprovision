"""Fail-closed controller for one pre-existing, audited Oracle RM stack."""
import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.request
import zipfile

import oci

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
        req = urllib.request.Request('https://api.github.com/repos/' + self.repo + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'}, method=method)
        with urllib.request.urlopen(req, timeout=25) as response:
            content = response.read()
            return json.loads(content) if content else None

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
            return
        require(not any(i['title'] == STOP_TITLE for i in issues), 'Persistent safety stop issue is open')
        require(not any(i['title'] == SUCCESS_TITLE for i in issues), 'Persistent success latch is open')
        runs = self.request('GET', f'/actions/workflows/{WORKFLOW}/runs?status=completed&per_page=1')['workflow_runs']
        require(not runs or runs[0]['conclusion'] == 'success', 'Previous workflow did not finish successfully')

    def stop(self, success, reason):
        # Independent durable barriers: disabled workflow AND an open issue.
        errors = []
        try:
            self.request('PUT', f'/actions/workflows/{WORKFLOW}/disable')
        except Exception:
            errors.append('Could not disable workflow')
        title = SUCCESS_TITLE if success else STOP_TITLE
        try:
            if not any(i['title'] == title for i in self.issues()):
                run = f'https://github.com/{self.repo}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'
                self.request('POST', '/issues', {'title': title, 'body':
                    f'{reason}\n\nRun: {run}\n\nAutomatic Apply is stopped. '
                    'Review Oracle privately before resuming. Do not paste credentials, Terraform state or raw Oracle logs here.'})
        except Exception:
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

    @staticmethod
    def all(method, *args, **kwargs):
        kwargs['retry_strategy'] = oci.retry.NoneRetryStrategy()
        return oci.pagination.list_call_get_all_results(method, *args, **kwargs).data

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
        stack = self.rm.get_stack(self.stack_id).data
        require(stack.display_name == 'aboba-instance' and stack.lifecycle_state == 'ACTIVE', 'Stack identity/status changed')
        require(stack.compartment_id == self.tenancy, 'Stack compartment changed')
        require(stack.config_source.config_source_type == 'ZIP_UPLOAD', 'Stack config source changed')
        require(not stack.config_source.working_directory, 'Stack working directory changed')
        require(stack.terraform_version == '1.5.x', 'Terraform version changed')
        require(stack.variables == {'region':REGION, 'tenancy_ocid':self.tenancy, 'compartment_ocid':self.tenancy},
                'Stack variables changed')
        content = self.rm.get_stack_tf_config(self.stack_id).data.content
        require(config_digest(content) == CONFIG_SHA256, 'Audited Terraform configuration changed')
        text = zipfile.ZipFile(io.BytesIO(content)).read('main.tf').decode()
        image_id = re.search(r'source_id\s*=\s*"([^"]+)"', text).group(1)
        expected_name = re.search(r'\n\s*display_name\s*=\s*"(instance-[^"]+)"', text).group(1)
        ad = re.search(r'availability_domain\s*=\s*"([^"]+)"', text).group(1)
        jobs = self.jobs()
        if any(j.lifecycle_state in ACTIVE_JOBS for j in jobs):
            return 'busy'
        require(not any(j.lifecycle_state not in ACTIVE_JOBS | {'SUCCEEDED','FAILED','CANCELED'} for j in jobs),
                'Unknown job lifecycle state')
        state = json.loads(self.rm.get_stack_tf_state(self.stack_id).data.content)
        ids = state_instances(state)
        if ids:
            instance = self.compute.get_instance(ids[0]).data
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
        image = self.compute.get_image(image_id).data
        require(image.operating_system == 'Canonical Ubuntu', 'Image eligibility changed')
        boot_gb = max(50, math.ceil(image.size_in_mbs / 1024))
        check_free_budget(instances, boots, volumes, boot_gb)
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
        job = self.rm.create_job(details, opc_retry_token=token).data
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            job = self.rm.get_job(job.id).data
            if job.lifecycle_state not in ACTIVE_JOBS:
                if job.lifecycle_state == 'SUCCEEDED':
                    return self.inspect()
                self.check_job(job)
                return 'capacity'
            time.sleep(15)
        return 'busy'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--local', action='store_true')
    args = parser.parse_args()
    require(not args.local or args.dry_run, 'Local mode must be read-only')
    gh = None
    diagnostic = {}
    try:
        if not args.local:
            gh = GitHub()
            gh.guard(read_only=args.dry_run)
        controller = Controller(args.local)
        outcome = controller.inspect()
        if outcome == 'ready' and not args.dry_run:
            outcome = controller.apply()
        diagnostic = {'outcome': outcome, 'dry_run':args.dry_run}
        print(json.dumps(diagnostic))
        if outcome == 'success' and gh and not args.dry_run:
            gh.stop(True, 'Existing Stack successfully created the audited Always Free VPS (1 OCPU / 6 GB).')
    except Exception as error:
        # Never print exception bodies, raw Terraform logs, state or credentials.
        diagnostic = {'outcome':'halted', 'error_type':type(error).__name__}
        if isinstance(error, SafetyStop):
            diagnostic['reason'] = str(error)
        if isinstance(error, oci.exceptions.ServiceError):
            diagnostic.update(status=error.status, code=error.code, request_id=error.request_id)
        print(json.dumps(diagnostic))
        if gh and not args.dry_run:
            try:
                gh.stop(False, json.dumps(diagnostic))
            except Exception:
                print('Could not fully persist the stop. Previous failed run remains an additional Apply barrier.')
        return 1
    finally:
        Path('diagnostic.json').write_text(json.dumps(diagnostic, indent=2))
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
                summary.write('```json\n' + json.dumps(diagnostic, indent=2) + '\n```\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
