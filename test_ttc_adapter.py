import copy
import inspect
import json
import time
import unittest
from unittest.mock import patch

import api_ttc
from api_ttc import (
    API_TTC,
    TTCAuthError,
    TTCClient,
    TTCFetchState,
    TTCJobCooldown,
    TTCJobType,
    TTCNoJobAvailable,
    TTCRateLimitError,
    TTCResponseError,
    TTCVipIneligibleError,
)


_MISSING = object()


class FakeFetchResponse:
    def __init__(
        self,
        payload=_MISSING,
        *,
        status_code=200,
        text=None,
        content_type="application/json; charset=utf-8",
    ):
        self.status_code = status_code
        self._payload = payload
        self.text = (
            json.dumps(payload, ensure_ascii=False)
            if text is None and payload is not _MISSING
            else (text or "")
        )
        self.content = self.text.encode("utf-8")
        self.headers = {"content-type": content_type}

    def json(self):
        if self._payload is _MISSING:
            raise ValueError("response is not valid JSON")
        return self._payload


class FakeFetchSession:
    def __init__(self, response, post_response=None):
        self.response = response
        self.post_response = post_response
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.response

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_response or FakeFetchResponse(text="1", content_type="text/plain")


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
        client.configure_facebook_uid("parser-fixture-uid")
        return client

    @staticmethod
    def valid_comment_job(suffix="1"):
        return {
            "idpost": f"post-{suffix}",
            "link": f"https://facebook.com/1000{suffix}",
            "nd": "hello TTC",
        }

    @staticmethod
    def valid_job_for_type(job_type, suffix="1"):
        raw_job = {
            "idpost": f"post-{suffix}",
            "link": f"https://facebook.com/1000{suffix}",
        }
        if job_type in (TTCJobType.REACTION_VIP_CHEO, TTCJobType.REACTION_VIP_RE):
            raw_job["loaicx"] = "LOVE"
        if job_type is TTCJobType.COMMENT:
            raw_job["nd"] = "hello TTC"
        return raw_job

    def test_valid_jobs_are_normalized(self):
        client = self.make_client(FakeFetchResponse([self.valid_comment_job()]))

        jobs = client.get_jobs(TTCJobType.COMMENT)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["type"], TTCJobType.COMMENT.value)
        self.assertEqual(jobs[0]["idpost"], "post-1")
        self.assertEqual(jobs[0]["comment_text"], "hello TTC")

    def test_multiple_valid_jobs_are_normalized_without_loss(self):
        client = self.make_client(FakeFetchResponse([
            self.valid_comment_job("1"),
            self.valid_comment_job("2"),
        ]))

        jobs = client.get_jobs(TTCJobType.COMMENT)

        self.assertEqual(len(jobs), 2)
        self.assertEqual([job["idpost"] for job in jobs], ["post-1", "post-2"])

    def test_successful_fetch_preserves_exact_multiline_response_body(self):
        raw_body = (
            '[\n  {"idpost":"post-1","link":"https://facebook.com/10001",'
            '"nd":"hello"}\n]\n'
        )
        client = self.make_client(FakeFetchResponse(text=raw_body))

        job = client.get_jobs(TTCJobType.COMMENT)[0]

        self.assertEqual(job["server_raw_text"], raw_body)
        self.assertEqual(job["http_status"], 200)
        self.assertEqual(job["content_type"], "application/json; charset=utf-8")

    def test_all_supported_jobs_are_executable_and_claimable_after_normalization(self):
        executor = api_ttc.AUTO_Action()
        self.addCleanup(executor._requests.close)
        supported_types = (
            TTCJobType.LIKE_POST_VIP_CHEO,
            TTCJobType.LIKE_POST_VIP_RE,
            TTCJobType.REACTION_VIP_CHEO,
            TTCJobType.REACTION_VIP_RE,
            TTCJobType.COMMENT,
        )
        for job_type in supported_types:
            with self.subTest(job_type=job_type.value):
                client = self.make_client(FakeFetchResponse([
                    self.valid_job_for_type(job_type)
                ]))

                job = client.get_jobs(job_type)[0]

                self.assertTrue(job["object_id"])
                self.assertTrue(job["idpost"])
                client.claim_reward(job)
                claim_payload = client._session.post_calls[-1][1]["data"]
                self.assertEqual(claim_payload["id"], job["idpost"])
                if job_type is TTCJobType.COMMENT:
                    self.assertTrue(job["comment_text"].strip())
                else:
                    self.assertIn(job["reaction_type"], executor.id_reaction)
                if job_type in (
                    TTCJobType.REACTION_VIP_CHEO,
                    TTCJobType.REACTION_VIP_RE,
                ):
                    self.assertEqual(claim_payload["loaicx"], job["reaction_type"])

    def test_like_post_without_loaicx_remains_an_intrinsic_like(self):
        for job_type in (
            TTCJobType.LIKE_POST_VIP_CHEO,
            TTCJobType.LIKE_POST_VIP_RE,
        ):
            with self.subTest(job_type=job_type.value):
                client = self.make_client(FakeFetchResponse([
                    self.valid_job_for_type(job_type)
                ]))

                job = client.get_jobs(job_type)[0]

                self.assertEqual(job["reaction_type"], "LIKE")

    def test_reaction_requires_supported_explicit_loaicx(self):
        invalid_values = (
            ("missing", _MISSING),
            ("null", None),
            ("blank", ""),
            ("whitespace", "   \t"),
            ("unsupported", "NOT_A_REACTION"),
        )
        for job_type in (
            TTCJobType.REACTION_VIP_CHEO,
            TTCJobType.REACTION_VIP_RE,
        ):
            for name, loaicx in invalid_values:
                with self.subTest(job_type=job_type.value, value=name):
                    raw_job = self.valid_job_for_type(job_type)
                    if loaicx is _MISSING:
                        raw_job.pop("loaicx")
                    else:
                        raw_job["loaicx"] = loaicx
                    client = self.make_client(FakeFetchResponse([raw_job]))

                    with self.assertRaisesRegex(
                        TTCResponseError,
                        r"Malformed TTC reaction job at index 0: missing/unsupported loaicx",
                    ):
                        client.get_jobs(job_type)

    def test_mixed_valid_and_invalid_reaction_fails_the_whole_list(self):
        invalid_job = self.valid_job_for_type(TTCJobType.REACTION_VIP_CHEO, "2")
        invalid_job.pop("loaicx")
        client = self.make_client(FakeFetchResponse([
            self.valid_job_for_type(TTCJobType.REACTION_VIP_CHEO, "1"),
            invalid_job,
        ]))

        with self.assertRaisesRegex(
            TTCResponseError,
            r"Malformed TTC reaction job at index 1: missing/unsupported loaicx",
        ):
            client.get_jobs(TTCJobType.REACTION_VIP_CHEO)

    def test_valid_multi_reaction_batch_is_canonicalized_without_loss(self):
        raw_values = (" like ", "love", "CARE", "haha", "Wow", "sad", "angry")
        expected = ["LIKE", "LOVE", "CARE", "HAHA", "WOW", "SAD", "ANGRY"]
        raw_jobs = []
        for index, raw_value in enumerate(raw_values, start=1):
            raw_job = self.valid_job_for_type(TTCJobType.REACTION_VIP_RE, str(index))
            raw_job["loaicx"] = raw_value
            raw_jobs.append(raw_job)
        client = self.make_client(FakeFetchResponse(raw_jobs))

        jobs = client.get_jobs(TTCJobType.REACTION_VIP_RE)

        self.assertEqual([job["reaction_type"] for job in jobs], expected)
        self.assertEqual(
            [job["raw_data"]["loaicx"] for job in jobs],
            list(raw_values),
        )

    def test_valid_reaction_claim_uses_normalized_loaicx(self):
        raw_job = self.valid_job_for_type(TTCJobType.REACTION_VIP_CHEO)
        raw_job["loaicx"] = " love "
        client = self.make_client(FakeFetchResponse([raw_job]))

        job = client.get_jobs(TTCJobType.REACTION_VIP_CHEO)[0]
        client.claim_reward(job)

        self.assertEqual(job["reaction_type"], "LOVE")
        self.assertEqual(job["raw_data"]["loaicx"], " love ")
        self.assertEqual(client._session.post_calls[-1][1]["data"]["loaicx"], "LOVE")

    def test_link_target_without_idpost_is_response_error(self):
        client = self.make_client(FakeFetchResponse([{
            "link": "https://facebook.com/10001",
        }]))

        with self.assertRaisesRegex(TTCResponseError, r"index 0.*idpost"):
            client.get_jobs(TTCJobType.LIKE_POST_VIP_CHEO)

    def test_idpost_without_target_object_is_response_error(self):
        client = self.make_client(FakeFetchResponse([{
            "idpost": "post-1",
        }]))

        with self.assertRaisesRegex(TTCResponseError, r"index 0.*object_id"):
            client.get_jobs(TTCJobType.LIKE_POST_VIP_CHEO)

    def test_comment_without_nd_is_response_error(self):
        for name, raw_job in (
            ("missing", {"idpost": "post-1", "link": "https://facebook.com/10001"}),
            ("null", {"idpost": "post-1", "link": "https://facebook.com/10001", "nd": None}),
        ):
            with self.subTest(name=name):
                client = self.make_client(FakeFetchResponse([raw_job]))

                with self.assertRaisesRegex(TTCResponseError, r"index 0.*comment_text"):
                    client.get_jobs(TTCJobType.COMMENT)

    def test_blank_comment_is_response_error(self):
        for comment_text in ("", "   \t"):
            with self.subTest(comment_text=comment_text):
                client = self.make_client(FakeFetchResponse([{
                    "idpost": "post-1",
                    "link": "https://facebook.com/10001",
                    "nd": comment_text,
                }]))

                with self.assertRaisesRegex(TTCResponseError, r"index 0.*comment_text"):
                    client.get_jobs(TTCJobType.COMMENT)

    def test_mixed_valid_and_missing_idpost_is_response_error(self):
        client = self.make_client(FakeFetchResponse([
            self.valid_job_for_type(TTCJobType.LIKE_POST_VIP_CHEO),
            {"link": "https://facebook.com/10002"},
        ]))

        with self.assertRaisesRegex(TTCResponseError, r"index 1.*idpost"):
            client.get_jobs(TTCJobType.LIKE_POST_VIP_CHEO)

    def test_mixed_valid_and_invalid_comment_is_response_error(self):
        client = self.make_client(FakeFetchResponse([
            self.valid_comment_job(),
            {"idpost": "post-2", "link": "https://facebook.com/10002"},
        ]))

        with self.assertRaisesRegex(TTCResponseError, r"index 1.*comment_text"):
            client.get_jobs(TTCJobType.COMMENT)

    def test_empty_object_job_is_response_error(self):
        client = self.make_client(FakeFetchResponse([{}]))

        with self.assertRaisesRegex(TTCResponseError, r"Malformed TTC job at index 0"):
            client.get_jobs(TTCJobType.COMMENT)

    def test_non_empty_malformed_list_never_returns_partial_result(self):
        cases = (
            ("valid_then_empty", [self.valid_comment_job(), {}], 1),
            ("empty_then_valid", [{}, self.valid_comment_job()], 0),
            ("non_dict", [None], 0),
            ("missing_identifier", [{"idpost": "", "link": ""}], 0),
        )
        for name, payload, malformed_index in cases:
            with self.subTest(name=name):
                client = self.make_client(FakeFetchResponse(payload))

                with self.assertRaisesRegex(
                    TTCResponseError,
                    rf"Malformed TTC job at index {malformed_index}",
                ):
                    client.get_jobs(TTCJobType.COMMENT)

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

    def test_vip_ineligible_message_has_dedicated_classification(self):
        phrase = (
            "  Nick chưa đủ điều kiện   làm nhiệm vụ VIP, hãy đổi ảnh đại diện "
            "khác và đợi 15 ngày sau.  "
        )
        for payload in ({"message": phrase}, {"error": phrase}):
            with self.subTest(response_key=next(iter(payload))):
                client = self.make_client(FakeFetchResponse(payload))
                with self.assertRaises(TTCVipIneligibleError):
                    client.get_jobs(TTCJobType.LIKE_POST_VIP_CHEO)

    def test_countdown_message_is_typed_endpoint_cooldown(self):
        client = self.make_client(FakeFetchResponse({
            "message": (
                "Lấy nhiệm vụ sau 28 giây, spam sẽ văng tài khoản, "
                "đi làm nhiệm vụ khác quay lại sau!"
            ),
            "countdown": 28,
        }))

        with self.assertRaises(TTCJobCooldown) as caught:
            client.get_jobs(TTCJobType.LIKE_POST_VIP_CHEO)
        self.assertNotIsInstance(caught.exception, TTCVipIneligibleError)
        self.assertEqual(caught.exception.retry_after, 28)

    def test_job_cooldown_preserves_exact_raw_response_and_metadata(self):
        raw_body = (
            '{"error":"Lấy nhiệm vụ sau 31 giây, spam sẽ văng tài khoản, '
            'đi làm nhiệm vụ khác quay lại sau!"}'
        )
        client = self.make_client(FakeFetchResponse(text=raw_body))

        with self.assertRaises(TTCJobCooldown) as caught:
            client.get_jobs(TTCJobType.LIKE_POST_VIP_CHEO)

        error = caught.exception
        self.assertEqual(error.retry_after, 31)
        self.assertEqual(error.job_type, TTCJobType.LIKE_POST_VIP_CHEO.value)
        self.assertEqual(error.http_status, 200)
        self.assertEqual(error.content_type, "application/json; charset=utf-8")
        self.assertEqual(error.server_raw_text, raw_body)

    def test_generic_fetch_error_preserves_multiline_raw_response(self):
        raw_body = '{\n  "error": "SOME TTC SERVER MESSAGE"\n}\n'
        client = self.make_client(FakeFetchResponse(text=raw_body))

        with self.assertRaises(TTCResponseError) as caught:
            client.get_jobs(TTCJobType.COMMENT)

        self.assertEqual(caught.exception.server_raw_text, raw_body)

    def test_comment_claim_failure_preserves_raw_and_reference_payload(self):
        raw_body = '{"error":"SOME TTC SERVER MESSAGE"}'
        client = self.make_client(FakeFetchResponse([self.valid_comment_job()]))
        client._session.post_response = FakeFetchResponse(text=raw_body)
        job = client.get_jobs(TTCJobType.COMMENT)[0]

        result = client.claim_reward(job)

        self.assertIs(result["success"], False)
        self.assertEqual(result["server_raw_text"], raw_body)
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["content_type"], "application/json; charset=utf-8")
        claim_url, claim_kwargs = client._session.post_calls[-1]
        self.assertTrue(claim_url.endswith("/kiemtien/cmtcheo/nhantien.php"))
        self.assertEqual(claim_kwargs["data"], {"id": job["idpost"]})

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
        client_patcher = patch("api_ttc.TTCClient", autospec=True)
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
        self.assertFalse(hasattr(TTCJobType, "FOLLOW"))
        self.assertFalse(hasattr(TTCJobType, "LIKE_PAGE"))

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

    def test_endpoint_cooldown_continues_and_skips_only_until_expiry(self):
        first_type = TTCJobType.LIKE_POST_VIP_CHEO
        calls_for_first = 0

        def get_jobs(job_type):
            nonlocal calls_for_first
            if job_type is first_type:
                calls_for_first += 1
                if calls_for_first == 1:
                    raise TTCJobCooldown(
                        "cooldown",
                        job_type=job_type.value,
                        retry_after=31,
                        http_status=200,
                        server_raw_text='{"error":"Lấy nhiệm vụ sau 31 giây"}',
                    )
            raise TTCNoJobAvailable("none", 7)

        self.client.get_jobs.side_effect = get_jobs
        self.api.set_facebook_uid("uid-a")

        with patch("api_ttc.time.monotonic", side_effect=(100.0, 120.0, 132.0)):
            first = self.api.fetch_jobs_for_legacy_type("like_facebook")
            calls_after_first = list(self.client.get_jobs.call_args_list)
            second = self.api.fetch_jobs_for_legacy_type("like_facebook")
            calls_after_second = list(self.client.get_jobs.call_args_list)
            third = self.api.fetch_jobs_for_legacy_type("like_facebook")

        self.assertEqual(
            [item.args[0] for item in calls_after_first],
            list(API_TTC.REACTION_JOB_TYPES),
        )
        self.assertEqual(first["cooldowns"][0]["retry_after"], 31)
        self.assertEqual(
            [item.args[0] for item in calls_after_second[len(calls_after_first):]],
            list(API_TTC.REACTION_JOB_TYPES[1:]),
        )
        self.assertEqual(second["cooldowns"][0]["remaining"], 11)
        self.assertEqual(
            [item.args[0] for item in self.client.get_jobs.call_args_list[len(calls_after_second):]],
            list(API_TTC.REACTION_JOB_TYPES),
        )
        self.assertEqual(third["cooldowns"], [])

    def test_permanent_ineligible_skips_only_exact_endpoint_and_uid(self):
        blocked_type = TTCJobType.REACTION_VIP_CHEO
        blocked_once = False

        def get_jobs(job_type):
            nonlocal blocked_once
            if job_type is blocked_type and not blocked_once:
                blocked_once = True
                raise TTCVipIneligibleError(
                    "not eligible",
                    job_type=job_type.value,
                    http_status=200,
                    server_raw_text='{"error":"Nick chưa đủ điều kiện làm nhiệm vụ vip"}',
                )
            raise TTCNoJobAvailable("none", 5)

        self.client.get_jobs.side_effect = get_jobs
        self.api.set_facebook_uid("uid-a")
        first = self.api.fetch_jobs_for_legacy_type("like_facebook")
        first_count = self.client.get_jobs.call_count
        self.api.fetch_jobs_for_legacy_type("like_facebook")
        second_calls = self.client.get_jobs.call_args_list[first_count:]

        self.assertEqual(first["permanent_skips"][0]["job_type"], blocked_type.value)
        self.assertEqual([item.args[0] for item in second_calls], [
            TTCJobType.LIKE_POST_VIP_CHEO,
            TTCJobType.LIKE_POST_VIP_RE,
            TTCJobType.REACTION_VIP_RE,
        ])

        self.api.set_facebook_uid("uid-b")
        before_uid_b = self.client.get_jobs.call_count
        self.api.fetch_jobs_for_legacy_type("like_facebook")
        self.assertEqual(
            [item.args[0] for item in self.client.get_jobs.call_args_list[before_uid_b:]],
            list(API_TTC.REACTION_JOB_TYPES),
        )

    def test_first_vip_ineligible_continues_remaining_three_endpoints(self):
        first_type = TTCJobType.LIKE_POST_VIP_CHEO

        def get_jobs(job_type):
            if job_type is first_type:
                raise TTCVipIneligibleError(
                    "not eligible",
                    job_type=job_type.value,
                    server_raw_text='{"error":"not eligible"}',
                )
            raise TTCNoJobAvailable("none", 5)

        self.client.get_jobs.side_effect = get_jobs
        self.api.set_facebook_uid("uid-a")

        result = self.api.fetch_jobs_for_legacy_type("like_facebook")

        self.assertEqual(
            [item.args[0] for item in self.client.get_jobs.call_args_list],
            list(API_TTC.REACTION_JOB_TYPES),
        )
        self.assertEqual(
            result["permanent_skips"][0]["job_type"],
            first_type.value,
        )

    def test_new_fetch_state_resets_permanent_endpoint_skip(self):
        stale_state = TTCFetchState()
        stale_state.permanent_skip.add(("uid-a", TTCJobType.LIKE_POST_VIP_CHEO.value))
        stale_api = API_TTC("ttc-token", fetch_state=stale_state)
        stale_api._client = self.client
        stale_api.set_facebook_uid("uid-a")
        self.client.get_jobs.side_effect = TTCNoJobAvailable("none", 1)
        stale_api.fetch_jobs_for_legacy_type("like_facebook")
        self.assertEqual(
            [item.args[0] for item in self.client.get_jobs.call_args_list],
            list(API_TTC.REACTION_JOB_TYPES[1:]),
        )

        self.client.reset_mock()
        fresh_api = API_TTC("ttc-token", fetch_state=TTCFetchState())
        fresh_api._client = self.client
        fresh_api.set_facebook_uid("uid-a")
        self.client.get_jobs.side_effect = TTCNoJobAvailable("none", 1)

        fresh_api.fetch_jobs_for_legacy_type("like_facebook")

        self.assertEqual(
            [item.args[0] for item in self.client.get_jobs.call_args_list],
            list(API_TTC.REACTION_JOB_TYPES),
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
        source = inspect.getsource(api_ttc.run_multi_tokens)
        self.assertIn("api = API_TTC(", source)
        self.assertNotIn("api = API_BUMX(", source)
        self.assertIs(api_ttc.API_TTC, API_TTC)


if __name__ == "__main__":
    unittest.main()
