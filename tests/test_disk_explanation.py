"""Consequence and shortage calculations use current samples, not old messages."""
import unittest
from tests import test_disk_diagnostics as fixtures
from webapp.disk_budget import DiskBudget,GIB,MIB
from webapp.disk_diagnostics import explain


class DiskExplanationTests(unittest.TestCase):
    # Reuse the isolated HTTP/database fixture without inheriting its test suite.
    setUp=fixtures.DiskNoticeTests.setUp
    snapshot=fixtures.DiskNoticeTests.snapshot
    job_task=fixtures.DiskNoticeTests.job_task

    def budget(self,**config):
        return DiskBudget(self.store.directory,{'disk_min_free_gb':0,'disk_resume_free_gb':0,**config})

    def require(self,budget,needed,category='test'):
        task=self.job_task()
        with budget.connect() as db:
            db.execute('INSERT INTO requirements VALUES (?,?,?,?,?)',(task['id'],budget._version(),needed,0,0))
            budget._set(db,'pressure',dict(task_id=task['id'],category=category,kind='budget',
                requested_bytes=999*GIB,allocatable_bytes=0,reason='Old snapshot must not be shown'))
        return task

    def test_free_disk_does_not_hide_category_quota_shortage(self):
        budget=self.budget(disk_import_budget_mb=1)
        self.require(budget,2*MIB,'imports')
        snapshot=self.snapshot(budget,free=60*GIB)
        decision=explain(budget,snapshot)['download']
        self.assertEqual(decision['impact'],'some_tasks_waiting')
        self.assertEqual(decision['shortfall_bytes'],MIB)
        self.assertIn('导入与上传',decision['reason'])
        self.assertIn('60.0 GiB',decision['reason'])
        self.assertNotIn('999',decision['shortfall_text'])
        self.assertEqual(decision['request_bytes'],2*MIB)

    def test_low_space_recovers_at_high_watermark_and_keeps_manual_pause(self):
        budget=self.budget(disk_min_free_gb=10,disk_resume_free_gb=15)
        snapshot={**self.snapshot(budget,free=12*GIB),'low_space':True}
        decision=explain(budget,snapshot)['download']
        self.assertEqual(decision['impact'],'new_downloads_paused')
        self.assertEqual(decision['shortfall_bytes'],3*GIB)
        self.store.set_downloads_paused(True)
        budget.publish(self.store,snapshot)
        detail=self.client.get('/api/notifications').json['items'][0]['detail']
        self.assertIn('仍需手动开启',detail['next_step'])
        self.assertTrue(self.store.downloads_paused())

    def test_simultaneous_limits_are_separate_not_summed(self):
        budget=self.budget(disk_budget_gb=100,disk_test_budget_gb=80)
        self.require(budget,2*GIB)
        snapshot=self.snapshot(budget,free=60*GIB)
        snapshot.update(used_bytes=int(99.5*GIB),committed_bytes=int(99.75*GIB),waiting_tasks=1)
        snapshot['categories']['test'].update(used_bytes=int(79.25*GIB),committed_bytes=int(79.5*GIB))
        snapshot['categories']['downloads'].update(used_bytes=int(20.25*GIB),committed_bytes=int(20.25*GIB))
        decision=explain(budget,snapshot)['download']
        self.assertEqual(decision['shortfall_bytes'],int(1.75*GIB))
        limits={r['key']:r for r in decision['constraints'] if r['limiting']}
        self.assertEqual(limits['test']['shortfall_bytes'],int(1.5*GIB))
        self.assertEqual(limits['total']['shortfall_bytes'],int(1.75*GIB))
        self.assertIn('不能简单相加',decision['shortfall_text'])

    def test_unknown_next_request_is_not_fabricated_as_one_gib(self):
        budget=self.budget()
        snapshot=self.snapshot(budget,free=60*GIB)
        snapshot.update(used_bytes=20*GIB,committed_bytes=20*GIB)
        decision=explain(budget,snapshot)['download']
        self.assertEqual(decision['impact'],'new_downloads_paused')
        self.assertIsNone(decision['shortfall_bytes'])
        self.assertIsNone(decision['request_bytes'])

    def test_last_request_fitting_does_not_hide_other_waiting_tasks(self):
        budget=self.budget()
        self.require(budget,MIB)
        with budget.connect() as db:
            db.execute('INSERT INTO requirements VALUES (?,?,?,?,?)',('another-video',budget._version(),100*GIB,0,0))
        decision=explain(budget,self.snapshot(budget,free=60*GIB))['download']
        self.assertEqual(decision['impact'],'some_tasks_waiting')
        self.assertIn('1 个任务等待空间复查',decision['impact_text'])
        self.assertIsNone(decision['shortfall_bytes'])

    def test_high_disk_usage_alone_does_not_claim_downloads_stopped(self):
        budget=self.budget()
        with budget.connect() as db:budget._set(db,'disk_alert',dict(severity='critical'))
        decision=explain(budget,self.snapshot(budget,free=9*GIB))['download']
        self.assertEqual(decision['impact'],'unaffected')
        self.assertEqual(decision['shortfall_bytes'],0)

    def test_reserved_capacity_has_its_own_shortage(self):
        budget=self.budget(disk_min_free_gb=10,disk_resume_free_gb=15,disk_budget_gb=100,disk_test_budget_gb=80)
        self.require(budget,4*GIB)
        snapshot=self.snapshot(budget,free=20*GIB)
        snapshot.update(used_bytes=GIB,committed_bytes=9*GIB,waiting_tasks=1)
        decision=explain(budget,snapshot)['download']
        self.assertEqual(decision['shortfall_bytes'],2*GIB)
        self.assertEqual(decision['reservation_shortfall_bytes'],2*GIB)
        self.assertIn('其他执行已预留',decision['reason'])

    def test_actual_category_overflow_also_prevents_new_reservations(self):
        budget=self.budget(disk_import_budget_mb=1)
        snapshot=self.snapshot(budget,free=60*GIB)
        snapshot['categories']['imports'].update(used_bytes=2*MIB,committed_bytes=2*MIB)
        snapshot.update(used_bytes=2*MIB,committed_bytes=2*MIB)
        decision=explain(budget,snapshot)['download']
        self.assertEqual(decision['impact'],'new_downloads_paused')
        self.assertEqual(decision['shortfall_bytes'],MIB)
        self.assertIn('导入与上传',decision['reason'])

    def test_retained_local_inputs_make_restart_demand_unknown(self):
        budget=self.budget(disk_test_budget_gb=4)
        task=self.require(budget,2*GIB)
        with budget.lease('test',reserve=MIB,task=task['id']) as lease:
            (lease.path/'source').write_bytes(b'x')
            lease.preserve=True
        with budget.connect() as db:
            db.execute('INSERT OR REPLACE INTO requirements VALUES (?,?,?,?,?)',(task['id'],budget._version(),2*GIB,0,0))
        budget.config['disk_test_budget_gb']=1
        decision=explain(budget,self.snapshot(budget,free=60*GIB))['download']
        self.assertIsNone(decision['request_bytes'])
        self.assertIsNone(decision['shortfall_bytes'])
        self.assertIn('本地恢复',decision['shortfall_text'])
