import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from unittest.mock import patch

import api_ttc


class _VipEligibilityClient:
    def __init__(self):
        self.calls = []

    def get_jobs(self, job_type):
        self.calls.append(job_type)
        if len(self.calls) == 1:
            raise api_ttc.TTCVipIneligibleError(
                "VIP ineligible",
                job_type=job_type.value,
                server_raw_text="vip ineligible",
            )
        return [{"type": job_type.value, "idpost": "job-1"}]


class UX5LockedContractTests(unittest.TestCase):
    def test_vip_ineligible_skip_stays_scoped_to_exact_uid_endpoint(self):
        fetch_state = api_ttc.TTCFetchState()
        adapter = api_ttc.API_TTC("ttc-token", fetch_state=fetch_state)
        client = _VipEligibilityClient()
        adapter._client = client
        adapter._facebook_uid = "facebook-uid-1"

        result = adapter.fetch_jobs_for_legacy_type("like_facebook")

        first_endpoint = api_ttc.TTCJobType.LIKE_POST_VIP_CHEO.value
        self.assertEqual(
            fetch_state.permanent_skip,
            {("facebook-uid-1", first_endpoint)},
        )
        self.assertEqual(
            client.calls[:2],
            [
                api_ttc.TTCJobType.LIKE_POST_VIP_CHEO,
                api_ttc.TTCJobType.LIKE_POST_VIP_RE,
            ],
        )
        self.assertEqual(len(result["jobs"]), 1)
        self.assertEqual(result["permanent_skips"][0]["job_type"], first_endpoint)

    def test_burst_plan_is_not_refilled_to_max_jobs(self):
        fake_api = _CountingAPI()
        template = fake_api.jobs[0]
        fake_api.jobs = []
        for index in range(5):
            job = deepcopy(template)
            job["_ttc_job"]["job_id"] = f"job-{index}"
            job["_ttc_job"]["idpost"] = f"claim-{index}"
            fake_api.jobs.append(job)
        output = io.StringIO()

        with patch.dict(
            os.environ, {api_ttc.RUNTIME_DEBUG_ENV: ""}
        ), redirect_stdout(output):
            done = api_ttc.run_ttc_jobs(
                fake_api,
                "facebook-secret-token",
                max_jobs=5,
                job_types={"like_facebook"},
                like_batch_min=2,
                like_batch_max=2,
                like_delay_range=(0, 0),
                failed_delay_range=(0, 0),
                action=_CountingAction(),
                transaction_sleep_fn=lambda _seconds: None,
            )

        self.assertEqual(done, 2)
        self.assertEqual(fake_api.submit_calls, 2)
        result_lines = [
            line for line in output.getvalue().splitlines() if "LIKE" in line
        ]
        self.assertEqual(len(result_lines), 2)
        self.assertIn("[1/2]", result_lines[0])
        self.assertIn("[2/2]", result_lines[1])


class UX5MenuPersistenceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_file_patch = patch.object(
            api_ttc,
            "DATA_FILE",
            os.path.join(self.temp_dir.name, "ttc-data.json"),
        )
        self.data_file_patch.start()
        self.old_session_profile_id = api_ttc.SESSION_PROFILE_ID

    def tearDown(self):
        api_ttc.SESSION_PROFILE_ID = self.old_session_profile_id
        self.data_file_patch.stop()
        self.temp_dir.cleanup()

    def make_profile(self, profile_id, name, auth_token, accounts, run_config):
        return {
            "profile_id": profile_id,
            "name": name,
            "auth_token": auth_token,
            "fb_accounts": accounts,
            "run_config": api_ttc.normalize_run_config(run_config),
        }

    def save_profiles(self, profiles, active_profile_id):
        api_ttc.save_database({
            "version": api_ttc.DATA_VERSION,
            "active_profile_id": active_profile_id,
            "profiles": profiles,
        })
        api_ttc.SESSION_PROFILE_ID = active_profile_id

    def run_main(self, inputs):
        output = io.StringIO()
        with patch("builtins.input", side_effect=inputs), patch.object(
            api_ttc.os, "system"
        ), patch.object(api_ttc, "run_multi_tokens") as runtime, redirect_stdout(output):
            api_ttc.main()
        return runtime, output.getvalue()

    def test_wizard_persists_full_config_and_runtime_receives_same_values(self):
        accounts = [
            {
                "email": "account-a",
                "password": "password-a",
                "secret": "two-factor-a",
                "token": "facebook-token-a",
            },
            {
                "email": "account-b",
                "password": "password-b",
                "secret": "two-factor-b",
                "token": "facebook-token-b",
            },
        ]
        original = api_ttc.normalize_run_config({
            "job_types": ["like_facebook", "like_poster"],
            "continuous": False,
            "max_jobs_per_acc": 41,
            "like_batch_min": 7,
            "like_batch_max": 9,
            "comment_batch_min": 3,
            "comment_batch_max": 4,
            "like_delay_min": 8,
            "like_delay_max": 12,
            "comment_delay_min": 15,
            "comment_delay_max": 20,
            "failed_delay_min": 21,
            "failed_delay_max": 25,
            "account_delay_min": 31,
            "account_delay_max": 35,
            "skip_hidden_comments": False,
            "selected_fb_account_keys": ["email:account-a", "email:account-b"],
        })
        profile = self.make_profile(
            "profile-a", "Primary", "ttc-token-a", accounts, original
        )
        self.save_profiles([profile], "profile-a")

        runtime, rendered = self.run_main([
            "1", "3", "55", "10", "15", "", "", "", "y", "0",
        ])

        persisted = api_ttc.get_profile(api_ttc.load_database(), "profile-a")
        config = persisted["run_config"]
        self.assertEqual(config["max_jobs_per_acc"], 55)
        self.assertEqual(
            (
                config["like_delay_min"],
                config["like_delay_max"],
                config["comment_delay_min"],
                config["comment_delay_max"],
            ),
            (10, 15, 10, 15),
        )
        for field in (
            "like_batch_min",
            "like_batch_max",
            "comment_batch_min",
            "comment_batch_max",
            "failed_delay_min",
            "failed_delay_max",
            "skip_hidden_comments",
            "selected_fb_account_keys",
        ):
            with self.subTest(field=field):
                self.assertEqual(config[field], original[field])

        runtime.assert_called_once()
        args, kwargs = runtime.call_args
        self.assertEqual(args[0], "ttc-token-a")
        self.assertEqual(
            [account["email"] for account in args[1]],
            ["account-a", "account-b"],
        )
        self.assertEqual(kwargs["max_jobs_per_acc"], config["max_jobs_per_acc"])
        self.assertEqual(
            (kwargs["like_delay_min"], kwargs["like_delay_max"]), (10, 15)
        )
        self.assertEqual(
            (kwargs["comment_delay_min"], kwargs["comment_delay_max"]),
            (10, 15),
        )
        self.assertEqual(
            (kwargs["like_batch_min"], kwargs["like_batch_max"]), (7, 9)
        )
        self.assertEqual(
            (kwargs["comment_batch_min"], kwargs["comment_batch_max"]), (3, 4)
        )
        self.assertEqual(
            (kwargs["failed_delay_min"], kwargs["failed_delay_max"]), (21, 25)
        )
        self.assertIs(kwargs["skip_hidden_comments"], False)
        for secret in (
            "ttc-token-a",
            "facebook-token-a",
            "password-a",
            "two-factor-a",
        ):
            self.assertNotIn(secret, rendered)

    def test_mixed_delay_enter_preserves_distinct_runtime_ranges(self):
        config = api_ttc.normalize_run_config({
            "job_types": ["like_facebook", "like_poster"],
            "like_delay_min": 8,
            "like_delay_max": 12,
            "comment_delay_min": 15,
            "comment_delay_max": 20,
            "selected_fb_account_keys": ["email:account-a"],
        })
        profile = self.make_profile(
            "profile-a",
            "Primary",
            "ttc-token-a",
            [{"email": "account-a", "token": "facebook-token-a"}],
            config,
        )
        self.save_profiles([profile], "profile-a")

        runtime, _ = self.run_main([
            "1", "3", "", "", "", "", "", "", "y", "0",
        ])

        runtime.assert_called_once()
        kwargs = runtime.call_args.kwargs
        self.assertEqual(
            (kwargs["like_delay_min"], kwargs["like_delay_max"]), (8, 12)
        )
        self.assertEqual(
            (kwargs["comment_delay_min"], kwargs["comment_delay_max"]),
            (15, 20),
        )
        persisted = api_ttc.get_profile(
            api_ttc.load_database(), "profile-a"
        )["run_config"]
        self.assertEqual(persisted, config)

    def test_account_menu_selection_is_used_by_next_run(self):
        accounts = [
            {"email": "account-a", "token": "facebook-token-a"},
            {"email": "account-b", "token": "facebook-token-b"},
        ]
        profile = self.make_profile(
            "profile-a",
            "Primary",
            "ttc-token-a",
            accounts,
            {"selected_fb_account_keys": ["email:account-a", "email:account-b"]},
        )
        self.save_profiles([profile], "profile-a")

        runtime, _ = self.run_main([
            "2", "3", "2", "0",
            "1", "3", "", "", "", "", "", "", "y", "0",
        ])

        persisted = api_ttc.get_profile(api_ttc.load_database(), "profile-a")
        self.assertEqual(
            persisted["run_config"]["selected_fb_account_keys"],
            ["email:account-b"],
        )
        runtime.assert_called_once()
        selected = runtime.call_args.args[1]
        self.assertEqual(
            [(account["email"], account["token"]) for account in selected],
            [("account-b", "facebook-token-b")],
        )

    def test_profile_switch_refreshes_header_accounts_and_run_config(self):
        profile_a = self.make_profile(
            "profile-a",
            "Profile A",
            "ttc-token-a",
            [{"email": "account-a", "token": "facebook-token-a"}],
            {
                "max_jobs_per_acc": 11,
                "selected_fb_account_keys": ["email:account-a"],
            },
        )
        profile_b = self.make_profile(
            "profile-b",
            "Profile B",
            "ttc-token-b",
            [{"email": "account-b", "token": "facebook-token-b"}],
            {
                "job_types": ["like_poster"],
                "max_jobs_per_acc": 22,
                "comment_delay_min": 15,
                "comment_delay_max": 20,
                "selected_fb_account_keys": ["email:account-b"],
            },
        )
        self.save_profiles([profile_a, profile_b], "profile-a")

        runtime, rendered = self.run_main([
            "3", "1", "2", "0",
            "1", "2", "", "", "", "", "", "", "y", "0",
        ])

        self.assertEqual(api_ttc.SESSION_PROFILE_ID, "profile-b")
        self.assertEqual(api_ttc.load_database()["active_profile_id"], "profile-b")
        runtime.assert_called_once()
        args, kwargs = runtime.call_args
        self.assertEqual(args[0], "ttc-token-b")
        self.assertEqual([account["email"] for account in args[1]], ["account-b"])
        self.assertEqual(kwargs["profile_id"], "profile-b")
        self.assertEqual(kwargs["max_jobs_per_acc"], 22)
        self.assertEqual(kwargs["job_types"], {"like_poster"})
        self.assertIn("Profile A", rendered)
        self.assertIn("Profile B", rendered)

    def test_login_routes_reuse_existing_batch_and_manual_boundaries(self):
        profile = self.make_profile(
            "profile-a", "Primary", "ttc-token-a", [], {}
        )
        self.save_profiles([profile], "profile-a")
        credentials = "10001|password-value|two-factor-value"
        manual_token = "manual-facebook-token"
        output = io.StringIO()

        with patch("builtins.input", side_effect=[
            "2", "2", "1", credentials, "2", manual_token, "0", "0", "0",
        ]), patch.object(api_ttc.os, "system"), patch.object(
            api_ttc, "batch_login_accounts"
        ) as batch, patch.object(
            api_ttc, "add_manual_account", return_value=True
        ) as manual, redirect_stdout(output):
            api_ttc.main()

        batch.assert_called_once_with("profile-a", credentials)
        manual.assert_called_once_with("profile-a", manual_token)
        rendered = output.getvalue()
        self.assertNotIn("password-value", rendered)
        self.assertNotIn("two-factor-value", rendered)
        self.assertNotIn(manual_token, rendered)

    def test_cancelled_wizard_does_not_persist_or_start_runtime(self):
        profile = self.make_profile(
            "profile-a",
            "Primary",
            "ttc-token-a",
            [{"email": "account-a", "token": "facebook-token-a"}],
            {
                "max_jobs_per_acc": 41,
                "selected_fb_account_keys": ["email:account-a"],
            },
        )
        self.save_profiles([profile], "profile-a")
        before = deepcopy(
            api_ttc.get_profile(api_ttc.load_database(), "profile-a")["run_config"]
        )

        runtime, _ = self.run_main(["1", "3", "99", "cancel", "0"])

        after = api_ttc.get_profile(
            api_ttc.load_database(), "profile-a"
        )["run_config"]
        self.assertEqual(after, before)
        runtime.assert_not_called()


class _CountingAPI:
    def __init__(self):
        original = {
            "type": "ttc_reaction_vip_re",
            "ttc_source": "ttc_reaction_vip_re",
            "job_id": "job-1",
            "idpost": "claim-1",
            "object_id": "object-123",
            "post_id": "456",
            "reaction_type": "LIKE",
        }
        self.jobs = [{"type": "like_facebook", "_ttc_job": original}]
        self.fetch_calls = 0
        self.submit_calls = 0
        self.bound_uids = []

    def set_facebook_uid(self, uid):
        self.bound_uids.append(uid)

    def bind_facebook_uid(self, uid):
        self.bound_uids.append(uid)

    def fetch_jobs_for_legacy_type(self, _job_type):
        self.fetch_calls += 1
        jobs, self.jobs = self.jobs, []
        return {
            "jobs": jobs,
            "no_job": [] if jobs else [{"countdown": 0}],
            "cooldowns": [],
            "permanent_skips": [],
        }

    def submit_job(self, _job):
        self.submit_calls += 1
        return {"success": True, "earned_coin": 600, "balance": 10600}

    def get_account_info(self):
        return {"balance": 10000}


class _CountingAction:
    def __init__(self):
        self.action_calls = 0
        self.log_fn = None
        self.last_reaction_evidence = {
            "success": True,
            "classification": "SUCCESS",
            "attempts": 1,
        }

    def set_log_fn(self, log_fn):
        self.log_fn = log_fn

    def check_live(self, _token):
        return {
            "status": api_ttc.FACEBOOK_LIVE,
            "uid": "facebook-uid-1",
            "reason": "Facebook token LIVE",
        }

    def auto_reaction(self, *_args):
        self.action_calls += 1
        self.log_fn("object id resolved\nauthorization=runtime-secret", "raw")
        return True


class UX5DebugEquivalenceTests(unittest.TestCase):
    def execute(self, debug_value):
        fake_api = _CountingAPI()
        action = _CountingAction()
        output = io.StringIO()
        with patch.dict(
            os.environ, {api_ttc.RUNTIME_DEBUG_ENV: debug_value}
        ), redirect_stdout(output):
            result = api_ttc.run_ttc_jobs(
                fake_api,
                "facebook-secret-token",
                max_jobs=1,
                job_types={"like_facebook"},
                like_delay_range=(0, 0),
                failed_delay_range=(0, 0),
                action=action,
                transaction_sleep_fn=lambda _seconds: None,
            )
        return result, fake_api, action, output.getvalue()

    def test_debug_changes_diagnostics_not_business_execution(self):
        normal = self.execute("")
        debug = self.execute("1")

        self.assertEqual(normal[0], debug[0])
        self.assertEqual(normal[1].fetch_calls, debug[1].fetch_calls)
        self.assertEqual(normal[1].submit_calls, debug[1].submit_calls)
        self.assertEqual(normal[1].bound_uids, debug[1].bound_uids)
        self.assertEqual(normal[2].action_calls, debug[2].action_calls)
        normal_results = [line for line in normal[3].splitlines() if "[1/1] LIKE" in line]
        debug_results = [line for line in debug[3].splitlines() if "[1/1] LIKE" in line]
        self.assertEqual(normal_results, debug_results)
        self.assertNotIn("object id resolved", normal[3])
        self.assertIn("object id resolved", debug[3])
        self.assertNotIn("runtime-secret", debug[3])
        self.assertIn("authorization=***", debug[3])


if __name__ == "__main__":
    unittest.main()
