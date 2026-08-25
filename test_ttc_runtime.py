import copy
import contextlib
import inspect
import unittest
from unittest.mock import MagicMock, call, patch

import api_bumx
from api_bumx import (
    TTCAuthError,
    TTCRateLimitError,
    TTCResponseError,
)


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


def fetched(*jobs, no_job=None, cooldowns=None, permanent_skips=None):
    return {
        "jobs": list(jobs),
        "no_job": list(no_job or []),
        "cooldowns": list(cooldowns or []),
        "permanent_skips": list(permanent_skips or []),
    }


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
        self.assertEqual(sleep.call_args_list, [call(3), call(5), call(5)])
        self.assertEqual(
            [args[0] for args, _kwargs in countdown.call_args_list],
            [7, 4],
        )

    def test_rejected_comment_claim_waits_then_next_comment_starts_normally(self):
        first = make_original("comment", "1", comment="first comment")
        second = make_original("comment", "2", comment="second comment")
        api = MagicMock()
        api.fetch_jobs_for_legacy_type.return_value = fetched(
            wrap(first, "like_poster"),
            wrap(second, "like_poster"),
        )
        api.submit_job.side_effect = [
            {
                "success": False,
                "reason": "TTC_REJECTED",
                "retryable": False,
            },
            {"success": True, "earned_coin": 9},
        ]
        action = MagicMock()
        action.get_info.return_value = ("fb-uid", "OK")
        action.auto_cmt.return_value = True

        done, action, sleep, countdown = self._run_worker(
            api,
            job_types={"like_poster"},
            action=action,
        )

        self.assertEqual(done, 1)
        self.assertEqual(action.auto_cmt.call_count, 2)
        self.assertEqual(api.submit_job.call_args_list, [call(first), call(second)])
        self.assertEqual(sleep.call_args_list, [call(5), call(5)])
        countdown.assert_called_once_with(7, unittest.mock.ANY)

    def test_pending_comment_skips_claim_and_uses_only_remaining_capacity_for_like(self):
        initial_likes = [
            make_original("reaction", str(index), reaction="LIKE")
            for index in range(1, 11)
        ]
        comments = [
            make_original("comment", str(index), comment=f"comment {index}")
            for index in range(11, 21)
        ]
        fallback_likes = [
            make_original("reaction", str(index), reaction="LOVE")
            for index in range(21, 31)
        ]
        api = MagicMock()
        like_fetches = iter((
            fetched(*(wrap(job, "like_facebook") for job in initial_likes)),
            fetched(*(wrap(job, "like_facebook") for job in fallback_likes)),
        ))
        api.fetch_jobs_for_legacy_type.side_effect = lambda group: (
            next(like_fetches)
            if group == "like_facebook"
            else fetched(*(wrap(job, "like_poster") for job in comments))
        )
        api.submit_job.return_value = {"success": True, "earned_coin": 1}
        action = MagicMock()
        action.get_info.return_value = ("fb-uid", "OK")
        action.auto_reaction.return_value = True
        action.auto_cmt.return_value = True
        action.last_comment_evidence = {
            "visibility_scalars": {"comment_privacy_value": "PENDING_APPROVAL"}
        }

        with (
            patch.object(api_bumx, "AUTO_Action", return_value=action),
            patch.object(api_bumx, "get_facebook_post_id", return_value="10001"),
            patch.object(api_bumx.time, "sleep"),
            patch.object(api_bumx, "countdown"),
            patch.object(api_bumx, "print_info") as info,
            patch.object(api_bumx, "print_warning") as warning,
            patch.object(api_bumx, "print_error"),
            patch.object(api_bumx, "print_success"),
            patch("builtins.print") as raw_output,
        ):
            done = api_bumx.run_ttc_jobs(
                api,
                "facebook-token",
                max_jobs=10,
                job_types={"like_facebook", "like_poster"},
                like_batch_min=3,
                like_batch_max=3,
                comment_batch_min=10,
                comment_batch_max=10,
                like_delay_range=(0, 0),
                comment_delay_range=(0, 0),
                failed_delay_range=(0, 0),
            )

        self.assertEqual(done, 10)
        self.assertEqual(api_bumx.total_coin, 10)
        action.auto_cmt.assert_called_once_with(
            "10001", "comment 11", "facebook-token", "fb-uid"
        )
        self.assertEqual(action.auto_reaction.call_count, 10)
        self.assertTrue(all(
            job["action_group"] == "reaction"
            for job in (call.args[0] for call in api.submit_job.call_args_list)
        ))
        self.assertEqual(api.submit_job.call_count, 10)
        self.assertEqual(api.fetch_jobs_for_legacy_type.call_args_list.count(call("like_facebook")), 2)
        self.assertIn("Acc yếu bị Facebook ẩn comment.", [
            item.args[0] for item in warning.call_args_list
        ])
        self.assertIn("Đang lấy job Like...", [
            item.args[0] for item in info.call_args_list
        ])
        for item in warning.call_args_list + info.call_args_list:
            self.assertFalse(str(item.args[0]).startswith(("⚠️", "ℹ️", "✅", "❌", "⏳")))

    def test_only_exact_pending_privacy_value_activates_comment_fallback(self):
        self.assertTrue(api_bumx._is_comment_pending_approval({
            "visibility_scalars": {"comment_privacy_value": "PENDING_APPROVAL"}
        }))
        for value in ("PENDING_APPROVAL ", "DEFAULT", None, True):
            with self.subTest(value=value):
                self.assertFalse(api_bumx._is_comment_pending_approval({
                    "visibility_scalars": {
                        "comment_privacy_value": value,
                        "spam_display_mode": "none",
                    }
                }))

    def test_pending_comment_is_scoped_to_one_account_turn_and_next_cycle_retries(self):
        def make_api(comment_jobs, like_jobs=()):
            api = MagicMock()
            api.submit_job.return_value = {"success": True, "earned_coin": 1}
            api.fetch_jobs_for_legacy_type.side_effect = lambda group: (
                fetched(*(wrap(job, "like_facebook") for job in like_jobs))
                if group == "like_facebook"
                else fetched(*(wrap(job, "like_poster") for job in comment_jobs))
            )
            return api

        a_comments = [
            make_original("comment", str(index), comment=f"A comment {index}")
            for index in range(1, 11)
        ]
        api_a = make_api(a_comments, [make_original("reaction", "50")])
        api_b = make_api([make_original("comment", "60", comment="B comment")])
        api_c = make_api([make_original("comment", "70", comment="C comment")])
        api_d = make_api([make_original("comment", "80", comment="D comment")])
        api_a_next_cycle = make_api([
            make_original("comment", "90", comment="A comment next cycle")
        ])

        pending_action = MagicMock()
        pending_action.get_info.return_value = ("uid-a", "OK")
        pending_action.auto_cmt.return_value = True
        pending_action.last_comment_evidence = {
            "visibility_scalars": {"comment_privacy_value": "PENDING_APPROVAL"}
        }
        fallback_like_action = MagicMock()
        fallback_like_action.get_info.return_value = ("uid-a", "OK")
        fallback_like_action.auto_reaction.return_value = True

        def normal_comment_action(uid):
            action = MagicMock()
            action.get_info.return_value = (uid, "OK")
            action.auto_cmt.return_value = True
            action.last_comment_evidence = {"visibility_scalars": {}}
            return action

        b_action = normal_comment_action("uid-b")
        c_action = normal_comment_action("uid-c")
        d_action = normal_comment_action("uid-d")
        a_next_cycle_action = normal_comment_action("uid-a")
        accounts = [
            {"email": "A", "token": "token-a"},
            {"email": "B", "token": "token-b"},
            {"email": "C", "token": "token-c"},
            {"email": "D", "token": "token-d"},
        ]

        with (
            patch.object(
                api_bumx,
                "API_TTC",
                side_effect=[api_a, api_b, api_c, api_d, api_a_next_cycle, KeyboardInterrupt()],
            ),
            patch.object(
                api_bumx,
                "AUTO_Action",
                side_effect=[
                    pending_action,
                    fallback_like_action,
                    b_action,
                    c_action,
                    d_action,
                    a_next_cycle_action,
                ],
            ),
            patch.object(api_bumx, "get_facebook_post_id", return_value="10001"),
            patch.object(api_bumx.time, "sleep"),
            patch.object(api_bumx, "countdown"),
            patch.object(api_bumx, "print_info"),
            patch.object(api_bumx, "print_warning"),
            patch.object(api_bumx, "print_error"),
            patch.object(api_bumx, "print_success"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                api_bumx.run_multi_tokens(
                    "ttc-profile-token",
                    accounts,
                    max_jobs_per_acc=10,
                    continuous=True,
                    job_types={"like_poster"},
                    like_batch_min=1,
                    like_batch_max=1,
                    comment_batch_min=10,
                    comment_batch_max=10,
                    like_delay_min=0,
                    like_delay_max=0,
                    comment_delay_min=0,
                    comment_delay_max=0,
                    failed_delay_min=0,
                    failed_delay_max=0,
                    account_delay_min=0,
                    account_delay_max=0,
                )

        pending_action.auto_cmt.assert_called_once()
        fallback_like_action.auto_reaction.assert_called_once()
        self.assertEqual(api_a.submit_job.call_count, 1)
        b_action.auto_cmt.assert_called_once()
        c_action.auto_cmt.assert_called_once()
        d_action.auto_cmt.assert_called_once()
        a_next_cycle_action.auto_cmt.assert_called_once()
        self.assertEqual(api_a.submit_job.call_args[0][0]["action_group"], "reaction")

    def test_successful_comment_settles_five_seconds_before_claim(self):
        original = make_original("comment", "settle", comment="uy tín nha")
        api = MagicMock()
        api.fetch_jobs_for_legacy_type.return_value = fetched(
            wrap(original, "like_poster")
        )
        events = []

        def submit(job):
            events.append(("claim", job))
            return {"success": True, "earned_coin": 3}

        api.submit_job.side_effect = submit
        action = MagicMock()
        action.get_info.return_value = ("fb-uid", "OK")
        action.auto_cmt.side_effect = lambda *_args: events.append(("action", None)) or True
        action.last_comment_evidence = {
            "visibility_scalars": {"comment_privacy_value": "DEFAULT"}
        }

        with (
            patch.object(api_bumx, "AUTO_Action", return_value=action),
            patch.object(api_bumx, "get_facebook_post_id", return_value="10001"),
            patch.object(
                api_bumx.time,
                "sleep",
                side_effect=lambda seconds: events.append(("sleep", seconds)),
            ),
            patch.object(api_bumx, "countdown"),
            patch.object(api_bumx, "print_info"),
            patch.object(api_bumx, "print_warning"),
            patch.object(api_bumx, "print_error"),
            patch.object(api_bumx, "print_success"),
        ):
            done = api_bumx.run_ttc_jobs(
                api,
                "facebook-token",
                max_jobs=1,
                job_types={"like_poster"},
                comment_batch_min=1,
                comment_batch_max=1,
            )

        self.assertEqual(done, 1)
        self.assertEqual(
            events,
            [("action", None), ("sleep", 5), ("claim", original)],
        )

    def test_comment_claim_keeps_settle_and_claim_without_debug_audit_spam(self):
        original = make_original("comment", "timing", comment="exact text")
        api = MagicMock()
        api.fetch_jobs_for_legacy_type.return_value = fetched(
            wrap(original, "like_poster")
        )
        api.submit_job.return_value = {"success": True, "earned_coin": 1}
        action = MagicMock()
        action.get_info.return_value = ("fb-uid", "OK")
        action.auto_cmt.return_value = True
        action.last_comment_evidence = {
            "outgoing_text": "exact text",
            "returned_text": "exact text",
        }

        with (
            patch.object(api_bumx, "AUTO_Action", return_value=action),
            patch.object(api_bumx, "get_facebook_post_id", return_value="10001"),
            patch.object(api_bumx.time, "sleep"),
            patch.object(api_bumx, "countdown"),
            patch.object(api_bumx, "print_info") as info,
            patch.object(api_bumx, "print_warning") as warning,
            patch.object(api_bumx, "print_error") as error,
            patch.object(api_bumx, "print_success") as success,
        ):
            done = api_bumx.run_ttc_jobs(
                api,
                "facebook-token",
                max_jobs=1,
                job_types={"like_poster"},
                comment_batch_min=1,
                comment_batch_max=1,
            )

        messages = [str(item.args[0]) for item in info.call_args_list]
        self.assertEqual(done, 1)
        self.assertEqual(api.submit_job.call_args_list, [call(original)])
        self.assertFalse(any(
            "[COMMENT TIMING]" in message
            or "[COMMENT CLAIM REQUEST]" in message
            for message in messages
        ))

    def test_failed_claim_log_preserves_exact_server_body_and_separates_classification(self):
        original = make_original("comment", "raw", comment="hello")
        raw_body = '{\n  "error": "SOME TTC SERVER MESSAGE"\n}\n'
        api = MagicMock()
        api.fetch_jobs_for_legacy_type.return_value = fetched(
            wrap(original, "like_poster")
        )
        api.submit_job.return_value = {
            "success": False,
            "reason": "TTC_REJECTED",
            "message": "TTC returned an explicit structured failure",
            "retryable": False,
            "http_status": 200,
            "response_kind": "dict",
            "response_keys": ("error",),
            "server_raw_text": raw_body,
        }
        action = MagicMock()
        action.get_info.return_value = ("uid-a", "OK")
        action.auto_cmt.return_value = True

        with (
            patch.object(api_bumx, "AUTO_Action", return_value=action),
            patch.object(api_bumx, "get_facebook_post_id", return_value="10001"),
            patch.object(api_bumx.time, "sleep"),
            patch.object(api_bumx, "countdown"),
            patch.object(api_bumx, "print_info"),
            patch.object(api_bumx, "print_warning") as warning,
            patch.object(api_bumx, "print_error"),
            patch.object(api_bumx, "print_success"),
            patch("builtins.print") as raw_output,
        ):
            api_bumx.run_ttc_jobs(
                api,
                "facebook-token",
                max_jobs=1,
                job_types={"like_poster"},
                comment_batch_min=1,
                comment_batch_max=1,
                comment_delay_range=(0, 0),
                failed_delay_range=(0, 0),
            )

        messages = [str(item.args[0]) for item in warning.call_args_list]
        self.assertIn(call(raw_body), raw_output.call_args_list)
        self.assertIn("[TTC][CLAIM]", messages)
        self.assertIn("TTC không xác nhận job.", messages)
        self.assertFalse(any("reason=" in message for message in messages))

    def test_comment_pipeline_keeps_exact_payload_without_debug_spam(self):
        raw_nd = '["Anh ấy nói \\"xin chào\\" ❤️ / 100%"]'
        normalized = 'Anh ấy nói "xin chào" ❤️ / 100%'
        original = make_original("comment", "diagnostic", comment=normalized)
        original["raw_data"]["nd"] = raw_nd
        api = MagicMock()
        api.fetch_jobs_for_legacy_type.return_value = fetched(
            wrap(original, "like_poster")
        )
        action = MagicMock()
        action.get_info.return_value = ("uid-a", "OK")
        action.auto_cmt.return_value = False

        with (
            patch.object(api_bumx, "AUTO_Action", return_value=action),
            patch.object(api_bumx, "get_facebook_post_id", return_value="10001"),
            patch.object(api_bumx.time, "sleep"),
            patch.object(api_bumx, "countdown"),
            patch.object(api_bumx, "print_info") as info,
            patch.object(api_bumx, "print_warning") as warning,
            patch.object(api_bumx, "print_error") as error,
            patch.object(api_bumx, "print_success") as success,
        ):
            api_bumx.run_ttc_jobs(
                api,
                "facebook-token",
                max_jobs=1,
                job_types={"like_poster"},
                comment_batch_min=1,
                comment_batch_max=1,
                failed_delay_range=(0, 0),
            )

        messages = [str(item.args[0]) for logger in (info, warning, error, success)
                    for item in logger.call_args_list]
        debug_markers = (
            "[TTC COMMENT RAW ND]", "[TTC COMMENT DECODED]",
            "[FB COMMENT OUTGOING]", "[TTC COMMENT EXACT]",
            "[FB OUTGOING EXACT]", "[FB RETURNED EXACT]",
            "[FB COMMENT RESULT]", "[FB COMMENT VISIBILITY]",
            "[COMMENT CLAIM REQUEST]", "[COMMENT TIMING]", "sha256=",
            "comment_keys=", "visibility_fields=",
        )
        self.assertFalse(any(marker in message for marker in debug_markers for message in messages))
        action.auto_cmt.assert_called_once_with(
            "10001", normalized, "facebook-token", "uid-a"
        )

    def test_fetch_cooldown_log_preserves_server_body_and_separates_classification(self):
        raw_body = '{\n  "error": "Lấy nhiệm vụ sau 31 giây"\n}\n'
        comment = make_original("comment", "cooldown", comment="hello")
        api = MagicMock()
        api.fetch_jobs_for_legacy_type.side_effect = [
            fetched(cooldowns=[{
                "job_type": "ttc_like_post_vip_cheo",
                "retry_after": 31,
                "remaining": 31,
                "source": "server",
                "server_raw_text": raw_body,
            }]),
            fetched(wrap(comment, "like_poster")),
        ]
        action = MagicMock()
        action.get_info.return_value = ("uid-a", "OK")
        action.auto_cmt.return_value = False

        with (
            patch.object(api_bumx, "AUTO_Action", return_value=action),
            patch.object(api_bumx, "get_facebook_post_id", return_value="10001"),
            patch.object(api_bumx.time, "sleep"),
            patch.object(api_bumx, "countdown"),
            patch.object(api_bumx, "print_info") as info,
            patch.object(api_bumx, "print_warning") as warning,
            patch.object(api_bumx, "print_error"),
            patch.object(api_bumx, "print_success"),
            patch("builtins.print") as raw_output,
        ):
            api_bumx.run_ttc_jobs(
                api,
                "facebook-token",
                max_jobs=1,
                job_types={"like_facebook", "like_poster"},
                comment_batch_min=1,
                comment_batch_max=1,
                failed_delay_range=(0, 0),
            )

        self.assertIn(call(raw_body), raw_output.call_args_list)
        self.assertIn("[TTC][ttc_like_post_vip_cheo]", [
            str(item.args[0]) for item in warning.call_args_list
        ])
        self.assertIn(
            "Tạm chờ job này 31 giây.",
            [str(item.args[0]) for item in info.call_args_list],
        )

    def test_ttc_exception_logs_unknown_raw_body_without_wrapper(self):
        raw_body = 'line 1\nline  2\nline 3'
        exc = type("FakeTTCError", (), {
            "job_type": "ttc_unknown",
            "server_raw_text": raw_body,
        })()

        with (
            patch.object(api_bumx, "print_error") as error,
            patch.object(api_bumx, "print_warning") as warning,
            patch("builtins.print") as raw_output,
        ):
            handled = api_bumx.print_ttc_exception("TTC request failed.", exc)

        self.assertTrue(handled)
        self.assertEqual(error.call_args_list, [call("TTC request failed.")])
        self.assertEqual(warning.call_args_list, [call("[TTC][ttc_unknown]")])
        self.assertEqual(raw_output.call_args_list, [call(raw_body)])

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

    def test_facebook_timeout_is_clean_failure_and_next_job_continues(self):
        session = MagicMock()
        session.get.side_effect = api_bumx.requests.exceptions.ReadTimeout(
            "synthetic Facebook read timeout"
        )
        with (
            patch.object(api_bumx.requests, "Session", return_value=session),
            patch("builtins.print"),
        ):
            self.assertIsNone(
                api_bumx.get_facebook_post_id("https://www.facebook.com/first")
            )

        first = make_original("like_post_vip_cheo", "timeout", reaction="LIKE")
        second = make_original("reaction_vip_re", "next", reaction="CARE")
        api = MagicMock()
        api.fetch_jobs_for_legacy_type.return_value = fetched(
            wrap(first, "like_facebook"),
            wrap(second, "like_facebook"),
        )
        api.submit_job.return_value = {"success": True, "earned_coin": 4}
        action = MagicMock()
        action.get_info.return_value = ("fb-uid", "OK")
        action.auto_reaction.return_value = True

        with (
            patch.object(api_bumx, "AUTO_Action", return_value=action),
            patch.object(
                api_bumx,
                "get_facebook_post_id",
                side_effect=[None, "10002"],
            ),
            patch.object(api_bumx.time, "sleep"),
            patch.object(api_bumx, "countdown"),
            patch.object(api_bumx, "print_info"),
            patch.object(api_bumx, "print_warning"),
            patch.object(api_bumx, "print_error"),
            patch.object(api_bumx, "print_success"),
        ):
            done = api_bumx.run_ttc_jobs(
                api,
                "facebook-token",
                max_jobs=2,
                job_types={"like_facebook"},
                like_batch_min=2,
                like_batch_max=2,
                like_delay_range=(0, 0),
                failed_delay_range=(0, 0),
            )

        self.assertEqual(done, 1)
        action.auto_reaction.assert_called_once_with(
            "10002", "CARE", "facebook-token", "fb-uid"
        )
        api.submit_job.assert_called_once_with(second)

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

    def test_vip_ineligible_is_session_scoped_and_normal_comment_continues(self):
        comments = [
            make_original("comment", "1", comment="first"),
            make_original("comment", "2", comment="second"),
        ]
        first_api = MagicMock()
        second_api = MagicMock()

        def first_fetch(group):
            if group == "like_facebook":
                return fetched(permanent_skips=[{
                    "job_type": "ttc_like_post_vip_cheo",
                    "source": "server",
                    "server_raw_text": '{"error":"not eligible"}',
                }])
            return fetched(wrap(comments[0], "like_poster"))

        first_api.fetch_jobs_for_legacy_type.side_effect = first_fetch
        second_api.fetch_jobs_for_legacy_type.side_effect = lambda group: (
            fetched(permanent_skips=[{
                "job_type": "ttc_like_post_vip_cheo",
                "source": "local",
            }])
            if group == "like_facebook"
            else fetched(wrap(comments[1], "like_poster"))
        )
        first_api.submit_job.return_value = {"success": True, "earned_coin": 11}
        second_api.submit_job.return_value = {"success": True, "earned_coin": 13}
        action = MagicMock()
        action.get_info.return_value = ("uid-a", "OK")
        action.auto_cmt.return_value = True

        with (
            patch.object(api_bumx, "API_TTC", side_effect=[first_api, second_api, KeyboardInterrupt]),
            patch.object(api_bumx, "AUTO_Action", return_value=action),
            patch.object(api_bumx, "get_facebook_post_id", side_effect=lambda url: url.rsplit("/", 1)[-1]),
            patch.object(api_bumx.time, "sleep"),
            patch.object(api_bumx, "countdown"),
            patch.object(api_bumx, "print_info"),
            patch.object(api_bumx, "print_warning") as warning,
            patch.object(api_bumx, "print_error"),
            patch.object(api_bumx, "print_success"),
            patch("builtins.print") as raw_output,
        ):
            with self.assertRaises(KeyboardInterrupt):
                api_bumx.run_multi_tokens(
                    "ttc-token",
                    [{"email": "a", "token": "fb-a"}],
                    continuous=True,
                    job_types={"like_facebook", "like_poster"},
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

        self.assertEqual(
            first_api.fetch_jobs_for_legacy_type.call_args_list,
            [call("like_facebook"), call("like_poster")],
        )
        self.assertEqual(
            second_api.fetch_jobs_for_legacy_type.call_args_list,
            [call("like_facebook"), call("like_poster")],
        )
        self.assertEqual(first_api.submit_job.call_count, 1)
        self.assertEqual(second_api.submit_job.call_count, 1)
        self.assertEqual(action.auto_cmt.call_count, 2)
        self.assertEqual(api_bumx.total_coin, 24)
        self.assertIn(call('{"error":"not eligible"}'), raw_output.call_args_list)
        self.assertIn("[TTC][ttc_like_post_vip_cheo]", [
            item.args[0] for item in warning.call_args_list
        ])

    def test_endpoint_state_is_shared_within_run_and_reset_on_new_start(self):
        def run_once(account_count):
            apis = [MagicMock(name=f"api-{index}") for index in range(account_count)]
            for index, api in enumerate(apis):
                original = make_original("like_post_vip_cheo", str(index))
                api.fetch_jobs_for_legacy_type.return_value = fetched(
                    wrap(original, "like_facebook")
                )
            action = MagicMock()
            action.get_info.side_effect = [
                (f"uid-{index}", "OK") for index in range(account_count)
            ]
            action.auto_reaction.return_value = False
            with (
                patch.object(api_bumx, "API_TTC", side_effect=apis),
                patch.object(api_bumx, "AUTO_Action", return_value=action),
                patch.object(api_bumx, "get_facebook_post_id", return_value="10001"),
                patch.object(api_bumx.time, "sleep"),
                patch.object(api_bumx, "countdown"),
                patch.object(api_bumx, "print_info"),
                patch.object(api_bumx, "print_warning"),
                patch.object(api_bumx, "print_error"),
                patch.object(api_bumx, "print_success"),
            ):
                api_bumx.run_multi_tokens(
                    "ttc-token",
                    [
                        {"email": str(index), "token": f"fb-{index}"}
                        for index in range(account_count)
                    ],
                    continuous=False,
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
            return [api.use_fetch_state.call_args.args[0] for api in apis]

        first_run_states = run_once(2)
        second_run_states = run_once(1)

        self.assertIs(first_run_states[0], first_run_states[1])
        self.assertIsNot(first_run_states[0], second_run_states[0])

    def test_non_endpoint_fetch_errors_still_propagate(self):
        for error in (
            TTCAuthError("bad token"),
            TTCRateLimitError("slow down"),
            TTCResponseError("bad response"),
        ):
            with self.subTest(error=type(error).__name__):
                api = MagicMock()
                api.fetch_jobs_for_legacy_type.side_effect = error
                with self.assertRaises(type(error)):
                    self._run_worker(
                        api,
                        job_types={"like_facebook"},
                    )

    def test_endpoint_cooldown_and_no_job_metadata_drive_safe_backoff(self):
        comment = make_original("comment", "after-wait")
        api = MagicMock()
        api.fetch_jobs_for_legacy_type.side_effect = [
            fetched(cooldowns=[{
                "job_type": "ttc_like_post_vip_cheo",
                "remaining": 31,
                "source": "local",
            }]),
            fetched(no_job=[{"job_type": "ttc_comment", "countdown": 15}]),
            fetched(no_job=[{"job_type": "ttc_like_post_vip_re", "countdown": 7}]),
            fetched(wrap(comment, "like_poster")),
        ]

        done, _action, _sleep, countdown = self._run_worker(
            api,
            job_types={"like_facebook", "like_poster"},
        )

        self.assertEqual(done, 0)
        self.assertEqual(
            api.fetch_jobs_for_legacy_type.call_args_list,
            [
                call("like_facebook"),
                call("like_poster"),
                call("like_facebook"),
                call("like_poster"),
            ],
        )
        countdown.assert_called_once_with(31, unittest.mock.ANY)

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
        self.assertFalse(hasattr(api_bumx, "API_BUMX"))
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
