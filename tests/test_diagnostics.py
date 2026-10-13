import datetime
import io
import json
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import zipfile
import urllib.error
import oci

from diagnostics import safe_text, rate_limit_headers
from local_timer import GitHubAPIError, describe, latest_outcome, current_outcome_details
from provision import GitHub, main, pending_cooldown, throttle_diagnostic, SafetyStop, oracle_job_url, adaptive_interval


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        # main() writes an Actions summary in production. Test scenarios must
        # never append their synthetic failures to the real runner summary.
        summary=patch.dict(os.environ,{'GITHUB_STEP_SUMMARY':''})
        summary.start()
        self.addCleanup(summary.stop)

    def test_redaction_preserves_useful_error(self):
        token='ghp_'+'a'*36
        text='HTTP 403 '+token+' Authorization: Bearer '+token+' https://host/file?signature=secret'
        cleaned=safe_text(text)
        self.assertIn('HTTP 403',cleaned)
        self.assertNotIn(token,cleaned)
        self.assertNotIn('signature=secret',cleaned)
        key='-----BEGIN '+'PRIVATE KEY-----\nsecret-body\n-----END '+'PRIVATE KEY-----'
        self.assertNotIn('secret-body',safe_text(key))
        with patch.dict(os.environ, {'OCI_PRIVATE_KEY':'injected-secret'}):
            self.assertNotIn('injected-secret',safe_text('message injected-secret'))

    def test_cli_error_contains_operation_status_and_reason(self):
        error=GitHubAPIError('GET actions/workflows/provision.yml',Mock(returncode=1,stderr='gh: Bad credentials (HTTP 401)',stdout=''))
        self.assertEqual(error.status,401)
        self.assertIn('Bad credentials',str(error))
        self.assertEqual(error.operation_name,'GET actions/workflows/provision.yml')

    def test_rate_limit_backoff_is_durable_bounded_and_honors_retry_after(self):
        now=datetime.datetime(2026,10,13,tzinfo=datetime.timezone.utc)
        error=oci.exceptions.ServiceError(429,'TooManyRequests',{},'Too many requests',operation_name='create_job')
        previous={}
        for expected in [360,720,1440,1800,1800,1800]:
            previous=throttle_diagnostic(error,previous,now)
            self.assertEqual(previous['backoff_seconds'],expected)
        pending=pending_cooldown(previous,now+datetime.timedelta(seconds=10))
        self.assertEqual(pending['outcome'],'cooldown')
        self.assertEqual(pending['consecutive_throttles'],6)
        self.assertIsNone(pending_cooldown(previous,now+datetime.timedelta(seconds=1800)))
        error.headers={'retry-after':'2000'}
        self.assertEqual(throttle_diagnostic(error,{},now)['backoff_seconds'],2000)

    def test_429_does_not_disable_workflow_but_403_does(self):
        for status,expected,disable in [(429,0,False),(403,1,True)]:
            error=oci.exceptions.ServiceError(status,'TooManyRequests' if status==429 else 'NotAuthorized',{},'Safe API failure',operation_name='get_stack')
            gh=Mock(previous_diagnostic={})
            gh.guard.return_value=None
            controller=Mock()
            controller.inspect.side_effect=error
            with tempfile.TemporaryDirectory() as directory:
                original=os.getcwd()
                try:
                    os.chdir(directory)
                    with patch('sys.argv',['provision.py']),patch('provision.GitHub',return_value=gh), \
                         patch('provision.Controller',return_value=controller),patch('provision.report'):
                        self.assertEqual(main(),expected)
                    diagnostic=json.loads(Path('diagnostic.json').read_text())
                    self.assertEqual(diagnostic['operation'],'get_stack')
                    self.assertEqual(diagnostic['status'],status)
                    self.assertEqual(gh.stop.called,disable)
                    controller.apply.assert_not_called()
                    context=diagnostic['context']
                    self.assertEqual(context['mode'],'cloud-apply')
                    self.assertEqual(context['oci_sdk_version'],oci.__version__)
                    self.assertIn('python_version',context)
                    self.assertGreaterEqual(context['duration_seconds'],0)
                    self.assertGreaterEqual(context['finished_at'],context['started_at'])
                finally:
                    os.chdir(original)

    def test_cooldown_does_not_construct_oracle_clients(self):
        gh=Mock()
        gh.guard.return_value={'outcome':'cooldown','wait_seconds':120,'retry_at':'2026-10-13T12:00:00+00:00'}
        with tempfile.TemporaryDirectory() as directory:
            original=os.getcwd()
            try:
                os.chdir(directory)
                with patch('sys.argv',['provision.py']),patch('provision.GitHub',return_value=gh), \
                     patch('provision.Controller') as client,patch('provision.report'):
                    self.assertEqual(main(),0)
                    client.assert_not_called()
            finally:
                os.chdir(original)

    def test_artifact_download_does_not_forward_auth_to_storage(self):
        gh=GitHub.__new__(GitHub)
        gh.repo='owner/repo'
        gh.token='private-github-token'
        gh.request=Mock(return_value={'artifacts':[{'name':'safe-diagnostic-42','expired':False,'id':7}]})
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            archive.writestr('diagnostic.json',json.dumps({'outcome':'throttled','backoff_seconds':180}))
        opener=Mock()
        opener.open.side_effect=urllib.error.HTTPError('https://api.github.com/artifact',302,'Found',{'Location':'https://storage.example/file?sig=secret'},None)
        with patch('urllib.request.build_opener',return_value=opener), \
             patch('urllib.request.urlopen',return_value=io.BytesIO(buffer.getvalue())) as storage:
            self.assertEqual(gh.read_diagnostic({'id':42})['outcome'],'throttled')
            storage.assert_called_once_with('https://storage.example/file?sig=secret',timeout=25)

    def test_skip_log_explains_wait_and_has_run_link(self):
        now=datetime.datetime(2026,10,13,10,tzinfo=datetime.timezone.utc)
        run={'status':'completed','conclusion':'success','created_at':'2026-10-13T09:59:00Z','html_url':'https://github.com/owner/repo/actions/runs/42','event':'schedule'}
        result=describe('recent',{'state':'active'},[run],now)
        self.assertEqual(result['wait_seconds'],120)
        self.assertEqual(result['latest_run_age_seconds'],60)
        self.assertEqual(result['latest_run_url'],run['html_url'])

    def test_stop_keeps_both_underlying_failures(self):
        gh=GitHub.__new__(GitHub)
        gh.repo='owner/repo'
        first=RuntimeError('disable rejected: insufficient permission')
        second=RuntimeError('issue rejected: GitHub temporarily unavailable')
        gh.request=Mock(side_effect=[first,second])
        gh.issues=Mock(return_value=iter([]))
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'42'}),patch('provision.report') as logged:
            with self.assertRaises(SafetyStop):
                gh.stop(False,'safe reason')
        self.assertEqual([e['operation'] for e in gh.stop_errors],['disable_workflow','create_stop_issue'])
        self.assertIn('insufficient permission',gh.stop_errors[0]['message'])
        self.assertIn('temporarily unavailable',gh.stop_errors[1]['message'])
        self.assertEqual(logged.call_count,2)

    def test_github_http_error_keeps_response_reason_and_request_id(self):
        gh=GitHub.__new__(GitHub)
        gh.repo='owner/repo'
        gh.token='private-token'
        error=urllib.error.HTTPError('https://api.github.com/test',403,'Forbidden',
            {'X-GitHub-Request-Id':'request-42'},io.BytesIO(b'{"message":"Resource not accessible by integration"}'))
        with patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                gh.request('PUT','/actions/workflows/provision.yml/disable')
        self.assertEqual(raised.exception.status,403)
        self.assertEqual(raised.exception.request_id,'request-42')
        self.assertIn('Resource not accessible',raised.exception.message)

    def test_local_result_reads_actual_outcome_once_per_run(self):
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            archive.writestr('diagnostic.json',json.dumps({'outcome':'throttled','retry_at':'2026-10-13T12:00:00+00:00',
                'backoff_seconds':360,'context':{'mode':'cloud-apply'}}))
        run={'id':42,'status':'completed','conclusion':'success','html_url':'https://github.com/owner/repo/actions/runs/42'}
        artifacts={'artifacts':[{'id':7,'name':'safe-diagnostic-42','expired':False}]}
        with tempfile.TemporaryDirectory() as directory,patch('local_timer.RUNTIME',Path(directory)), \
             patch('local_timer.api',side_effect=[artifacts,buffer.getvalue()]) as request:
            first=latest_outcome('gh',[run])
            second=latest_outcome('gh',[run])
        self.assertEqual(first,second)
        self.assertEqual(first['last_oracle_outcome'],'throttled')
        self.assertEqual(first['oracle_backoff_seconds'],360)
        self.assertEqual(first['diagnostic_mode'],'cloud-apply')
        self.assertEqual(request.call_count,2)

    def test_oracle_job_link_survives_redaction_but_credentials_do_not(self):
        job_id='.'.join(['ocid1','ormjob','oc1','ap-singapore-2','a'*30])
        url=oracle_job_url(job_id)
        self.assertEqual(safe_text(url),url)
        self.assertIsNone(oracle_job_url('not-a-job'))
        tenancy='.'.join(['ocid1','tenancy','oc1','','a'*40])
        self.assertNotIn(tenancy,safe_text(tenancy))

    def test_cached_wait_is_recomputed_not_reported_as_current(self):
        cached={'last_oracle_outcome':'cooldown','oracle_wait_seconds':20,
                'oracle_retry_at':'2026-10-13T10:00:00+00:00'}
        now=datetime.datetime(2026,10,13,10,1,tzinfo=datetime.timezone.utc)
        result=current_outcome_details(cached,now)
        self.assertEqual(result['oracle_wait_seconds_at_run'],20)
        self.assertEqual(result['oracle_wait_seconds'],0)
        self.assertFalse(result['oracle_cooldown_active'])
        self.assertEqual(cached['oracle_wait_seconds'],20)

    def test_header_allowlist_excludes_auth_and_cookies(self):
        headers={'Retry-After':'60','X-RateLimit-Remaining':'0','RateLimit-Reset':'100',
                 'RequestLimit-Write-Remaining':'0','Authorization':'secret','Set-Cookie':'secret'}
        result=rate_limit_headers(headers)
        self.assertEqual(result['retry-after'],'60')
        self.assertEqual(result['x-ratelimit-remaining'],'0')
        self.assertNotIn('authorization',result)
        self.assertNotIn('set-cookie',result)

    def test_adaptive_interval_survives_success_and_eases_only_after_three_attempts(self):
        now=datetime.datetime(2026,10,13,tzinfo=datetime.timezone.utc)
        error=oci.exceptions.ServiceError(429,'TooManyRequests',{},'Too many requests')
        previous=throttle_diagnostic(error,{},now)
        self.assertEqual(previous['attempt_interval_seconds'],360)
        for expected in [360,360,324]:
            previous=adaptive_interval('capacity',previous,now)
            self.assertEqual(previous['attempt_interval_seconds'],expected)
        self.assertEqual(previous['successful_attempts_at_interval'],0)
        waiting=pending_cooldown(previous,now+datetime.timedelta(seconds=10))
        self.assertEqual(waiting['outcome'],'waiting')
        self.assertEqual(waiting['wait_reason'],'adaptive_interval')
        self.assertIsNone(pending_cooldown(previous,now+datetime.timedelta(seconds=324)))
        again=throttle_diagnostic(error,previous,now)
        self.assertEqual(again['attempt_interval_seconds'],648)

    def test_adaptive_interval_keeps_busy_state_and_has_floor(self):
        result=adaptive_interval('busy',{'attempt_interval_seconds':720,'successful_attempts_at_interval':2})
        self.assertEqual(result['attempt_interval_seconds'],720)
        self.assertEqual(result['successful_attempts_at_interval'],2)
        self.assertNotIn('next_apply_at',result)
        result=adaptive_interval('capacity',{'attempt_interval_seconds':180,'successful_attempts_at_interval':2})
        self.assertEqual(result['attempt_interval_seconds'],180)

    def test_waiting_does_not_construct_oracle_clients(self):
        gh=Mock()
        gh.guard.return_value={'outcome':'waiting','wait_seconds':120,'next_apply_at':'2026-10-13T12:00:00+00:00'}
        with tempfile.TemporaryDirectory() as directory:
            original=os.getcwd()
            try:
                os.chdir(directory)
                with patch('sys.argv',['provision.py']),patch('provision.GitHub',return_value=gh), \
                     patch('provision.Controller') as client,patch('provision.report'):
                    self.assertEqual(main(),0)
                    client.assert_not_called()
            finally:
                os.chdir(original)


if __name__=='__main__':
    unittest.main()
