import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import api_ttc


class RunWizardTests(unittest.TestCase):
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
        self.config = api_ttc.normalize_run_config({
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
        self.profile = {
            "profile_id": "profile-1",
            "name": "Test",
            "run_config": self.config,
        }

    def run_wizard(self, inputs, config=None):
        existing = api_ttc.normalize_run_config(config or self.config)
        profile = dict(self.profile, run_config=existing)

        def persist(profile_id, updates):
            self.assertEqual(profile_id, "profile-1")
            merged = dict(existing)
            merged.update(updates)
            return api_ttc.normalize_run_config(merged)

        output = io.StringIO()
        with patch("builtins.input", side_effect=inputs), patch.object(
            api_ttc, "update_profile_run_config", side_effect=persist
        ) as update, redirect_stdout(output):
            result = api_ttc.configure_run(profile)
        return result, update, output.getvalue()

    def test_job_type_choices_map_to_runtime_types(self):
        cases = {
            "1": ["like_facebook"],
            "2": ["like_poster"],
            "3": ["like_facebook", "like_poster"],
        }
        for choice, expected in cases.items():
            with self.subTest(choice=choice):
                result, update, _ = self.run_wizard(
                    [choice, "", "", "", "", "", "", "y"]
                )
                self.assertEqual(result["job_types"], expected)
                update.assert_called_once()

    def test_enter_keeps_current_values_and_hidden_fields(self):
        result, update, output = self.run_wizard(
            ["3", "", "", "", "", "", "", "y"]
        )
        self.assertEqual(result, self.config)
        update.assert_called_once_with(
            "profile-1",
            {"job_types": ["like_facebook", "like_poster"]},
        )
        self.assertIn("Max/acc   : 41", output)
        self.assertNotIn("burst", output.lower())
        self.assertNotIn("failed delay", output.lower())
        self.assertNotIn("skip_hidden_comments", output)
        self.assertNotIn("selected_fb_account_keys", output)

    def test_new_shared_delay_updates_like_and_comment_and_preserves_hidden(self):
        result, update, _ = self.run_wizard(
            ["3", "55", "8", "15", "10", "16", "y", "y"]
        )
        self.assertEqual(
            (result["like_delay_min"], result["like_delay_max"]),
            (8, 15),
        )
        self.assertEqual(
            (result["comment_delay_min"], result["comment_delay_max"]),
            (8, 15),
        )
        self.assertEqual(result["max_jobs_per_acc"], 55)
        self.assertEqual(
            (result["account_delay_min"], result["account_delay_max"]),
            (10, 16),
        )
        self.assertIs(result["continuous"], True)
        for field in self.HIDDEN_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(result[field], self.config[field])
        persisted = update.call_args.args[1]
        self.assertNotIn("failed_delay_min", persisted)
        self.assertNotIn("skip_hidden_comments", persisted)
        self.assertNotIn("selected_fb_account_keys", persisted)

    def test_mixed_delay_enter_preserves_both_original_ranges(self):
        mixed = dict(self.config)
        mixed.update({
            "like_delay_min": 6,
            "like_delay_max": 12,
            "comment_delay_min": 7,
            "comment_delay_max": 13,
        })
        result, update, output = self.run_wizard(
            ["3", "", "", "", "", "", "", "y"],
            config=mixed,
        )
        self.assertEqual(
            (
                result["like_delay_min"],
                result["like_delay_max"],
                result["comment_delay_min"],
                result["comment_delay_max"],
            ),
            (6, 12, 7, 13),
        )
        self.assertIn("riêng theo từng loại job", output)
        persisted = update.call_args.args[1]
        self.assertNotIn("like_delay_min", persisted)
        self.assertNotIn("comment_delay_min", persisted)

    def test_invalid_range_then_cancel_does_not_persist_partial_config(self):
        result, update, output = self.run_wizard(
            ["3", "", "20", "10", "cancel"]
        )
        self.assertIsNone(result)
        update.assert_not_called()
        self.assertIn("Delay job", output)

    def test_back_and_confirmation_cancel_do_not_persist_or_run(self):
        result, update, _ = self.run_wizard(["0"])
        self.assertIsNone(result)
        update.assert_not_called()

        result, update, _ = self.run_wizard(
            ["3", "", "", "", "", "", "", "0"]
        )
        self.assertIsNone(result)
        update.assert_not_called()


class MainMenuTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "profile_id": "profile-1",
            "name": "Primary",
            "auth_token": "ttc-token",
            "fb_accounts": [],
            "run_config": api_ttc.normalize_run_config(),
        }
        self.data = {
            "version": api_ttc.DATA_VERSION,
            "active_profile_id": "profile-1",
            "profiles": [self.profile],
        }

    def test_main_menu_has_only_three_groups_and_exit(self):
        output = io.StringIO()
        with redirect_stdout(output):
            api_ttc.print_menu(self.profile)
        rendered = output.getvalue()
        self.assertIn("1. Chạy job", rendered)
        self.assertIn("2. Quản lý Facebook", rendered)
        self.assertIn("3. Quản lý TTC", rendered)
        self.assertIn("0. Thoát", rendered)
        self.assertNotIn("Kiểm tra số dư", rendered)
        self.assertNotIn("Xem jobs", rendered)
        self.assertNotIn("Cấu hình delay / burst", rendered)

    def test_ttc_submenu_keeps_balance_jobs_and_profile_management_reachable(self):
        output = io.StringIO()
        with patch("builtins.input", side_effect=["4", "5", "6", "0"]), patch.object(
            api_ttc, "load_database", return_value=self.data
        ), patch.object(api_ttc, "get_profile", return_value=self.profile), patch.object(
            api_ttc, "check_balance"
        ) as balance, patch.object(
            api_ttc, "show_jobs_for_profile"
        ) as jobs, patch.object(
            api_ttc, "profile_manager"
        ) as profiles, redirect_stdout(output):
            api_ttc.ttc_manager()
        balance.assert_called_once_with(self.profile)
        jobs.assert_called_once_with(self.profile)
        profiles.assert_called_once_with(self.data)

    def test_main_runs_only_after_wizard_returns_persisted_config(self):
        updated = api_ttc.apply_run_config_facade(
            self.profile["run_config"], {"job_type": "comment"}
        )
        with patch("builtins.input", side_effect=["1", "0"]), patch.object(
            api_ttc.os, "system"
        ), patch.object(
            api_ttc, "load_database", return_value=self.data
        ), patch.object(
            api_ttc, "get_profile", return_value=self.profile
        ), patch.object(
            api_ttc, "print_menu"
        ), patch.object(
            api_ttc, "configure_run", return_value=updated
        ) as wizard, patch.object(
            api_ttc, "run_active_profile"
        ) as run:
            api_ttc.main()
        wizard.assert_called_once_with(self.profile)
        run.assert_called_once()
        run_profile = run.call_args.args[0]
        self.assertEqual(run_profile["run_config"], updated)
        self.assertEqual(run_profile["profile_id"], "profile-1")


if __name__ == "__main__":
    unittest.main()
