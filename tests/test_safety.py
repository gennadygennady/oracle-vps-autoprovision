import io
import json
import unittest
from types import SimpleNamespace as N
from unittest.mock import Mock, patch
import zipfile
import oci

from provision import (Controller, GitHub, SafetyStop, capacity_only, check_free_budget,
                       config_digest, state_instances)


class SafetyTests(unittest.TestCase):
    def test_github_credentials_produce_valid_sdk_config(self):
        def oid(kind):
            return '.'.join(['ocid1',kind,'oc1','','a'*40])
        env={'OCI_STACK_ID':'stack','OCI_TENANCY_OCID':oid('tenancy'),'OCI_USER_OCID':oid('user'),
             'OCI_FINGERPRINT':':'.join(['ab']*16),'OCI_PRIVATE_KEY':'in-memory-key'}
        def client(**kwargs):
            oci.config.validate_config(kwargs['config'])
            self.assertEqual(kwargs['config']['key_content'],'in-memory-key')
            self.assertNotIn('key_file',kwargs['config'])
            return Mock()
        with patch.dict('os.environ',env), patch('oci.signer.Signer'), \
             patch('oci.resource_manager.ResourceManagerClient',side_effect=client), \
             patch('oci.identity.IdentityClient',side_effect=client), \
             patch('oci.core.ComputeClient',side_effect=client), \
             patch('oci.core.BlockstorageClient',side_effect=client):
            Controller(local=False)

    def test_only_explicit_capacity_error_is_retryable(self):
        self.assertTrue(capacity_only(['Error: 500-InternalError, Out of host capacity.']))
        self.assertTrue(capacity_only(['\x1b[31m│ Error: 500-InternalError, Out of host capacity.\x1b[0m']))
        for messages in [[], ['500-InternalError'], ['Error: NotAuthorized'],
                         ['Out of host capacity'], ['Error: Out of host capacity', 'Error: InvalidParameter']]:
            self.assertFalse(capacity_only(messages))

    def test_state_preserves_existing_instance(self):
        state = {'resources':[{'mode':'managed','type':'oci_core_instance','instances':[{'attributes':{'id':'existing'}}]}]}
        self.assertEqual(state_instances(state), ['existing'])
        self.assertEqual(state_instances({'resources':[]}), [])

    def test_deposed_and_duplicate_state_stop(self):
        for instances in [[{'attributes':{'id':'a'},'deposed':'old'}],
                          [{'attributes':{'id':'a'}},{'attributes':{'id':'b'}}], [{'attributes':{}}]]:
            with self.assertRaises(SafetyStop):
                state_instances({'resources':[{'mode':'managed','type':'oci_core_instance','instances':instances}]})

    def test_unexpected_managed_resource_stops(self):
        with self.assertRaises(SafetyStop):
            state_instances({'resources':[{'mode':'managed','type':'oci_core_vcn','instances':[]}]})

    def test_free_budget_counts_all_disks_and_stopped_instances(self):
        instance = N(lifecycle_state='STOPPED', shape='VM.Standard.A1.Flex',
                     shape_config=N(ocpus=1, memory_in_gbs=6))
        disk = N(lifecycle_state='AVAILABLE', size_in_gbs=150)
        check_free_budget([instance], [disk], [], 50)
        with self.assertRaises(SafetyStop):
            check_free_budget([instance, instance], [], [], 50)
        with self.assertRaises(SafetyStop):
            check_free_budget([], [disk], [N(lifecycle_state='AVAILABLE',size_in_gbs=1)], 50)
        with self.assertRaises(SafetyStop):
            check_free_budget([N(lifecycle_state='RUNNING',shape='VM.Standard.A1.Flex',shape_config=N(ocpus=1,memory_in_gbs=7))], [], [], 50)

    def test_configuration_hash_detects_edits_not_zip_compression(self):
        def archive(text, compression):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', compression=compression) as z:
                z.writestr('main.tf',text)
            return buffer.getvalue()
        self.assertEqual(config_digest(archive('original',0)), config_digest(archive('original',8)))
        self.assertNotEqual(config_digest(archive('original',0)), config_digest(archive('changed',0)))

    def controller(self, state=None, job=None):
        c = Controller.__new__(Controller)
        c.stack_id = 'stack'
        c.tenancy = 'tenancy'
        c.identity, c.rm, c.compute, c.block = Mock(), Mock(), Mock(), Mock()
        c.identity.list_region_subscriptions.return_value = N(data=[N(region_name='ap-singapore-2',is_home_region=True,status='READY')],headers={})
        stack = N(display_name='aboba-instance',lifecycle_state='ACTIVE',compartment_id='tenancy',
                  config_source=N(config_source_type='ZIP_UPLOAD',working_directory=None),terraform_version='1.5.x',
                  variables={'region':'ap-singapore-2','tenancy_ocid':'tenancy','compartment_ocid':'tenancy'})
        c.rm.get_stack.return_value = N(data=stack)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as z:
            z.writestr('main.tf','source_id="image"\n display_name="instance-test"\navailability_domain="AD"')
        c.rm.get_stack_tf_config.return_value = N(data=N(content=buffer.getvalue()))
        c.rm.get_stack_tf_state.return_value = N(data=N(content=json.dumps(state or {'resources':[]})))
        c.jobs = Mock(return_value=[job] if job else [])
        # No real cloud calls in these decision tests.
        c.all = lambda method,*args,**kwargs: method(*args,**kwargs).data
        return c

    @patch('provision.config_digest',return_value='5266612964d57c18270e5cc78917639a487de10b33b36ae61f7a189591614733')
    def test_running_job_skips_without_apply_or_state_fetch(self, _):
        c = self.controller(job=N(lifecycle_state='IN_PROGRESS'))
        self.assertEqual(c.inspect(),'busy')
        c.rm.create_job.assert_not_called()
        c.rm.get_stack_tf_state.assert_not_called()

    @patch('provision.config_digest',return_value='5266612964d57c18270e5cc78917639a487de10b33b36ae61f7a189591614733')
    def test_existing_success_never_applies(self, _):
        state={'resources':[{'mode':'managed','type':'oci_core_instance','instances':[{'attributes':{'id':'existing'}}]}]}
        c=self.controller(state,N(operation='APPLY',lifecycle_state='SUCCEEDED'))
        c.compute.get_instance.return_value=N(data=N(shape='VM.Standard.A1.Flex',shape_config=N(ocpus=1,memory_in_gbs=6),
            availability_domain='AD',compartment_id='tenancy',image_id='image',lifecycle_state='RUNNING'))
        self.assertEqual(c.inspect(),'success')
        c.rm.create_job.assert_not_called()

    def test_parallel_job_race_blocks_submission(self):
        c=self.controller(job=N(lifecycle_state='ACCEPTED'))
        with self.assertRaises(SafetyStop):
            c.apply()
        c.rm.create_job.assert_not_called()

    def test_persistent_issue_and_failed_run_block(self):
        gh=GitHub.__new__(GitHub)
        gh.issues=Mock(return_value=iter([{'title':'[autoprovision] Safety stop'}]))
        with self.assertRaises(SafetyStop):
            gh.guard()
        gh.issues=Mock(return_value=iter([]))
        gh.request=Mock(return_value={'workflow_runs':[{'conclusion':'failure'}]})
        with self.assertRaises(SafetyStop):
            gh.guard()


if __name__ == '__main__':
    unittest.main()
