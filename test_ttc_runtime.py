import copy
import contextlib
import inspect
import unittest
from unittest.mock import MagicMock, call, patch

import api_bumx
from TTC_API import TTCAuthError, TTCRateLimitError, TTCResponseError


def make_original(kind, suffix="1", *, reaction="LOVE", comment="hello TTC"):
    return {
        "job_id": f"ttc_{kind}:{suffix}",
        "buff_id": f"ttc_{kind}:{suffix}",
        "type": f"ttc_{kind}",
        "action_group": "comment" if kind == "comment" else "reaction",
        "object_id": f"1000{suffix}",
        "link": f"https://facebook.com/1000{suffix}",
        "reaction_type": "" if kind == "comment" else reaction,
        "comment_text": comment if kind == "comment" else "",
        "ttc_source": f"ttc_{kind}",
        "idpost": f"post-{suffix}",
        "raw_data": {"idpost": f"post-{suffix}"},
    }


def wrap(original, legacy_type):
    wrapper = dict(original)
    wrapper["type"] = legacy_type
    wrapper["_ttc_job"] = original
    return wrapper


def fetched(*jobs, no_job=None):
    return {"jobs": list(jobs), "no_job": list(no_job or [])}


class TTCCoreRuntimeTests(unittest.TestCase):
    def setUp(self):
        api_bumx.total_coin = 0

    def _run_worker(self, api, *, job_types, action=None, **kwargs):
        if action is None:
            action = MagicMock()
            action.get_info.return_value = ("fb-uid", "OK")
            action.auto_reaction.return_value = False
            action.auto_cmt.return_value = False
        defaults = {
            "max_jobs": 30,
            "like_batch_min": 10,
            "like_batch_max": 10,
            "comment_batch_min": 5,
            "comment_batch_max": 5,
            "like_delay_range": (3, 3),
            "comment_delay_range": (4, 4),
            "failed_delay_range": (7, 7),
        }
        defaults.update(kwargs)
        with (
            patch.object(api_bumx, "AUTO_Action", return_value=action),
            patch.object(api_bumx, "get_facebook_post_id", side_effect=lambda url: url.rsplit("/", 1)[-1]),
            patch.object(api_bumx.time, "sleep") as sleep,
            patch.object(api_bumx, "countdown") as countdown,
            patch.object(api_bumx, "print_info"),
            patch.object(api_bumx, "print_warning"),
            patch.object(api_bumx, "print_error"),
            patch.object(api_bumx, "print_success"),
        ):
            done = api_bumx.run_ttc_jobs(
                api,
                "facebook-token",
                job_types=job_types,
                **defaults,
            )
        return done, action, sleep, countdown

    def test_run_multi_tokens_uses_ttc_lifecycle_and_original_reaction_job(self):
        original = make_original("reaction_vip_cheo", reaction="WOW")
        original_snapshot = copy.deepcopy(original)
        api = MagicMock()
        action = MagicMock()
        events = []
        api.login.side_effect = lambda: events.append("login")
        action.get_info.side_effect = lambda token: events.append("facebook_info") or ("998877", "OK")
        api.set_facebook_uid.side_effect = lambda uid: events.append("set_uid")
        api.fetch_jobs_for_legacy_type.side_effect = (
            lambda group: events.append("fetch") or fetched(wrap(original, "like_facebook"))
        )
        action.auto_reaction.side_effect = lambda *args: events.append("facebook_action") or True
        api.submit_job.side_effect = (
            lambda job: events.append("submit")
            or {"success": True, "earned_coin": "37", "message": "ok"}
        )

        real_scheduler = api_bumx.schedule_jobs_by_burst
        with (
            patch.object(api_bumx, "API_TTC", return_value=api) as ttc_class,
            patch.object(api_bumx, "API_BUMX") as bumx_class,
            patch.object(api_bumx, "AUTO_Action", return_value=action),
            patch.object(api_bumx, "get_facebook_post_id", return_value="10001"),
            patch.object(api_bumx, "schedule_jobs_by_burst", wraps=real_scheduler) as scheduler,
            patch.object(api_bumx.time, "sleep") as sleep,
            patch.object(api_bumx, "countdown") as countdown,
            patch.object(api_bumx, "print_info"),
            patch.object(api_bumx, "print_warning"),
            patch.object(api_bumx, "print_error"),
            patch.object(api_bumx, "print_success"),
        ):
            api_bumx.run_multi_tokens(
                "ttc-profile-token",
                [{"email": "first", "token": "facebook-token"}],
                max_jobs_per_acc=1,
                job_types={"like_facebook"},
                like_batch_min=1,
                like_batch_max=1,
                comment_batch_min=1,
                comment_batch_max=1,
                like_delay_min=0,
                like_delay_max=0,
                comment_delay_min=0,
                comment_delay_max=0,
                failed_delay_min=0,
                failed_delay_max=0,
                account_delay_min=0,
                account_delay_max=0,
                profile_id="profile-1",
            )

        ttc_class.assert_called_once_with("ttc-profile-token", worker_id="profile-1:1")
        bumx_class.assert_not_called()
        api.login.assert_called_once_with()
        action.get_info.assert_called_once_with("facebook-token")
        api.set_facebook_uid.assert_called_once_with("998877")
        api.fetch_jobs_for_legacy_type.assert_called_once_with("like_facebook")
        scheduler.assert_called_once()
        self.assertEqual(
            {job["type"] for job in scheduler.call_args.args[0]},
            {"like_facebook"},
        )
        action.auto_reaction.assert_called_once_with("10001", "WOW", "facebook-token", "998877")
        api.submit_job.assert_called_once_with(original)
        api.close.assert_called_once_with()
        self.assertEqual(
            events,
            ["login", "facebook_info", "set_uid", "fetch", "facebook_action", "submit"],
        )
        sleep.assert_called_once_with(3)
        countdown.assert_not_called()
        self.assertEqual(original, original_snapshot)
        self.assertEqual(api_bumx.total_coin, 37)

    def test_comment_failure_and_semantic_submit_failure_use_failed_delay(self):
        first = make_original("comment", "1", comment="first comment")
        second = make_original("comment", "2", comment="second comment")
        third = make_original("comment", "3", comment="third comment")
        api = MagicMock()
        api.fetch_jobs_for_legacy_type.return_value = fetched(
            wrap(first, "like_poster"),
            wrap(second, "like_poster"),
            wrap(third, "like_poster"),
        )
        api.submit_job.side_effect = [
            {"success": True, "earned_coin": 11},
            {"success": False, "earned_coin": 999},
        ]
        action = MagicMock()
        action.get_info.return_value = ("fb-uid", "OK")
        action.auto_cmt.side_effect = [False, True, True]

        done, action, sleep, countdown = self._run_worker(
            api,
            job_types={"like_poster"},
            action=action,
        )

        self.assertEqual(done, 1)
        self.assertEqual(api_bumx.total_coin, 11)
        self.assertEqual(
            action.auto_cmt.call_args_list,
            [
                call("10001", "first comment", "facebook-token", "fb-uid"),
                call("10002", "second comment", "facebook-token", "fb-uid"),
                call("10003", "third comment", "facebook-token", "fb-uid"),
            ],
        )
        self.assertEqual(api.submit_job.call_args_list, [call(second), call(third)])
        self.assertEqual(sleep.call_args_list, [call(3), call(3), call(3)])
        self.assertEqual(
            [args[0] for args, _kwargs in countdown.call_args_list],
            [7, 4],
        )

    def test_reaction_failure_does_not_submit_and_uses_failed_delay(self):
        first = make_original("like_post_vip_cheo", "1", reaction="LIKE")
        second = make_original("reaction_vip_re", "2", reaction="CARE")
        api = MagicMock()
        api.fetch_jobs_for_legacy_type.return_value = fetched(
            wrap(first, "like_facebook"),
            wrap(second, "like_facebook"),
        )
        api.submit_job.return_value = {"success": True, "earned_coin": 12}
        action = MagicMock()
        action.get_info.return_value = ("fb-uid", "OK")
        action.auto_reaction.side_effect = [False, True]

        done, _action, _sleep, countdown = self._run_worker(
            api,
            job_types={"like_facebook"},
            action=action,
        )

        self.assertEqual(done, 1)
        self.assertEqual(api_bumx.total_coin, 12)
        api.submit_job.assert_called_once_with(second)
        countdown.assert_called_once_with(7, unittest.mock.ANY)

    def test_no_job_uses_max_positive_countdown_then_retries_both_groups(self):
        original = make_original("comment", "3")
        api = MagicMock()
        responses = {
            "like_facebook": [
                fetched(no_job=[{"countdown": 5}, {"countdown": 17}]),
                fetched(no_job=[{"countdown": 2}]),
            ],
            "like_poster": [
                fetched(no_job=[{"countdown": 9}]),
                fetched(wrap(original, "like_poster")),
            ],
        }
        api.fetch_jobs_for_legacy_type.side_effect = lambda group: responses[group].pop(0)

        done, _action, _sleep, countdown = self._run_worker(
            api,
            job_types={"like_facebook", "like_poster"},
        )

        self.assertEqual(done, 0)
        self.assertEqual(
            api.fetch_jobs_for_legacy_type.call_args_list,
            [call("like_facebook"), call("like_poster"), call("like_facebook"), call("like_poster")],
        )
        countdown.assert_called_once_with(17, unittest.mock.ANY)

    def test_no_job_without_positive_countdown_uses_ten_seconds(self):
        original = make_original("like_post_vip_re", "4")
        api = MagicMock()
        api.fetch_jobs_for_legacy_type.side_effect = [
            fetched(no_job=[{"countdown": 0}, {"countdown": None}]),
            fetched(wrap(original, "like_facebook")),
        ]

        _done, _action, _sleep, countdown = self._run_worker(
            api,
            job_types={"like_facebook"},
        )

        countdown.assert_called_once_with(10, unittest.mock.ANY)

    def test_ttc_fetch_errors_propagate_instead_of_becoming_empty_jobs(self):
        for error in (
            TTCAuthError("bad token"),
            TTCRateLimitError("slow down"),
            TTCResponseError("bad response"),
            ValueError("bad adapter response"),
        ):
            with self.subTest(error=type(error).__name__):
                api = MagicMock()
                api.fetch_jobs_for_legacy_type.side_effect = error
                with self.assertRaises(type(error)) as caught:
                    self._run_worker(api, job_types={"like_poster"})
                self.assertIs(caught.exception, error)

    def test_reward_normalization_uses_zero_for_missing_or_invalid_values(self):
        self.assertEqual(api_bumx._normalize_ttc_earned_coin(None), 0)
        self.assertEqual(api_bumx._normalize_ttc_earned_coin(""), 0)
        self.assertEqual(api_bumx._normalize_ttc_earned_coin("invalid"), 0)
        self.assertEqual(api_bumx._normalize_ttc_earned_coin(True), 0)
        self.assertEqual(api_bumx._normalize_ttc_earned_coin(-5), 0)
        self.assertEqual(api_bumx._normalize_ttc_earned_coin("25"), 25)

    def test_invalid_facebook_identity_stops_before_uid_or_fetch(self):
        api = MagicMock()
        action = MagicMock()
        action.get_info.return_value = (None, "dead token")

        done, _action, _sleep, countdown = self._run_worker(
            api,
            job_types={"like_facebook"},
            action=action,
        )

        self.assertEqual(done, 0)
        api.set_facebook_uid.assert_not_called()
        api.fetch_jobs_for_legacy_type.assert_not_called()
        countdown.assert_not_called()

    def test_active_profile_passes_existing_auth_token_field_to_core(self):
        profile = {
            "profile_id": "profile-1",
            "name": "profile",
            "auth_token": "token-from-auth-token",
            "fb_accounts": [{"email": "a", "token": "facebook-token"}],
            "run_config": api_bumx.normalize_run_config(),
        }
        with (
            patch.object(api_bumx, "selected_accounts", return_value=profile["fb_accounts"]),
            patch.object(api_bumx, "profile_run_lock", return_value=contextlib.nullcontext()),
            patch.object(api_bumx, "run_multi_tokens") as run_multi,
            patch.object(api_bumx, "print_info"),
            patch.object(api_bumx, "print_error"),
        ):
            api_bumx.run_active_profile(profile)

        self.assertEqual(run_multi.call_args.args[0], "token-from-auth-token")
        self.assertIs(run_multi.call_args.args[1], profile["fb_accounts"])

    def test_multi_account_order_limit_burst_and_account_delay_are_preserved(self):
        originals = [make_original("like_post_vip_cheo", str(i)) for i in range(1, 5)]
        apis = [MagicMock(), MagicMock()]
        for api in apis:
            api.fetch_jobs_for_legacy_type.return_value = fetched(
                *(wrap(job, "like_facebook") for job in originals)
            )
            api.submit_job.return_value = {"success": True, "earned_coin": 1}
        action = MagicMock()
        action.get_info.side_effect = [("uid-a", "OK"), ("uid-b", "OK")]
        action.auto_reaction.return_value = True
        real_scheduler = api_bumx.schedule_jobs_by_burst

        with (
            patch.object(api_bumx, "API_TTC", side_effect=apis) as ttc_class,
            patch.object(api_bumx, "AUTO_Action", return_value=action),
            patch.object(api_bumx, "get_facebook_post_id", return_value="10001"),
            patch.object(api_bumx, "schedule_jobs_by_burst", wraps=real_scheduler) as scheduler,
            patch.object(api_bumx.time, "sleep"),
            patch.object(api_bumx.random, "randint", side_effect=lambda a, b: a),
            patch.object(api_bumx, "countdown") as countdown,
            patch.object(api_bumx, "print_info"),
            patch.object(api_bumx, "print_warning"),
            patch.object(api_bumx, "print_error"),
            patch.object(api_bumx, "print_success"),
        ):
            api_bumx.run_multi_tokens(
                "ttc-token",
                [{"email": "a", "token": "fb-a"}, {"email": "b", "token": "fb-b"}],
                max_jobs_per_acc=2,
                job_types={"like_facebook"},
                like_batch_min=1,
                like_batch_max=1,
                comment_batch_min=1,
                comment_batch_max=1,
                like_delay_min=3,
                like_delay_max=3,
                comment_delay_min=4,
                comment_delay_max=4,
                failed_delay_min=7,
                failed_delay_max=7,
                account_delay_min=13,
                account_delay_max=13,
                profile_id="p",
            )

        self.assertEqual(
            ttc_class.call_args_list,
            [call("ttc-token", worker_id="p:1"), call("ttc-token", worker_id="p:2")],
        )
        self.assertEqual(action.get_info.call_args_list, [call("fb-a"), call("fb-b")])
        self.assertTrue(all(api.submit_job.call_count == 2 for api in apis))
        self.assertTrue(all(api.close.call_count == 1 for api in apis))
        self.assertEqual(scheduler.call_count, 2)
        for scheduler_call in scheduler.call_args_list:
            self.assertEqual(scheduler_call.args[2], 2)
            self.assertEqual(scheduler_call.kwargs["like_batch_min"], 1)
            self.assertEqual(scheduler_call.kwargs["like_batch_max"], 1)
        self.assertEqual(
            [args[0] for args, _kwargs in countdown.call_args_list],
            [3, 13, 3],
        )

    def test_continuous_mode_reenters_ttc_acquisition_without_fixed_cycle_wait(self):
        original = make_original("like_post_vip_cheo", "1")
        first_api = MagicMock()
        first_api.fetch_jobs_for_legacy_type.return_value = fetched(wrap(original, "like_facebook"))
        action = MagicMock()
        action.get_info.return_value = ("uid", "OK")
        action.auto_reaction.return_value = False

        with (
            patch.object(api_bumx, "API_TTC", side_effect=[first_api, KeyboardInterrupt]) as ttc_class,
            patch.object(api_bumx, "AUTO_Action", return_value=action),
            patch.object(api_bumx, "get_facebook_post_id", return_value="10001"),
            patch.object(api_bumx.time, "sleep"),
            patch.object(api_bumx, "countdown") as countdown,
            patch.object(api_bumx, "print_info"),
            patch.object(api_bumx, "print_warning"),
            patch.object(api_bumx, "print_error"),
            patch.object(api_bumx, "print_success"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                api_bumx.run_multi_tokens(
                    "ttc-token",
                    [{"email": "a", "token": "fb"}],
                    continuous=True,
                    job_types={"like_facebook"},
                    like_batch_min=1,
                    like_batch_max=1,
                    comment_batch_min=1,
                    comment_batch_max=1,
                    like_delay_min=0,
                    like_delay_max=0,
                    comment_delay_min=0,
                    comment_delay_max=0,
                    failed_delay_min=0,
                    failed_delay_max=0,
                    account_delay_min=0,
                    account_delay_max=0,
                )

        self.assertEqual(ttc_class.call_count, 2)
        self.assertFalse(any(args and args[0] == 20 for args, _kwargs in countdown.call_args_list))
        first_api.close.assert_called_once_with()

    def test_ttc_client_closes_when_worker_fetch_raises(self):
        api = MagicMock()
        api.fetch_jobs_for_legacy_type.side_effect = TTCAuthError("expired")
        action = MagicMock()
        action.get_info.return_value = ("uid", "OK")
        with (
            patch.object(api_bumx, "API_TTC", return_value=api),
            patch.object(api_bumx, "AUTO_Action", return_value=action),
            patch.object(api_bumx, "print_info"),
            patch.object(api_bumx, "print_warning"),
            patch.object(api_bumx, "print_error") as print_error,
            patch.object(api_bumx, "print_success"),
        ):
            api_bumx.run_multi_tokens(
                "ttc-token",
                [{"email": "a", "token": "fb"}],
                job_types={"like_facebook"},
                like_batch_min=1,
                like_batch_max=1,
                comment_batch_min=1,
                comment_batch_max=1,
                like_delay_min=0,
                like_delay_max=0,
                comment_delay_min=0,
                comment_delay_max=0,
                failed_delay_min=0,
                failed_delay_max=0,
                account_delay_min=0,
                account_delay_max=0,
            )

        api.close.assert_called_once_with()
        self.assertIn("expired", print_error.call_args.args[0])

    def test_core_source_has_no_bumx_mission_report_or_fixed_waits(self):
        multi_source = inspect.getsource(api_bumx.run_multi_tokens)
        worker_source = inspect.getsource(api_bumx.run_ttc_jobs)
        self.assertIn("API_TTC(", multi_source)
        self.assertNotIn("API_BUMX(", multi_source)
        self.assertNotIn("countdown(20", multi_source)
        self.assertNotIn("API_BUMX(", inspect.getsource(api_bumx.check_balance))
        self.assertIn("API_TTC(", inspect.getsource(api_bumx.check_balance))
        self.assertNotIn("API_BUMX(", inspect.getsource(api_bumx.show_jobs_for_profile))
        self.assertIn("API_TTC(", inspect.getsource(api_bumx.show_jobs_for_profile))
        for forbidden in (
            "get_new_mission_jobs",
            "get_mission_jobs",
            "load_mission",
            ".REPORT(",
            ".submit(",
            "countdown(300",
            "coin_reward = 20",
            "coin_reward = 50",
        ):
            self.assertNotIn(forbidden, worker_source)


if __name__ == "__main__":
    unittest.main()
