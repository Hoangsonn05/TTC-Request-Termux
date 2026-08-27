import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import api_ttc


def _comment_job(number):
    return {
        "type": "like_poster",
        "_ttc_job": {
            "job_id": f"comment-{number}",
            "object_id": str(1000 + number),
            "comment_text": f"comment text {number}",
        },
    }


def _like_job(number=1):
    return {
        "type": "like_facebook",
        "_ttc_job": {
            "job_id": f"like-{number}",
            "object_id": str(2000 + number),
            "reaction_type": "LIKE",
        },
    }


def _comment_evidence(privacy_value=None):
    return {
        "visibility_scalars": {
            "comment_privacy_value": privacy_value,
        }
    }


class _FakeAPI:
    def __init__(self):
        self.fetch_types = []
        self.submitted_job_ids = []
        self.facebook_uid = None

    def set_facebook_uid(self, uid):
        self.facebook_uid = uid

    def fetch_jobs_for_legacy_type(self, legacy_type):
        self.fetch_types.append(legacy_type)
        return {
            "jobs": [{"type": legacy_type}],
            "no_job": [],
            "cooldowns": [],
            "permanent_skips": [],
        }

    def submit_job(self, original_job):
        self.submitted_job_ids.append(original_job["job_id"])
        return {"success": True, "earned_coin": 1}


class _FakeAction:
    evidence_sequence = []
    comment_result = True
    comment_calls = []
    reaction_calls = []

    def __init__(self):
        self.last_comment_evidence = _comment_evidence()

    @classmethod
    def reset(cls, evidence_sequence, comment_result=True):
        cls.evidence_sequence = list(evidence_sequence)
        cls.comment_result = comment_result
        cls.comment_calls = []
        cls.reaction_calls = []

    def get_info(self, _fb_token):
        return "facebook-uid", "ok"

    def auto_cmt(self, post_id, comment_text, fb_token, uid):
        type(self).comment_calls.append((post_id, comment_text, fb_token, uid))
        if type(self).evidence_sequence:
            self.last_comment_evidence = type(self).evidence_sequence.pop(0)
        return type(self).comment_result

    def auto_reaction(self, post_id, reaction, fb_token, uid):
        type(self).reaction_calls.append((post_id, reaction, fb_token, uid))
        return True


class HiddenCommentConfigTests(unittest.TestCase):
    def setUp(self):
        api_ttc.total_coin = 0

    def test_pending_approval_evidence_contract_remains_unchanged(self):
        evidence = api_ttc._facebook_comment_response_evidence({
            "comment_privacy_value": "PENDING_APPROVAL",
        })

        self.assertEqual(
            evidence["visibility_scalars"]["comment_privacy_value"],
            "PENDING_APPROVAL",
        )
        self.assertIs(api_ttc._is_comment_pending_approval(evidence), True)

    def test_old_run_config_defaults_flag_on_without_changing_existing_values(self):
        old_config = dict(api_ttc.DEFAULT_RUN_CONFIG)
        old_config.pop("skip_hidden_comments", None)
        old_config.update({
            "continuous": True,
            "max_jobs_per_acc": 17,
            "like_batch_min": 2,
            "like_batch_max": 4,
            "selected_fb_account_keys": ["email:a@example.com"],
        })

        normalized = api_ttc.normalize_run_config(old_config)

        self.assertIs(normalized["skip_hidden_comments"], True)
        for key, value in old_config.items():
            self.assertEqual(normalized[key], value)

    def test_toggle_off_and_on_persists_through_database_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_path = Path(temp_dir) / "profiles.json"
            legacy_path = Path(temp_dir) / "legacy-does-not-exist.json"
            old_config = dict(api_ttc.DEFAULT_RUN_CONFIG)
            old_config.pop("skip_hidden_comments", None)
            raw_database = {
                "version": api_ttc.DATA_VERSION,
                "active_profile_id": "profile-1",
                "profiles": [{
                    "profile_id": "profile-1",
                    "name": "Test",
                    "auth_token": "ttc-token",
                    "fb_accounts": [],
                    "run_config": old_config,
                }],
            }
            data_path.write_text(json.dumps(raw_database), encoding="utf-8")

            with (
                patch.object(api_ttc, "DATA_FILE", str(data_path)),
                patch.object(api_ttc, "LEGACY_CONFIG_FILE", str(legacy_path)),
                patch.object(api_ttc, "profile_run_lock", side_effect=lambda _profile_id: contextlib.nullcontext()),
                patch.object(api_ttc, "db_transaction_lock", side_effect=contextlib.nullcontext),
                patch.object(api_ttc, "print_success"),
                patch("builtins.input", side_effect=["10", "12"]),
            ):
                loaded = api_ttc.load_database()
                profile = api_ttc.get_profile(loaded, "profile-1")
                self.assertIs(profile["run_config"]["skip_hidden_comments"], True)

                api_ttc.configure_run(profile)
                reloaded = api_ttc.load_database()
                profile = api_ttc.get_profile(reloaded, "profile-1")
                self.assertIs(profile["run_config"]["skip_hidden_comments"], False)

                with patch("builtins.input", side_effect=["10", "12"]):
                    api_ttc.configure_run(profile)
                reloaded_again = api_ttc.load_database()
                self.assertIs(
                    api_ttc.get_profile(reloaded_again, "profile-1")["run_config"]["skip_hidden_comments"],
                    True,
                )

    def test_summary_reports_hidden_comment_policy(self):
        config = api_ttc.normalize_run_config()
        self.assertIn("hidden-comment-skip=ON", api_ttc.run_config_summary(config))
        config["skip_hidden_comments"] = False
        self.assertIn("hidden-comment-skip=OFF", api_ttc.run_config_summary(config))

    def test_run_active_profile_propagates_flag(self):
        profile = {
            "profile_id": "profile-1",
            "name": "Test",
            "auth_token": "ttc-token",
            "fb_accounts": [{"email": "a@example.com", "token": "fb-token"}],
            "run_config": {"skip_hidden_comments": False},
        }
        with (
            patch.object(api_ttc, "selected_accounts", return_value=profile["fb_accounts"]),
            patch.object(api_ttc, "profile_run_lock", return_value=contextlib.nullcontext()),
            patch.object(api_ttc, "run_multi_tokens") as run_multi_tokens,
            patch.object(api_ttc, "print_info"),
        ):
            api_ttc.run_active_profile(profile)

        self.assertIs(run_multi_tokens.call_args.kwargs["skip_hidden_comments"], False)

    def test_run_multi_tokens_propagates_flag(self):
        class FakeTTC:
            def __init__(self, _auth_token, worker_id=None):
                self.worker_id = worker_id

            def use_fetch_state(self, _state):
                pass

            def login(self):
                pass

            def close(self):
                pass

        with (
            patch.object(api_ttc, "API_TTC", FakeTTC),
            patch.object(api_ttc, "run_ttc_jobs", return_value=0) as run_ttc_jobs,
            patch.object(api_ttc, "print_info"),
        ):
            api_ttc.run_multi_tokens(
                "ttc-token",
                [{"email": "a@example.com", "token": "fb-token"}],
                skip_hidden_comments=False,
            )

        self.assertIs(run_ttc_jobs.call_args.kwargs["skip_hidden_comments"], False)

    def _run_jobs(self, *, skip_hidden_comments, evidence_sequence, comment_result=True, comments=2):
        api = _FakeAPI()
        root_jobs = [_comment_job(number) for number in range(1, comments + 1)]
        fallback_jobs = [_like_job()]
        _FakeAction.reset(evidence_sequence, comment_result=comment_result)

        def schedule(_fetched_jobs, enabled_types, _max_jobs, **_kwargs):
            if enabled_types == frozenset({"like_facebook"}):
                return fallback_jobs
            return root_jobs

        with (
            patch.object(api_ttc, "AUTO_Action", _FakeAction),
            patch.object(api_ttc, "get_facebook_post_id", side_effect=lambda url: url.rsplit("/", 1)[-1]),
            patch.object(api_ttc, "schedule_jobs_by_burst", side_effect=schedule),
            patch.object(api_ttc, "_is_comment_pending_approval", wraps=api_ttc._is_comment_pending_approval) as detector,
            patch.object(api_ttc, "print_warning") as warning,
            patch.object(api_ttc, "print_info"),
            patch.object(api_ttc, "print_success"),
            patch.object(api_ttc.time, "sleep") as settle_sleep,
            patch.object(api_ttc, "countdown") as countdown,
        ):
            done = api_ttc.run_ttc_jobs(
                api,
                "fb-token",
                max_jobs=comments,
                job_types={"like_poster"},
                like_batch_min=1,
                like_batch_max=1,
                comment_batch_min=1,
                comment_batch_max=1,
                like_delay_range=(0, 0),
                comment_delay_range=(0, 0),
                failed_delay_range=(0, 0),
                skip_hidden_comments=skip_hidden_comments,
            )
        return api, done, detector, warning, settle_sleep, countdown

    def test_hidden_comment_on_preserves_skip_and_like_fallback(self):
        api, done, detector, warning, settle_sleep, _countdown = self._run_jobs(
            skip_hidden_comments=True,
            evidence_sequence=[_comment_evidence("PENDING_APPROVAL")],
        )

        self.assertEqual(done, 1)
        self.assertEqual(api.submitted_job_ids, ["like-1"])
        self.assertEqual(len(_FakeAction.comment_calls), 1)
        self.assertEqual(len(_FakeAction.reaction_calls), 1)
        detector.assert_called_once()
        warning.assert_called_once_with("Acc yếu bị Facebook ẩn comment.")
        self.assertNotIn(call(5), settle_sleep.call_args_list)
        self.assertEqual(api.fetch_types.count("like_facebook"), 1)

    def test_hidden_comment_off_warns_submits_and_allows_following_comment(self):
        api, done, detector, warning, settle_sleep, countdown = self._run_jobs(
            skip_hidden_comments=False,
            evidence_sequence=[
                _comment_evidence("PENDING_APPROVAL"),
                _comment_evidence("VISIBLE"),
            ],
        )

        self.assertEqual(done, 2)
        self.assertEqual(api.submitted_job_ids, ["comment-1", "comment-2"])
        self.assertEqual(len(_FakeAction.comment_calls), 2)
        self.assertEqual(_FakeAction.reaction_calls, [])
        self.assertEqual(detector.call_count, 2)
        warning.assert_called_once_with("Acc yếu bị Facebook ẩn comment.")
        self.assertEqual(settle_sleep.call_args_list, [call(5), call(5)])
        countdown.assert_called_once_with(0, "Waiting before job 2")
        self.assertNotIn("like_facebook", api.fetch_types)

    def test_normal_comment_is_identical_when_flag_is_on_or_off(self):
        outcomes = []
        for flag in (True, False):
            api, done, detector, warning, settle_sleep, countdown = self._run_jobs(
                skip_hidden_comments=flag,
                evidence_sequence=[_comment_evidence("VISIBLE")],
                comments=1,
            )
            outcomes.append((api.submitted_job_ids, done, settle_sleep.call_args_list, countdown.call_args_list))
            detector.assert_called_once()
            warning.assert_not_called()
        self.assertEqual(outcomes[0], outcomes[1])

    def test_failed_comment_never_submits_for_either_flag(self):
        for flag in (True, False):
            api, done, detector, _warning, settle_sleep, _countdown = self._run_jobs(
                skip_hidden_comments=flag,
                evidence_sequence=[_comment_evidence("PENDING_APPROVAL")],
                comment_result=False,
                comments=1,
            )
            self.assertEqual(done, 0)
            self.assertEqual(api.submitted_job_ids, [])
            detector.assert_not_called()
            settle_sleep.assert_called_once_with(3)


if __name__ == "__main__":
    unittest.main()
