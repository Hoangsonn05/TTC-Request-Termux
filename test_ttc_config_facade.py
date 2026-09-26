import unittest

from api_ttc import (
    apply_run_config_facade,
    get_run_config_facade,
    normalize_run_config,
    run_config_facade_summary,
)


class RunConfigFacadeTests(unittest.TestCase):
    HIDDEN_FIELDS = (
        "like_batch_min",
        "like_batch_max",
        "comment_batch_min",
        "comment_batch_max",
        "failed_delay_min",
        "failed_delay_max",
        "skip_hidden_comments",
        "selected_fb_account_keys",
    )

    def setUp(self):
        self.existing = normalize_run_config({
            "job_types": ["like_facebook", "like_poster"],
            "continuous": False,
            "max_jobs_per_acc": 41,
            "like_batch_min": 7,
            "like_batch_max": 9,
            "comment_batch_min": 3,
            "comment_batch_max": 4,
            "like_delay_min": 6,
            "like_delay_max": 12,
            "comment_delay_min": 6,
            "comment_delay_max": 12,
            "failed_delay_min": 21,
            "failed_delay_max": 25,
            "account_delay_min": 31,
            "account_delay_max": 35,
            "skip_hidden_comments": False,
            "selected_fb_account_keys": ["account-a", "account-b"],
        })

    def assert_hidden_fields_preserved(self, result):
        for field in self.HIDDEN_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(result[field], self.existing[field])

    def test_job_type_modes_map_without_changing_hidden_fields(self):
        expected = {
            "like": ["like_facebook"],
            "comment": ["like_poster"],
            "both": ["like_facebook", "like_poster"],
        }
        for mode, job_types in expected.items():
            with self.subTest(mode=mode):
                result = apply_run_config_facade(
                    self.existing,
                    {"job_type": mode},
                )
                self.assertEqual(result["job_types"], job_types)
                self.assert_hidden_fields_preserved(result)

    def test_apply_updates_only_user_facing_fields(self):
        result = apply_run_config_facade(self.existing, {
            "job_type": "both",
            "max_jobs_per_acc": 55,
            "job_delay_min": 8,
            "job_delay_max": 15,
            "account_delay_min": 10,
            "account_delay_max": 16,
            "continuous": True,
        })

        self.assertEqual((result["like_delay_min"], result["like_delay_max"]), (8, 15))
        self.assertEqual(
            (result["comment_delay_min"], result["comment_delay_max"]),
            (8, 15),
        )
        self.assertEqual(
            (result["account_delay_min"], result["account_delay_max"]),
            (10, 16),
        )
        self.assertEqual(result["max_jobs_per_acc"], 55)
        self.assertIs(result["continuous"], True)
        self.assert_hidden_fields_preserved(result)

        self.assertEqual(get_run_config_facade(result), {
            "job_type": "both",
            "max_jobs_per_acc": 55,
            "job_delay_min": 8,
            "job_delay_max": 15,
            "account_delay_min": 10,
            "account_delay_max": 16,
            "continuous": True,
        })
        self.assertEqual(
            run_config_facade_summary(result),
            "Job=Like+Comment | max=55 | delay=8-15s | "
            "acc=10-16s | continuous=ON",
        )

    def test_default_and_legacy_configs_round_trip(self):
        legacy = normalize_run_config({
            "job_types": ["like_facebook"],
            "like_batch_limit": 8,
            "comment_batch_limit": 4,
            "normal_delay_min": 9,
            "normal_delay_max": 14,
            "selected_fb_account_keys": ["legacy-account"],
        })
        for existing in (normalize_run_config(), legacy):
            with self.subTest(job_types=existing["job_types"]):
                facade = get_run_config_facade(existing)
                self.assertEqual(
                    apply_run_config_facade(existing, facade),
                    existing,
                )

    def test_existing_validation_is_used_and_mixed_delays_round_trip(self):
        invalid_updates = (
            {"job_type": "unsupported"},
            {"max_jobs_per_acc": 0},
            {"job_delay_min": 20, "job_delay_max": 10},
            {"job_delay_min": None, "job_delay_max": 10},
            {"unknown": 1},
        )
        for updates in invalid_updates:
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    apply_run_config_facade(self.existing, updates)

        mixed = dict(self.existing)
        mixed["comment_delay_max"] = 13
        facade = get_run_config_facade(mixed)
        self.assertIsNone(facade["job_delay_min"])
        self.assertIsNone(facade["job_delay_max"])
        self.assertEqual(apply_run_config_facade(mixed, facade), mixed)
        self.assertIn("delay=mixed", run_config_facade_summary(mixed))


if __name__ == "__main__":
    unittest.main()
