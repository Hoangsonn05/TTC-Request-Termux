import copy
import inspect
import json
import time
import unittest
from unittest.mock import patch

import api_bumx
from TTC_API import (
    TTCAuthError,
    TTCClient,
    TTCJobType,
    TTCNoJobAvailable,
    TTCRateLimitError,
    TTCResponseError,
)
from ttc_adapter import API_TTC


_MISSING = object()


class FakeFetchResponse:
    def __init__(self, payload=_MISSING, *, status_code=200, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = (
            json.dumps(payload, ensure_ascii=False)
            if text is None and payload is not _MISSING
            else (text or "")
        )

    def json(self):
        if self._payload is _MISSING:
            raise ValueError("response is not valid JSON")
        return self._payload


class FakeFetchSession:
    def __init__(self, response):
        self.response = response
        self.get_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.response


class FakeAuthSession:
    def __init__(self, response):
        self.response = response
        self.post_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.response

    def close(self):
        pass


def make_job(job_type, suffix="1"):
    return {
        "job_id": f"{job_type.value}:{suffix}",
        "buff_id": f"{job_type.value}:{suffix}",
        "type": job_type.value,
        "action_group": "comment" if job_type is TTCJobType.COMMENT else "reaction",
        "object_id": f"object-{suffix}",
        "link": f"https://facebook.com/object-{suffix}",
        "reaction_type": "" if job_type is TTCJobType.COMMENT else "LOVE",
        "comment_text": "hello" if job_type is TTCJobType.COMMENT else "",
        "ttc_source": job_type.value,
        "idpost": f"post-{suffix}",
        "raw_data": {"idpost": f"post-{suffix}"},
    }


class TTCAuthenticationBalanceTests(unittest.TestCase):
    def make_client(self, payload):
        client = TTCClient(worker_id="auth-contract", log_fn=lambda *_args: None)
        client._session.close()
        client._session = FakeAuthSession(FakeFetchResponse(payload))
        return client

    def test_integer_balance_is_accepted(self):
        client = self.make_client({"data": {"user": "tester", "sodu": 123}})

        client.authenticate("valid-token")

        self.assertEqual(client.get_account_info(), {
            "user": "tester",
            "balance": 123,
            "authenticated": True,
        })

    def test_integer_like_string_balance_is_normalized(self):
        client = self.make_client({"data": {"user": "tester", "sodu": "123"}})

        client.authenticate("valid-token")

        self.assertEqual(client.get_account_info(), {
            "user": "tester",
            "balance": 123,
            "authenticated": True,
        })

    def test_invalid_required_balances_fail_without_account_state(self):
        cases = (
            ("malformed", {"user": "tester", "sodu": "not-a-number"}),
            ("missing", {"user": "tester"}),
            ("null", {"user": "tester", "sodu": None}),
        )
        for name, account_data in cases:
            with self.subTest(name=name):
                client = self.make_client({"data": account_data})

                with self.assertRaises(TTCResponseError):
                    client.authenticate("valid-token")

                self.assertEqual(client.get_account_info(), {
                    "user": "",
                    "balance": None,
                    "authenticated": False,
                })

    def test_failed_subsequent_login_clears_previous_account_state(self):
        client = self.make_client({"data": {"user": "first-user", "sodu": 456}})
        client.authenticate("first-token")
        client._session.response = FakeFetchResponse({
            "data": {"user": "second-user", "sodu": "not-a-number"}
        })

        with self.assertRaises(TTCResponseError):
            client.authenticate("second-token")

        self.assertEqual(client.get_account_info(), {
            "user": "",
            "balance": None,
            "authenticated": False,
        })


class TTCFetchParserTests(unittest.TestCase):
    def make_client(self, response):
        client = TTCClient(worker_id="fetch-contract", log_fn=lambda *_args: None)
        client._session.close()
        client._session = FakeFetchSession(response)
        client._authenticated = True
        return client

    def test_valid_jobs_are_normalized(self):
        client = self.make_client(FakeFetchResponse([
            {
                "idpost": "post-1",
                "link": "https://facebook.com/10001",
                "nd": "hello TTC",
            }
        ]))

        jobs = client.get_jobs(TTCJobType.COMMENT)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["type"], TTCJobType.COMMENT.value)
        self.assertEqual(jobs[0]["idpost"], "post-1")
        self.assertEqual(jobs[0]["comment_text"], "hello TTC")

    def test_empty_list_is_no_job(self):
        client = self.make_client(FakeFetchResponse([]))

        with self.assertRaises(TTCNoJobAvailable) as caught:
            client.get_jobs(TTCJobType.COMMENT)

        self.assertEqual(caught.exception.countdown, 0)

    def test_semantic_auth_failure_is_auth_error(self):
        client = self.make_client(FakeFetchResponse({"message": "Login expired"}))

        with self.assertRaises(TTCAuthError):
            client.get_jobs(TTCJobType.COMMENT)

    def test_rate_limit_message_is_rate_limit_error(self):
        client = self.make_client(FakeFetchResponse({"message": "Rate limit exceeded"}))

        with self.assertRaises(TTCRateLimitError):
            client.get_jobs(TTCJobType.COMMENT)

    def test_rate_limit_countdown_does_not_become_no_job(self):
        client = self.make_client(FakeFetchResponse({
            "message": "Rate limit exceeded",
            "countdown": 15,
        }))

        with self.assertRaises(TTCRateLimitError):
            client.get_jobs(TTCJobType.COMMENT)

    def test_internal_server_error_message_is_response_error(self):
        client = self.make_client(FakeFetchResponse({"message": "Internal server error"}))

        with self.assertRaises(TTCResponseError):
            client.get_jobs(TTCJobType.COMMENT)

    def test_explicit_error_field_is_response_error(self):
        client = self.make_client(FakeFetchResponse({"error": "database unavailable"}))

        with self.assertRaises(TTCResponseError):
            client.get_jobs(TTCJobType.COMMENT)

    def test_unknown_dict_is_response_error(self):
        client = self.make_client(FakeFetchResponse({"unexpected": "value"}))

        with self.assertRaises(TTCResponseError):
            client.get_jobs(TTCJobType.COMMENT)

    def test_non_json_and_empty_body_are_response_errors(self):
        for text in ("<html>server error</html>", ""):
            with self.subTest(text=text):
                client = self.make_client(FakeFetchResponse(text=text))
                with self.assertRaises(TTCResponseError):
                    client.get_jobs(TTCJobType.COMMENT)

    def test_http_status_error_precedence(self):
        cases = (
            (401, TTCAuthError),
            (403, TTCAuthError),
            (429, TTCRateLimitError),
            (500, TTCResponseError),
            (503, TTCResponseError),
            (400, TTCResponseError),
        )
        for status_code, expected_error in cases:
            with self.subTest(status_code=status_code):
                client = self.make_client(FakeFetchResponse(
                    {"message": "not a no-job response"},
                    status_code=status_code,
                ))
                with self.assertRaises(expected_error):
                    client.get_jobs(TTCJobType.COMMENT)


class APITTCAdapterTests(unittest.TestCase):
    def setUp(self):
        client_patcher = patch("ttc_adapter.TTCClient", autospec=True)
        self.addCleanup(client_patcher.stop)
        self.client_class = client_patcher.start()
        self.client = self.client_class.return_value
        self.api = API_TTC("ttc-token", worker_id="worker-7")

    def test_construct_login_set_uid_account_info_and_close(self):
        self.client_class.assert_called_once_with(worker_id="worker-7")
        self.client.login.assert_not_called()

        account = {"user": "tester", "balance": 100, "authenticated": True}
        self.client.login.return_value = account
        self.assertIs(self.api.login(), account)
        self.client.login.assert_called_once_with("ttc-token")

        self.api.set_facebook_uid("123456")
        self.client.set_uid.assert_called_once_with("123456")

        self.client.get_account_info.return_value = account
        self.assertIs(self.api.get_account_info(), account)
        self.client.get_account_info.assert_called_once_with()

        self.api.close()
        self.client.close.assert_called_once_with()

    def test_like_fetch_uses_only_four_supported_reaction_endpoints(self):
        jobs_by_type = {
            job_type: [make_job(job_type, str(index))]
            for index, job_type in enumerate(API_TTC.REACTION_JOB_TYPES, start=1)
        }
        self.client.get_jobs.side_effect = lambda job_type: jobs_by_type[job_type]

        result = self.api.fetch_jobs_for_legacy_type("like_facebook")

        self.assertEqual(
            [call.args[0] for call in self.client.get_jobs.call_args_list],
            [
                TTCJobType.LIKE_POST_VIP_CHEO,
                TTCJobType.LIKE_POST_VIP_RE,
                TTCJobType.REACTION_VIP_CHEO,
                TTCJobType.REACTION_VIP_RE,
            ],
        )
        self.assertEqual(len(result["jobs"]), 4)
        self.assertTrue(all(job["type"] == "like_facebook" for job in result["jobs"]))
        self.assertEqual(result["no_job"], [])
        self.assertNotIn(TTCJobType.FOLLOW, [call.args[0] for call in self.client.get_jobs.call_args_list])
        self.assertNotIn(TTCJobType.LIKE_PAGE, [call.args[0] for call in self.client.get_jobs.call_args_list])

    def test_comment_fetch_uses_only_comment_endpoint(self):
        original = make_job(TTCJobType.COMMENT)
        self.client.get_jobs.return_value = [original]

        result = self.api.fetch_jobs_for_legacy_type("like_poster")

        self.client.get_jobs.assert_called_once_with(TTCJobType.COMMENT)
        self.assertEqual(result["jobs"][0]["type"], "like_poster")
        self.assertIs(result["jobs"][0]["_ttc_job"], original)

    def test_partial_no_job_keeps_countdown_and_other_jobs(self):
        available = make_job(TTCJobType.REACTION_VIP_CHEO)

        def get_jobs(job_type):
            if job_type is TTCJobType.REACTION_VIP_CHEO:
                return [available]
            raise TTCNoJobAvailable(f"none for {job_type.value}", 17)

        self.client.get_jobs.side_effect = get_jobs

        result = self.api.fetch_jobs_for_legacy_type("like_facebook")

        self.assertEqual(len(result["jobs"]), 1)
        self.assertIs(result["jobs"][0]["_ttc_job"], available)
        self.assertEqual(
            result["no_job"],
            [
                {"job_type": TTCJobType.LIKE_POST_VIP_CHEO.value, "countdown": 17},
                {"job_type": TTCJobType.LIKE_POST_VIP_RE.value, "countdown": 17},
                {"job_type": TTCJobType.REACTION_VIP_RE.value, "countdown": 17},
            ],
        )

    def test_all_no_job_returns_metadata_without_sleeping(self):
        self.client.get_jobs.side_effect = lambda job_type: (_ for _ in ()).throw(
            TTCNoJobAvailable("temporarily empty", 29)
        )

        with patch.object(time, "sleep") as sleep:
            result = self.api.fetch_jobs_for_legacy_type("like_facebook")

        self.assertEqual(result["jobs"], [])
        self.assertEqual(len(result["no_job"]), 4)
        self.assertTrue(all(item["countdown"] == 29 for item in result["no_job"]))
        sleep.assert_not_called()

    def test_real_ttc_api_errors_propagate(self):
        for error in (
            TTCAuthError("bad token"),
            TTCRateLimitError("slow down"),
            TTCResponseError("bad response"),
        ):
            with self.subTest(error=type(error).__name__):
                self.client.reset_mock()
                self.client.get_jobs.side_effect = error
                with self.assertRaises(type(error)) as caught:
                    self.api.fetch_jobs_for_legacy_type("like_poster")
                self.assertIs(caught.exception, error)

    def test_wrapper_does_not_mutate_original_and_submit_unwraps_it(self):
        original = make_job(TTCJobType.REACTION_VIP_RE)
        original_snapshot = copy.deepcopy(original)
        self.client.get_jobs.return_value = [original]

        wrapper = self.api.fetch_jobs_for_legacy_type("like_facebook")["jobs"][0]

        self.assertEqual(original, original_snapshot)
        self.assertIsNot(wrapper, original)
        self.assertIs(wrapper["_ttc_job"], original)
        self.assertEqual(wrapper["type"], "like_facebook")
        self.client.submit_job.return_value = {"success": True}
        self.assertEqual(self.api.submit_job(wrapper), {"success": True})
        self.client.submit_job.assert_called_once_with(original)

    def test_submit_accepts_original_ttc_job(self):
        original = make_job(TTCJobType.COMMENT)
        self.api.submit_job(original)
        self.client.submit_job.assert_called_once_with(original)

    def test_unsupported_legacy_type_is_rejected_without_fetch(self):
        with self.assertRaises(ValueError):
            self.api.fetch_jobs_for_legacy_type("follow")
        self.client.get_jobs.assert_not_called()

    def test_main_runtime_constructs_api_ttc_after_core_cutover(self):
        source = inspect.getsource(api_bumx.run_multi_tokens)
        self.assertIn("api = API_TTC(", source)
        self.assertNotIn("api = API_BUMX(", source)
        self.assertIs(api_bumx.API_TTC, API_TTC)


if __name__ == "__main__":
    unittest.main()
