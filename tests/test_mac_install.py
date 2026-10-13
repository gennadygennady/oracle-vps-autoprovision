from pathlib import Path
import plistlib
import tempfile
import unittest

from macos.manage import REPO, LABEL, check_layout, layout, validate_existing, write_layout


class MacInstallTests(unittest.TestCase):
    def fixture(self, directory):
        root=Path(directory)
        repo=root/'repo with spaces & chars'
        home=root/'home'
        (repo/'macos').mkdir(parents=True)
        (repo/'macos/launchagent.plist.template').write_bytes((REPO/'macos/launchagent.plist.template').read_bytes())
        (repo/'local_timer.py').write_text('INTERVAL = 180\n')
        (repo/'diagnostics.py').write_text('# diagnostics source\n')
        return repo,home

    def test_migration_is_idempotent_and_preserves_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            repo,home=self.fixture(directory)
            runtime,agent,links=layout(repo,home)
            runtime.mkdir(parents=True)
            agent.parent.mkdir(parents=True)
            for name in ['local_timer.py','diagnostics.py']:
                (runtime/name).write_bytes((repo/name).read_bytes())
            agent.write_bytes(plistlib.dumps({'Label':LABEL}))
            log=runtime/'timer.log'
            log.write_text('existing log history\n')
            cache=runtime/'last_outcome.json'
            cache.write_text('{"last_oracle_outcome":"capacity"}')
            validate_existing(repo,home)
            write_layout(repo,home,'/usr/bin/python3','/usr/bin/gh')
            self.assertEqual(check_layout(repo,home),[])
            self.assertFalse(agent.is_symlink())
            self.assertFalse((repo/'.local').exists())
            for destination,target in links.items():
                self.assertTrue(destination.is_symlink())
                self.assertEqual(destination.resolve(),target.resolve())
            self.assertEqual(log.read_text(),'existing log history\n')
            self.assertEqual(cache.read_text(),'{"last_oracle_outcome":"capacity"}')
            validate_existing(repo,home)
            write_layout(repo,home,'/usr/bin/python3','/usr/bin/gh')
            self.assertEqual(check_layout(repo,home),[])
            self.assertEqual(log.read_text(),'existing log history\n')

    def test_wrong_links_and_changed_copies_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            repo,home=self.fixture(directory)
            runtime,_,_=layout(repo,home)
            runtime.mkdir(parents=True)
            destination=runtime/'local_timer.py'
            destination.write_text('locally changed source')
            with self.assertRaisesRegex(ValueError,'differs'):
                validate_existing(repo,home)
            self.assertEqual(destination.read_text(),'locally changed source')
            destination.unlink()
            other=Path(directory)/'other.py'
            other.write_text('unrelated')
            destination.symlink_to(other)
            with self.assertRaisesRegex(ValueError,'unrelated symlink'):
                validate_existing(repo,home)

    def test_checker_detects_missing_link_and_wrong_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            repo,home=self.fixture(directory)
            self.assertTrue(check_layout(repo,home))
            write_layout(repo,home,'/usr/bin/python3','/usr/bin/gh')
            runtime,agent,_=layout(repo,home)
            (runtime/'diagnostics.py').unlink()
            self.assertTrue(any('symlink' in e for e in check_layout(repo,home)))
            write_layout(repo,home,'/usr/bin/python3','/usr/bin/gh')
            data=plistlib.loads(agent.read_bytes())
            data['StartInterval']=300
            agent.write_bytes(plistlib.dumps(data))
            self.assertTrue(any('StartInterval' in e for e in check_layout(repo,home)))

    def test_legacy_plist_symlink_is_migrated_and_local_directory_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo,home=self.fixture(directory)
            runtime,agent,_=layout(repo,home)
            legacy=repo/'.local'/f'{LABEL}.plist'
            legacy.parent.mkdir()
            legacy.write_bytes(plistlib.dumps({'Label':LABEL}))
            agent.parent.mkdir(parents=True)
            agent.symlink_to(legacy)
            self.assertTrue(any('regular file' in e for e in check_layout(repo,home)))
            validate_existing(repo,home)
            write_layout(repo,home,'/usr/bin/python3','/usr/bin/gh')
            self.assertEqual(check_layout(repo,home),[])
            self.assertFalse(agent.is_symlink())
            self.assertFalse(legacy.parent.exists())
            validate_existing(repo,home)
            write_layout(repo,home,'/usr/bin/python3','/usr/bin/gh')
            self.assertEqual(check_layout(repo,home),[])


if __name__=='__main__':
    unittest.main()
