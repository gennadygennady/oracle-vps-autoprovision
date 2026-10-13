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

from diagnostics import safe_text
from local_timer import GitHubAPIError, describe
from provision import GitHub, main, pending_cooldown, throttle_diagnostic


class DiagnosticsTests(unittest.TestCase):
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
        for expected in [180,360,720,1440,1800,1800]:
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


if __name__=='__main__':
    unittest.main()
