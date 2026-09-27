import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import api_ttc


class FakeAPI:
    def __init__(self, claim_result=None, jobs=None):
        self.claim_result = claim_result or {
            "success": True,
            "earned_coin": 600,
            "balance": 10600,
        }
        self.jobs = list(jobs if jobs is not None else [self._reaction_job()])
        self.submitted = []
        self.bound_uid = None

    @staticmethod
    def _reaction_job():
        original = {
            "type": "ttc_reaction_vip_re",
            "ttc_source": "ttc_reaction_vip_re",
            "job_id": "job-1",
            "idpost": "claim-1",
            "object_id": "object-123",
            "post_id": "456",
            "reaction_type": "LIKE",
        }
        return {"type": "like_facebook", "_ttc_job": original}

    def set_facebook_uid(self, uid):
        self.bound_uid = uid

    def bind_facebook_uid(self, uid):
        self.bound_uid = uid

    def fetch_jobs_for_legacy_type(self, _job_type):
        jobs, self.jobs = self.jobs, []
        return {
            "jobs": jobs,
            "no_job": [] if jobs else [{"countdown": 0}],
            "cooldowns": [],
            "permanent_skips": [],
        }

    def submit_job(self, job):
        self.submitted.append(job)
        return dict(self.claim_result)

    def get_account_info(self):
        return {"balance": 10000}


class FakeAction:
    def __init__(self, live_status=api_ttc.FACEBOOK_LIVE, action_success=True,
                 diagnostic="object id resolved\nauthorization=facebook-secret"):
        self.live_status = live_status
        self.action_success = action_success
        self.diagnostic = diagnostic
        self.log_fn = None
        self.last_reaction_evidence = {
            "success": bool(action_success),
            "classification": "SUCCESS" if action_success else "REJECTED",
            "attempts": 1,
        }

    def set_log_fn(self, log_fn):
        self.log_fn = log_fn

    def check_live(self, _token):
        if self.live_status == api_ttc.FACEBOOK_LIVE:
            return {
                "status": api_ttc.FACEBOOK_LIVE,
                "uid": "facebook-uid-1",
                "reason": "Facebook token LIVE",
            }
        return {
            "status": self.live_status,
            "uid": None,
            "reason": "Token đã hết hạn",
        }

    def auto_reaction(self, *_args):
        if self.log_fn is not None:
            self.log_fn(self.diagnostic, "raw")
        return self.action_success


class RuntimeLogTests(unittest.TestCase):
    def setUp(self):
        self.old_total_coin = api_ttc.total_coin
        api_ttc.total_coin = 0

    def tearDown(self):
        api_ttc.total_coin = self.old_total_coin

    def run_job(self, api, action, events=None):
        output = io.StringIO()
        callback = events.append if events is not None else None
        with patch.dict(os.environ, {api_ttc.RUNTIME_DEBUG_ENV: ""}), redirect_stdout(output):
            result = api_ttc.run_ttc_jobs(
                api,
                "facebook-secret-token",
                max_jobs=1,
                job_types={"like_facebook"},
                like_delay_range=(0, 0),
                failed_delay_range=(0, 0),
                action=action,
                event_callback=callback,
                transaction_sleep_fn=lambda _seconds: None,
            )
        return result, output.getvalue()

    def test_normal_success_is_one_final_line_without_technical_trace(self):
        events = []
        api = FakeAPI()

        done, rendered = self.run_job(api, FakeAction(), events)

        self.assertEqual(done, 1)
        self.assertEqual(len(api.submitted), 1)
        self.assertEqual(rendered.count("[1/1] LIKE"), 1)
        self.assertIn("[1/1] LIKE    | Thành công | +600 xu", rendered)
        self.assertNotIn("object id", rendered.casefold())
        self.assertNotIn("Post ID", rendered)
        self.assertNotIn("Đang xử lý", rendered)
        self.assertNotIn("claim", rendered.casefold())
        self.assertNotIn("facebook-secret", rendered)
        started = next(event for event in events if event["type"] == "job_started")
        action_result = next(
            event for event in events if event["type"] == "facebook_action_result"
        )
        self.assertEqual(started["message"], "")
        self.assertEqual(action_result["message"], "")

    def test_claim_failure_is_final_failure_not_intermediate_success(self):
        api = FakeAPI(claim_result={
            "success": False,
            "reason": "CLAIM_REJECTED",
            "message": "TTC không xác nhận",
            "server_raw_text": "raw claim response",
        })

        done, rendered = self.run_job(api, FakeAction())

        self.assertEqual(done, 0)
        self.assertEqual(len(api.submitted), 1)
        self.assertEqual(rendered.count("[1/1] LIKE"), 1)
        self.assertIn("[1/1] LIKE    | Thất bại", rendered)
        self.assertNotIn("Thành công", rendered)
        self.assertNotIn("TTC không xác nhận", rendered)
        self.assertNotIn("raw claim response", rendered)

    def test_actionable_account_error_remains_visible_in_normal_mode(self):
        api = FakeAPI()
        action = FakeAction(live_status=api_ttc.FACEBOOK_DIE_TOKEN)

        done, rendered = self.run_job(api, action)

        self.assertEqual(done, 0)
        self.assertIn("Facebook token/account không dùng được", rendered)
        self.assertIn("Token đã hết hạn", rendered)
        self.assertEqual(api.submitted, [])

    def test_actionable_ttc_error_hides_raw_payload_until_debug(self):
        error = api_ttc.TTCAuthError(
            "TTC token không hợp lệ\n"
            "[TTC SERVER][AUTH]\nraw authentication payload\n"
            "[TTC CLASS]\nAUTH_ERROR",
            job_type="AUTH",
            server_raw_text=(
                "raw authentication payload\n"
                "authorization=ttc-auth-secret"
            ),
        )
        normal = io.StringIO()
        debug = io.StringIO()

        with patch.dict(os.environ, {api_ttc.RUNTIME_DEBUG_ENV: ""}), redirect_stdout(normal):
            self.assertTrue(api_ttc.print_ttc_exception("Lỗi đăng nhập TTC.", error))
        with patch.dict(os.environ, {api_ttc.RUNTIME_DEBUG_ENV: "1"}), redirect_stdout(debug):
            self.assertTrue(api_ttc.print_ttc_exception("Lỗi đăng nhập TTC.", error))

        normal_text = normal.getvalue()
        debug_text = debug.getvalue()
        self.assertIn("TTC token không hợp lệ", normal_text)
        self.assertNotIn("raw authentication payload", normal_text)
        self.assertIn("raw authentication payload", debug_text)
        self.assertNotIn("ttc-auth-secret", debug_text)
        self.assertIn("authorization=***", debug_text)

    def test_debug_mode_restores_masked_diagnostics_and_keeps_user_result(self):
        api = FakeAPI(claim_result={
            "success": False,
            "reason": "CLAIM_REJECTED",
            "message": "claim debug detail",
            "server_raw_text": "raw response\nauthorization=claim-secret",
        })
        output = io.StringIO()

        with patch.dict(os.environ, {api_ttc.RUNTIME_DEBUG_ENV: "1"}), redirect_stdout(output):
            done = api_ttc.run_ttc_jobs(
                api,
                "facebook-secret-token",
                max_jobs=1,
                job_types={"like_facebook"},
                like_delay_range=(0, 0),
                failed_delay_range=(0, 0),
                action=FakeAction(),
                transaction_sleep_fn=lambda _seconds: None,
            )

        rendered = output.getvalue()
        self.assertEqual(done, 0)
        self.assertIn("Post ID: 456", rendered)
        self.assertIn("object id resolved", rendered)
        self.assertIn("claim debug detail", rendered)
        self.assertIn("raw response", rendered)
        self.assertIn("[1/1] LIKE    | Thất bại", rendered)
        self.assertNotIn("facebook-secret", rendered)
        self.assertNotIn("claim-secret", rendered)
        self.assertIn("authorization=***", rendered)

    def test_no_job_is_compact_and_does_not_show_fetch_attempts(self):
        api = FakeAPI(jobs=[])

        done, rendered = self.run_job(api, FakeAction())

        self.assertEqual(done, 0)
        self.assertEqual(rendered.count("Không có job phù hợp."), 1)
        self.assertNotIn("endpoint", rendered.casefold())
        self.assertNotIn("fetch", rendered.casefold())

    def test_missing_object_id_uses_existing_skip_path(self):
        job = FakeAPI._reaction_job()
        job["_ttc_job"]["object_id"] = ""
        api = FakeAPI(jobs=[job])

        done, rendered = self.run_job(api, FakeAction())

        self.assertEqual(done, 0)
        self.assertEqual(rendered.count("[1/1] LIKE"), 1)
        self.assertIn("[1/1] LIKE    | Bỏ qua", rendered)
        self.assertNotIn("thiếu object_id", rendered)
        self.assertEqual(api.submitted, [])

    def test_account_header_and_transition_do_not_render_secrets(self):
        accounts = [
            {"email": "account-one", "token": "secret-token-one"},
            {"email": "account-two", "token": "secret-token-two"},
        ]
        output = io.StringIO()

        with patch.dict(os.environ, {api_ttc.RUNTIME_DEBUG_ENV: ""}), patch.object(
            api_ttc, "API_TTC"
        ) as api_factory, patch.object(
            api_ttc, "run_ttc_jobs", side_effect=[2, 1]
        ), patch.object(
            api_ttc.time, "sleep"
        ), patch.object(
            api_ttc.random, "randint", return_value=12
        ), redirect_stdout(output):
            api_factory.return_value.use_fetch_state.return_value = None
            api_factory.return_value.login.return_value = {"balance": 10000}
            api_ttc.run_multi_tokens(
                "ttc-secret-token",
                accounts,
                max_jobs_per_acc=3,
                continuous=False,
                job_types={"like_facebook", "like_poster"},
                account_delay_min=12,
                account_delay_max=12,
            )

        rendered = output.getvalue()
        self.assertIn("Facebook: account-one", rendered)
        self.assertIn("Facebook: account-two", rendered)
        self.assertIn("Job: Like + Comment", rendered)
        self.assertIn("Chuyển account: 12s", rendered)
        self.assertIn("Hoàn thành chu kỳ 1, tổng job: 3", rendered)
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("ttc-secret", rendered)


if __name__ == "__main__":
    unittest.main()
