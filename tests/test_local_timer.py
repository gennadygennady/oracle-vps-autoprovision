import datetime
import unittest
from local_timer import decision


class TimerTests(unittest.TestCase):
    def test_dispatch_only_when_active_and_overdue(self):
        now=datetime.datetime(2026,10,12,15,50,tzinfo=datetime.timezone.utc)
        old={'status':'completed','conclusion':'success','created_at':'2026-10-12T15:40:00Z'}
        recent=dict(old,created_at='2026-10-12T15:48:00Z')
        self.assertEqual(decision({'state':'active'},[old],now),'dispatch')
        self.assertEqual(decision({'state':'disabled_manually'},[old],now),'disabled')
        self.assertEqual(decision({'state':'active'},[recent],now),'recent')
        self.assertEqual(decision({'state':'active'},[dict(old,status='queued')],now),'busy')
        self.assertEqual(decision({'state':'active'},[dict(old,conclusion='failure')],now),'review')
        self.assertEqual(decision({'state':'active'},[recent,dict(old,status='in_progress')],now),'busy')


if __name__ == '__main__':
    unittest.main()
