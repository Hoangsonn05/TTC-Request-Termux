import threading
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import api_ttc


NO_COMMENT = {
    "jobs": [],
    "no_job": [{"job_type": "ttc_comment", "countdown": 0}],
    "cooldowns": [],
    "permanent_skips": [],
}
NO_LIKE = {
    "jobs": [],
    "no_job": [{"job_type": "ttc_likepostvipcheo", "countdown": 0}],
    "cooldowns": [],
    "permanent_skips": [],
}
LIKE_JOB = {
    "type": "like_facebook",
    "_ttc_job": {
        "job_id": "like-1",
        "object_id": "101",
        "reaction_type": "LIKE",
        "ttc_source": "ttc_likepostvipcheo",
    },
}
COMMENT_JOB = {
    "type": "like_poster",
    "_ttc_job": {
        "job_id": "comment-1",
        "object_id": "202",
        "comment_text": "ok",
        "ttc_source": "ttc_comment",
    },
}
COMMENT_JOB_2 = {
    "type": "like_poster",
    "_ttc_job": {
        "job_id": "comment-2",
        "object_id": "203",
        "comment_text": "ok 2",
        "ttc_source": "ttc_comment",
    },
}


def actual_result(*jobs):
    return {
        "jobs": list(jobs),
        "no_job": [],
        "cooldowns": [],
        "permanent_skips": [],
    }


def comment_cooldown(seconds=5):
    return {
        "jobs": [],
        "no_job": [],
        "cooldowns": [{
            "job_type": "ttc_comment",
            "remaining": seconds,
            "source": "local",
        }],
        "permanent_skips": [],
    }


class FakeAction:
    reactions = []
    comments = []
    last_reaction_evidence = {"attempts": 1}
    last_comment_evidence = {}

    def check_live(self, _token):
        return {"status": api_ttc.FACEBOOK_LIVE, "uid": "100", "reason": "ok"}

    def auto_reaction(self, *args):
        type(self).reactions.append(args)
        return True

    def auto_cmt(self, *args):
        type(self).comments.append(args)
        return True

    def close(self):
        return None


class PendingCommentAction(FakeAction):
    def auto_cmt(self, *args):
        result = super().auto_cmt(*args)
        self.last_comment_evidence = {
            "visibility_scalars": {
                "comment_privacy_value": "PENDING_APPROVAL",
            },
        }
        return result


class FakeAPI:
    def __init__(self, fetch):
        self.fetch = fetch
        self.calls = []
        self.submits = []

    def bind_facebook_uid(self, uid):
        self.uid = uid

    def set_facebook_uid(self, uid):
        self.uid = uid

    def get_account_info(self):
        return {"balance": 0}

    def fetch_jobs_for_legacy_type(self, kind):
        self.calls.append(kind)
        return self.fetch(kind, self.calls.count(kind))

    def submit_job(self, job):
        self.submits.append(job)
        return {"success": True, "earned_coin": 1, "balance": 1}


class CommentRetryTests(unittest.TestCase):
    def setUp(self):
        FakeAction.reactions.clear()
        FakeAction.comments.clear()

    def run_one(self, api, job_types, max_jobs=1, countdown=None,
                action=None, stop_event=None):
        if countdown is None:
            countdown = lambda *_args, **_kwargs: self.fail("unexpected countdown")
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                api_ttc, "_cooperative_countdown", side_effect=countdown
            ))
            stack.enter_context(patch.object(
                api_ttc,
                "_resolve_ttc_facebook_post_id",
                side_effect=lambda job, log_fn=None: job["object_id"],
            ))
            stack.enter_context(patch.object(
                api_ttc.random, "shuffle", side_effect=lambda _items: None
            ))
            stack.enter_context(patch("builtins.print"))
            return api_ttc.run_ttc_jobs(
                api,
                "fb-token",
                max_jobs=max_jobs,
                job_types=job_types,
                like_delay_range=(0, 0),
                comment_delay_range=(0, 0),
                failed_delay_range=(0, 0),
                action=action or FakeAction(),
                transaction_sleep_fn=lambda _seconds: None,
                configure_remote_uid=False,
                facebook_live_result={
                    "status": api_ttc.FACEBOOK_LIVE,
                    "uid": "100",
                    "reason": "ok",
                },
                stop_event=stop_event,
            )

    def test_01_comment_first_fetch_has_job(self):
        api = FakeAPI(lambda _kind, _count: actual_result(COMMENT_JOB))

        self.assertEqual(self.run_one(api, {"like_poster"}), 1)
        self.assertEqual(api.calls, ["like_poster"])
        self.assertEqual(len(FakeAction.comments), 1)
        self.assertEqual(len(api.submits), 1)

    def test_02_comment_no_job_then_job(self):
        api = FakeAPI(lambda _kind, count: (
            dict(NO_COMMENT) if count == 1 else actual_result(COMMENT_JOB)
        ))
        waits = []

        self.assertEqual(
            self.run_one(
                api,
                {"like_poster"},
                countdown=lambda seconds, label, *_args: waits.append(
                    (seconds, label)
                ) or True,
            ),
            1,
        )
        self.assertEqual(api.calls, ["like_poster", "like_poster"])
        self.assertEqual(
            waits,
            [(api_ttc.TTC_ACQUISITION_POLL_SECONDS, "Chờ Comment")],
        )
        self.assertEqual(len(FakeAction.comments), 1)

    def test_03_two_comment_misses_do_not_delay_available_like(self):
        def fetch(kind, _count):
            return actual_result(LIKE_JOB) if kind == "like_facebook" else dict(NO_COMMENT)

        api = FakeAPI(fetch)
        waits = []

        self.assertEqual(
            self.run_one(
                api,
                {"like_facebook", "like_poster"},
                countdown=lambda seconds, label, *_args: waits.append(
                    (seconds, label)
                ) or True,
            ),
            1,
        )
        self.assertEqual(
            api.calls,
            ["like_facebook", "like_poster", "like_poster"],
        )
        self.assertEqual(
            waits,
            [(api_ttc.TTC_ACQUISITION_POLL_SECONDS, "Chờ Comment")],
        )
        self.assertEqual(len(FakeAction.reactions), 1)
        self.assertEqual(len(FakeAction.comments), 0)

    def test_04_comment_cooldown_then_job(self):
        api = FakeAPI(lambda _kind, count: (
            comment_cooldown(5) if count == 1 else actual_result(COMMENT_JOB)
        ))
        waits = []

        self.assertEqual(
            self.run_one(
                api,
                {"like_poster"},
                countdown=lambda seconds, label, *_args: waits.append(
                    (seconds, label)
                ) or True,
            ),
            1,
        )
        self.assertEqual(api.calls, ["like_poster", "like_poster"])
        self.assertEqual(waits, [(5, "Chờ Comment")])
        self.assertEqual(len(FakeAction.comments), 1)

    def test_05_second_comment_cooldown_yields_cycle_without_second_cooldown_wait(self):
        api = FakeAPI(lambda _kind, _count: comment_cooldown(5))
        waits = []

        def countdown(seconds, label, *_args):
            waits.append((seconds, label))
            return label != "Chờ lấy job"

        self.assertEqual(
            self.run_one(api, {"like_poster"}, countdown=countdown),
            0,
        )
        self.assertEqual(api.calls, ["like_poster", "like_poster"])
        self.assertEqual(
            waits,
            [(5, "Chờ Comment"),
             (api_ttc.TTC_ACQUISITION_POLL_SECONDS, "Chờ lấy job")],
        )

    def test_06_comment_only_two_misses_use_normal_poll_wait(self):
        api = FakeAPI(lambda _kind, _count: dict(NO_COMMENT))
        waits = []

        def countdown(seconds, label, *_args):
            waits.append((seconds, label))
            return label != "Chờ lấy job"

        self.assertEqual(
            self.run_one(api, {"like_poster"}, countdown=countdown),
            0,
        )
        self.assertEqual(api.calls, ["like_poster", "like_poster"])
        self.assertEqual(
            waits,
            [(api_ttc.TTC_ACQUISITION_POLL_SECONDS, "Chờ Comment"),
             (api_ttc.TTC_ACQUISITION_POLL_SECONDS, "Chờ lấy job")],
        )

    def test_07_next_acquisition_cycle_resets_comment_retry_budget(self):
        api = FakeAPI(lambda _kind, count: (
            dict(NO_COMMENT) if count <= 2 else actual_result(COMMENT_JOB)
        ))
        waits = []

        self.assertEqual(
            self.run_one(
                api,
                {"like_poster"},
                countdown=lambda seconds, label, *_args: waits.append(
                    (seconds, label)
                ) or True,
            ),
            1,
        )
        self.assertEqual(
            api.calls,
            ["like_poster", "like_poster", "like_poster"],
        )
        self.assertEqual(
            waits,
            [(api_ttc.TTC_ACQUISITION_POLL_SECONDS, "Chờ Comment"),
             (api_ttc.TTC_ACQUISITION_POLL_SECONDS, "Chờ lấy job")],
        )
        self.assertEqual(len(FakeAction.comments), 1)

    def test_08_response_error_on_first_attempt_propagates(self):
        def fetch(_kind, _count):
            raise api_ttc.TTCResponseError("first response failure")

        api = FakeAPI(fetch)
        with self.assertRaisesRegex(api_ttc.TTCResponseError, "first response failure"):
            self.run_one(api, {"like_poster"})
        self.assertEqual(api.calls, ["like_poster"])

    def test_09_response_error_on_retry_propagates(self):
        def fetch(_kind, count):
            if count == 1:
                return dict(NO_COMMENT)
            raise api_ttc.TTCResponseError("retry response failure")

        api = FakeAPI(fetch)
        waits = []
        with self.assertRaisesRegex(api_ttc.TTCResponseError, "retry response failure"):
            self.run_one(
                api,
                {"like_poster"},
                countdown=lambda seconds, label, *_args: waits.append(
                    (seconds, label)
                ) or True,
            )
        self.assertEqual(api.calls, ["like_poster", "like_poster"])
        self.assertEqual(len(waits), 1)

    def test_10_stop_during_first_wait_prevents_retry(self):
        api = FakeAPI(lambda _kind, _count: dict(NO_COMMENT))
        stop_event = threading.Event()

        def stop_during_wait(_seconds, _label, *_args):
            stop_event.set()
            return False

        self.assertEqual(
            self.run_one(
                api,
                {"like_poster"},
                countdown=stop_during_wait,
                stop_event=stop_event,
            ),
            0,
        )
        self.assertEqual(api.calls, ["like_poster"])

    def test_11_pending_approval_still_disables_comment_for_account_run(self):
        api = FakeAPI(lambda _kind, _count: actual_result(
            COMMENT_JOB, COMMENT_JOB_2
        ))

        self.assertEqual(
            self.run_one(
                api,
                {"like_poster"},
                max_jobs=2,
                action=PendingCommentAction(),
            ),
            0,
        )
        self.assertEqual(api.calls, ["like_poster"])
        self.assertEqual(len(FakeAction.comments), 1)
        self.assertEqual(api.submits, [])

    def test_12_like_acquisition_semantics_are_unchanged(self):
        api = FakeAPI(lambda _kind, count: (
            dict(NO_LIKE) if count == 1 else actual_result(LIKE_JOB)
        ))
        waits = []

        self.assertEqual(
            self.run_one(
                api,
                {"like_facebook"},
                countdown=lambda seconds, label, *_args: waits.append(
                    (seconds, label)
                ) or True,
            ),
            1,
        )
        self.assertEqual(api.calls, ["like_facebook", "like_facebook"])
        self.assertEqual(
            waits,
            [(api_ttc.TTC_ACQUISITION_POLL_SECONDS, "Chờ lấy job")],
        )
        self.assertEqual(len(FakeAction.reactions), 1)


if __name__ == "__main__":
    unittest.main()
