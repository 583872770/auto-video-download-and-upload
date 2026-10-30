"""Run behavioral regressions in isolated fixtures; propagate test failures."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHECKS = (
    'tests.test_lease_ownership',
    'tests.test_subscription_boundary',
    'tests.test_hybrid.HybridTests.test_local_rpc_protocol_matches_real_routes_and_excludes_shared_management',
    'tests.test_scale.ScaleTests.test_concurrent_claims_do_not_execute_the_same_task_twice',
    'tests.test_scale.ScaleTests.test_expired_lease_recovery_fences_old_owner_and_finish_is_idempotent',
    'tests.test_fleet.FleetTests.test_expired_attempt_cannot_report_or_bill_after_takeover',
    'tests.test_web.WebTests.test_settings_roundtrip_and_invalid_write_is_atomic',
    'tests.test_web.WebTests.test_video_stream_supports_ranges_unicode_and_blocks_non_video_paths',
    'tests.test_statistics_methods.SQLiteMethodStatisticsTests.test_automatic_first_assignment_and_retry_use_actual_adapter',
)


def main():
    suite = unittest.defaultTestLoader.loadTestsFromNames(CHECKS)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
