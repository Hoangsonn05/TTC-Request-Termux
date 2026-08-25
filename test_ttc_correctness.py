import json
import inspect
import os
import tempfile
import unittest
from unittest.mock import patch

import requests

import api_ttc
from api_ttc import (
    TTCAuthError,
    TTCClient,
    TTCJobType,
    TTCRateLimitError,
    TTCResponseError,
    normalize_jobs_list,
)


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, text=None, content_type="application/json"):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False) if text is None else text
        self.content = self.text.encode("utf-8")
        self.headers = {"content-type": content_type}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.post_calls = []
        self.cookies = requests.cookies.RequestsCookieJar()
        self.closed = False

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


def raw_job(job_type, *, loaicx="LOVE", comment="hello TTC", idpost="post-1", link="https://facebook.com/10001"):
    job = {"idpost": idpost, "link": link}
    if job_type in (TTCJobType.REACTION_VIP_CHEO.value, TTCJobType.REACTION_VIP_RE.value):
        job["loaicx"] = loaicx
    if job_type == TTCJobType.COMMENT.value:
        job["nd"] = comment
    return job


def normalized_claim_job():
    return normalize_jobs_list(
        TTCJobType.COMMENT.value,
        [raw_job(TTCJobType.COMMENT.value)],
    )[0]


class TTCNormalizationAdversarialTests(unittest.TestCase):
    reaction_endpoints = (
        TTCJobType.REACTION_VIP_CHEO.value,
        TTCJobType.REACTION_VIP_RE.value,
    )

    def test_both_reaction_endpoints_accept_only_canonical_text_with_case_and_whitespace(self):
        forms = ("like", " LIKE ", "Love", " care ", "HAHA", "Wow", "sad", " angry ")
        expected = ("LIKE", "LIKE", "LOVE", "CARE", "HAHA", "WOW", "SAD", "ANGRY")
        for endpoint in self.reaction_endpoints:
            for value, canonical in zip(forms, expected):
                with self.subTest(endpoint=endpoint, value=value):
                    job = normalize_jobs_list(endpoint, [raw_job(endpoint, loaicx=value)])[0]
                    self.assertEqual(job["reaction_type"], canonical)
                    self.assertEqual(job["raw_data"]["loaicx"], value)

    def test_both_reaction_endpoints_reject_aliases_unknown_and_non_text_scalars(self):
        invalid = (
            "thich",
            "yeu thich",
            "thuong thuong",
            "buon",
            "phan no",
            "NOT_A_REACTION",
            "LIKE_REACT",
            "",
            "   ",
            None,
            1,
            True,
            False,
        )
        for endpoint in self.reaction_endpoints:
            for value in invalid:
                with self.subTest(endpoint=endpoint, value=value):
                    with self.assertRaises(TTCResponseError):
                        normalize_jobs_list(endpoint, [raw_job(endpoint, loaicx=value)])

    def test_like_post_endpoints_remain_intrinsically_like_without_loaicx(self):
        for endpoint in (
            TTCJobType.LIKE_POST_VIP_CHEO.value,
            TTCJobType.LIKE_POST_VIP_RE.value,
        ):
            with self.subTest(endpoint=endpoint):
                job = normalize_jobs_list(endpoint, [raw_job(endpoint)])[0]
                self.assertEqual(job["reaction_type"], "LIKE")
                self.assertNotIn("loaicx", job["raw_data"])

    def test_comment_preserves_long_text_and_evidenced_wrapper_transformations(self):
        long_comment = "A" * 501
        cases = (
            (long_comment, long_comment),
            (
                'TTC wrapper: "hello &amp; goodbye"',
                'TTC wrapper: "hello & goodbye"',
            ),
            ("  surrounding whitespace  ", "surrounding whitespace"),
        )
        for source, expected in cases:
            with self.subTest(source_length=len(source)):
                job = normalize_jobs_list(
                    TTCJobType.COMMENT.value,
                    [raw_job(TTCJobType.COMMENT.value, comment=source)],
                )[0]
                self.assertEqual(job["comment_text"], expected)

    def test_malformed_bool_identifier_target_and_comment_fail_before_scheduler(self):
        cases = (
            raw_job(TTCJobType.COMMENT.value, idpost=True),
            raw_job(TTCJobType.COMMENT.value, link=True),
            raw_job(TTCJobType.COMMENT.value, comment=True),
        )
        for job in cases:
            with self.subTest(job=job):
                with self.assertRaises(TTCResponseError):
                    normalize_jobs_list(TTCJobType.COMMENT.value, [job])


class TTCClaimAdversarialTests(unittest.TestCase):
    def claim(self, payload=None, *, status_code=200, text=None, content_type="application/json"):
        client = TTCClient(worker_id="claim-adversarial", log_fn=lambda *_args: None)
        client._session.close()
        session = FakeSession(FakeResponse(
            payload,
            status_code=status_code,
            text=text,
            content_type=content_type,
        ))
        client._session = session
        client._authenticated = True
        return client.claim_reward(normalized_claim_job())

    def test_structured_failure_always_beats_positive_text(self):
        cases = (
            {"success": False, "message": "Thành công cộng 600 xu"},
            {"ok": False, "message": "Cộng 600 xu"},
            {"status": False, "message": "Cộng 600 xu"},
            {"status": "0", "message": "Cộng 600 xu"},
            {"status": "fail", "message": "Cộng 600 xu"},
            {"status": "failed", "message": "Cộng 600 xu"},
            {"status": "error", "message": "Cộng 600 xu"},
            {"status": "rejected", "message": "Cộng 600 xu"},
            {"error": "database unavailable", "message": "Thành công cộng 600 xu"},
            {"data": {"success": False}, "message": "Cộng 600 xu"},
            {"data": {"error": "failure"}, "message": "Cộng 600 xu"},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                result = self.claim(payload)
                self.assertIs(result["success"], False)
                self.assertEqual(result["earned_coin"], 0)

    def test_positive_and_unknown_claim_semantics_remain_fail_closed(self):
        positive = self.claim({"success": True, "message": "Thành công cộng 600 xu"})
        self.assertIs(positive["success"], True)
        self.assertEqual(positive["earned_coin"], 600)

        plain_positive = self.claim(text="Thành công cộng 600 xu", content_type="text/plain")
        self.assertIs(plain_positive["success"], True)

        plain_contradiction = self.claim(
            text="error: database unavailable; Thành công cộng 600 xu",
            content_type="text/plain",
        )
        self.assertIs(plain_contradiction["success"], False)

        unknown = self.claim({"unexpected": "value"})
        self.assertIs(unknown["success"], False)

        for unknown_status in ("pending", "mystery", None, 200):
            with self.subTest(unknown_status=unknown_status):
                structured_unknown = self.claim({
                    "status": unknown_status,
                    "message": "Thành công cộng 600 xu",
                })
                self.assertIs(structured_unknown["success"], False)

        html = self.claim(
            text="<html><body>Thành công cộng 600 xu</body></html>",
            content_type="text/html",
        )
        self.assertIs(html["success"], False)
        self.assertEqual(html["reason"], "INVALID_RESPONSE")

    def test_claim_http_status_precedes_positive_body(self):
        payload = {"success": True, "message": "Thành công cộng 600 xu"}
        for status_code, expected_error in (
            (401, TTCAuthError),
            (403, TTCAuthError),
            (429, TTCRateLimitError),
            (500, TTCResponseError),
        ):
            with self.subTest(status_code=status_code):
                with self.assertRaises(expected_error):
                    self.claim(payload, status_code=status_code)

    def test_optional_claim_balance_uses_strict_integer_semantics(self):
        cases = (
            (123, 123),
            ("123", 123),
            (True, None),
            (False, None),
            (None, None),
            ("bad", None),
            (1.5, None),
        )
        for raw_balance, expected in cases:
            with self.subTest(raw_balance=raw_balance):
                result = self.claim({
                    "success": True,
                    "message": "Thành công cộng 600 xu",
                    "data": {"sodu": raw_balance},
                })
                self.assertEqual(result["balance"], expected)


class TTCStateLifecycleTests(unittest.TestCase):
    @staticmethod
    def authenticated_client():
        client = TTCClient(worker_id="state-lifecycle", log_fn=lambda *_args: None)
        client._session.close()
        session = FakeSession(FakeResponse({"data": {"user": "tester", "sodu": 123}}))
        client._session = session
        return client, session

    def test_new_authentication_attempt_clears_stale_session_cookies(self):
        client, session = self.authenticated_client()
        session.cookies.set("stale_identity", "synthetic")

        client.authenticate("synthetic-token")

        self.assertEqual(list(session.cookies), [])

    def test_close_clears_logical_account_and_token_state(self):
        client, session = self.authenticated_client()
        client.authenticate("synthetic-token")

        client.close()

        self.assertTrue(session.closed)
        self.assertEqual(client._token, "")
        self.assertEqual(client.get_account_info(), {
            "user": "",
            "balance": None,
            "authenticated": False,
        })


class FacebookAppOAuthConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.secret_file = os.path.join(self.temp_dir.name, "facebook_app_oauth.txt")
        file_patcher = patch.object(api_ttc, "FB_APP_OAUTH_FILE", self.secret_file)
        file_patcher.start()
        self.addCleanup(file_patcher.stop)

    def test_environment_has_priority_over_local_file(self):
        with open(self.secret_file, "w", encoding="utf-8") as stream:
            stream.write("OAuth local-fixture|local-secret")
        with patch.dict(os.environ, {api_ttc.FB_APP_OAUTH_ENV: "OAuth env-fixture|env-secret"}):
            self.assertEqual(api_ttc.load_facebook_app_oauth(), "OAuth env-fixture|env-secret")

    def test_local_untracked_file_is_used_when_environment_is_absent(self):
        with open(self.secret_file, "w", encoding="utf-8") as stream:
            stream.write("OAuth local-fixture|local-secret\n")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(api_ttc.FB_APP_OAUTH_ENV, None)
            self.assertEqual(api_ttc.load_facebook_app_oauth(), "OAuth local-fixture|local-secret")

    def test_missing_configuration_fails_before_facebook_request(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(api_ttc.FB_APP_OAUTH_ENV, None)
            facebook = api_ttc.Requests_facebook()
            self.addCleanup(facebook._requests.close)
            with (
                patch.object(facebook._requests, "post") as post,
                patch.object(api_ttc, "print_error"),
            ):
                token = facebook.login_and_get_token(
                    "fixture-user", "fixture-password", ""
                )
            self.assertIsNone(token)
            post.assert_not_called()
            self.assertEqual(facebook._last_login_result["status"], "CONFIGURATION_ERROR")
            self.assertIn(api_ttc.FB_APP_OAUTH_ENV, facebook._last_login_result["message"])

    def test_login_loads_once_and_reuses_one_oauth_boundary(self):
        source = inspect.getsource(api_ttc.Requests_facebook._login_facebook)
        self.assertEqual(source.count("load_facebook_app_oauth()"), 1)
        self.assertEqual(source.count('"Authorization": app_oauth'), 3)


class FacebookSensitiveInputPolicyTests(unittest.TestCase):
    def test_batch_password_and_2fa_credentials_use_hidden_input(self):
        source = inspect.getsource(api_ttc.account_manager)
        self.assertIn("raw_credentials = secret_input", source)
        self.assertNotIn('raw_credentials = input(', source)


if __name__ == "__main__":
    unittest.main()
