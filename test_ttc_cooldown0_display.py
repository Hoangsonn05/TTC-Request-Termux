import io
import json
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

import api_ttc


COOLDOWN_ZERO_MESSAGE = (
    "Lấy nhiệm vụ sau 0 giây, spam sẽ văng tài khoản, "
    "đi làm nhiệm vụ khác quay lại sau!"
)


class FakeResponse:
    def __init__(self, data):
        self.status_code = 200
        self.content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.headers = {"content-type": "application/json; charset=utf-8"}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"


class FakeSession:
    def __init__(self, data):
        self.data = data

    def get(self, _url, **_kwargs):
        return FakeResponse(self.data)


class FakeAction:
    reactions = []
    comments = []
    last_reaction_evidence = {"attempts": 1}
    last_comment_evidence = {}

    def auto_reaction(self, *args):
        type(self).reactions.append(args)
        return True

    def auto_cmt(self, *args):
        type(self).comments.append(args)
        return True


class FakeAPI:
    def __init__(self, fetch):
        self.fetch = fetch
        self.calls = []
        self.submits = []

    def bind_facebook_uid(self, uid):
        self.uid = uid

    def fetch_jobs_for_legacy_type(self, kind):
        self.calls.append(kind)
        return self.fetch(kind, self.calls.count(kind))

    def submit_job(self, job):
        self.submits.append(job)
        return {"success": True, "earned_coin": 400, "balance": 1024064}


def jobs_result(*jobs):
    return {
        "jobs": list(jobs),
        "no_job": [],
        "cooldowns": [],
        "permanent_skips": [],
    }


def cooldown_result(seconds=0):
    return {
        "jobs": [],
        "no_job": [],
        "cooldowns": [{
            "job_type": "ttc_comment",
            "retry_after": seconds,
            "remaining": seconds,
            "source": "server",
            "server_raw_text": COOLDOWN_ZERO_MESSAGE,
        }],
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
LIKE_JOB_2 = {
    "type": "like_facebook",
    "_ttc_job": {
        "job_id": "like-2",
        "object_id": "102",
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


class CooldownZeroAndDisplayTests(unittest.TestCase):
    def setUp(self):
        FakeAction.reactions.clear()
        FakeAction.comments.clear()

    def fetch_exception(self, message):
        client = api_ttc.TTCClient()
        client._authenticated = True
        client._configured_facebook_uid = "100"
        client._session = FakeSession({"error": message})
        with self.assertRaises(api_ttc.TTCError) as caught:
            client.fetch_jobs("ttc_comment")
        return caught.exception

    def run_jobs(self, api, job_types, countdown, max_jobs=1):
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
                action=FakeAction(),
                transaction_sleep_fn=lambda _seconds: None,
                configure_remote_uid=False,
                facebook_live_result={
                    "status": api_ttc.FACEBOOK_LIVE,
                    "uid": "100",
                    "reason": "ok",
                },
            )

    def rendered_row(self, coin, balance):
        output = io.StringIO()
        with redirect_stdout(output):
            api_ttc.print_job_row(
                1, "100", "200", "LIKE", coin, balance, True
            )
        return output.getvalue()

    def test_01_positive_cooldown_is_parsed_and_classified(self):
        self.assertEqual(api_ttc._job_cooldown_seconds(
            "Lấy nhiệm vụ sau 10 giây"
        ), 10)
        error = self.fetch_exception("Lấy nhiệm vụ sau 10 giây")
        self.assertIsInstance(error, api_ttc.TTCJobCooldown)
        self.assertEqual(error.retry_after, 10)

    def test_02_zero_cooldown_is_distinct_from_no_match(self):
        self.assertEqual(
            api_ttc._job_cooldown_seconds(COOLDOWN_ZERO_MESSAGE), 0
        )
        error = self.fetch_exception(COOLDOWN_ZERO_MESSAGE)
        self.assertIsInstance(error, api_ttc.TTCJobCooldown)
        self.assertNotIsInstance(error, api_ttc.TTCResponseError)
        self.assertEqual(error.retry_after, 0)

    def test_03_unknown_explicit_error_remains_response_error(self):
        self.assertIsNone(api_ttc._job_cooldown_seconds("Lỗi không xác định"))
        error = self.fetch_exception("Lỗi không xác định")
        self.assertIsInstance(error, api_ttc.TTCResponseError)
        self.assertNotIsInstance(error, api_ttc.TTCJobCooldown)

    def test_04_comment_zero_cooldown_retries_once_then_processes_job(self):
        api = FakeAPI(lambda _kind, count: (
            cooldown_result(0) if count == 1 else jobs_result(COMMENT_JOB)
        ))
        waits = []

        result = self.run_jobs(
            api,
            {"like_poster"},
            lambda seconds, label, *_args: waits.append((seconds, label)) or True,
        )

        self.assertEqual(result, 1)
        self.assertEqual(api.calls, ["like_poster", "like_poster"])
        self.assertEqual(waits, [(0, "Chờ Comment")])
        self.assertEqual(len(FakeAction.comments), 1)

    def test_05_refill_zero_cooldown_keeps_current_account_and_like(self):
        def fetch(kind, count):
            if kind == "like_facebook":
                return jobs_result(LIKE_JOB if count == 1 else LIKE_JOB_2)
            return jobs_result(COMMENT_JOB) if count == 1 else cooldown_result(0)

        api = FakeAPI(fetch)
        waits = []
        result = self.run_jobs(
            api,
            {"like_facebook", "like_poster"},
            lambda seconds, label, *_args: waits.append((seconds, label)) or True,
            max_jobs=3,
        )

        self.assertEqual(result, 3)
        self.assertEqual(
            api.calls,
            [
                "like_facebook", "like_poster",
                "like_facebook", "like_poster", "like_poster",
            ],
        )
        self.assertEqual(waits, [
            (0, "Chờ job tiếp theo"),
            (0, "Chờ job tiếp theo"),
            (0, "Chờ Comment"),
        ])
        self.assertEqual(len(FakeAction.reactions), 2)
        self.assertEqual(len(FakeAction.comments), 1)

    def test_06_comment_only_second_zero_cooldown_uses_normal_poll_delay(self):
        api = FakeAPI(lambda _kind, _count: cooldown_result(0))
        waits = []

        def countdown(seconds, label, *_args):
            waits.append((seconds, label))
            return label != "Chờ lấy job"

        result = self.run_jobs(api, {"like_poster"}, countdown)

        self.assertEqual(result, 0)
        self.assertEqual(api.calls, ["like_poster", "like_poster"])
        self.assertEqual(waits, [
            (0, "Chờ Comment"),
            (api_ttc.TTC_ACQUISITION_POLL_SECONDS, "Chờ lấy job"),
        ])

    def test_07_real_response_error_still_propagates(self):
        api = FakeAPI(lambda _kind, _count: (_ for _ in ()).throw(
            api_ttc.TTCResponseError("real response failure")
        ))

        with self.assertRaisesRegex(
            api_ttc.TTCResponseError, "real response failure"
        ):
            self.run_jobs(
                api, {"like_poster"},
                lambda *_args: self.fail("unexpected countdown"),
            )

        self.assertEqual(api.calls, ["like_poster"])

    def test_08_seven_digit_balance_is_not_truncated(self):
        output = self.rendered_row(400, 1024064)
        self.assertIn("1.024.064", output)
        self.assertNotIn("1.024.06 |", output)

    def test_09_nine_digit_balance_is_not_truncated(self):
        self.assertIn("123.456.789", self.rendered_row(400, 123456789))

    def test_10_none_balance_is_rendered_as_dash(self):
        self.assertIn("| -", self.rendered_row(400, None))

    def test_11_earned_coin_is_formatted_without_truncation(self):
        self.assertIn("+1.400", self.rendered_row(1400, 1024064))


if __name__ == "__main__":
    unittest.main()
