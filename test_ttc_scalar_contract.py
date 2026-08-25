import copy
import json
import unittest
from unittest.mock import Mock, patch

import requests

import api_ttc
from api_ttc import (
    TTCAuthError,
    TTCClient,
    TTCJobType,
    TTCNoJobAvailable,
    TTCResponseError,
    _parse_countdown,
    normalize_jobs_list,
    normalize_object_id,
)


class FakeResponse:
    def __init__(self, payload=None, *, text=None, status_code=200, content_type="application/json"):
        self.status_code = status_code
        self._payload = payload
        self.text = (
            json.dumps(payload, ensure_ascii=False, default=lambda value: f"<{type(value).__name__}>")
            if text is None
            else text
        )
        self.content = self.text.encode("utf-8")
        self.headers = {"content-type": content_type}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class RecordingSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.post_calls = []
        self.cookies = requests.cookies.RequestsCookieJar()

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected fake HTTP POST")
        return self.responses.pop(0)

    def close(self):
        pass


def raw_job(job_type, *, idpost="post-1", link="https://facebook.com/10001", nd="hello TTC", loaicx="LOVE"):
    job = {"idpost": idpost, "link": link}
    if job_type == TTCJobType.COMMENT.value:
        job["nd"] = nd
    if job_type in (TTCJobType.REACTION_VIP_CHEO.value, TTCJobType.REACTION_VIP_RE.value):
        job["loaicx"] = loaicx
    return job


def normalized_job(job_type=TTCJobType.COMMENT.value):
    return normalize_jobs_list(job_type, [raw_job(job_type)])[0]


def client_with_session(session):
    client = TTCClient(worker_id="scalar-contract", log_fn=lambda *_args: None)
    client._session.close()
    client._session = session
    return client


class TTCExternalScalarContractTests(unittest.TestCase):
    def test_job_identifier_target_comment_and_reaction_type_matrix(self):
        valid_comment = normalize_jobs_list(
            TTCJobType.COMMENT.value,
            [raw_job(TTCJobType.COMMENT.value, nd="A" * 501)],
        )[0]
        self.assertEqual(valid_comment["idpost"], "post-1")
        self.assertEqual(valid_comment["link"], "https://facebook.com/10001")
        self.assertEqual(valid_comment["comment_text"], "A" * 501)

        invalid_fields = {
            "idpost": ([], {}, 1.5, True, None, "   "),
            "link": ([], {}, 123, 1.5, True, None),
            "nd": ([], {}, 123, 1.5, True, None, "   "),
        }
        for field, values in invalid_fields.items():
            for value in values:
                with self.subTest(field=field, value_type=type(value).__name__, value=value):
                    kwargs = {field: value}
                    with self.assertRaises(TTCResponseError):
                        normalize_jobs_list(
                            TTCJobType.COMMENT.value,
                            [raw_job(TTCJobType.COMMENT.value, **kwargs)],
                        )

        canonical = ("LIKE", "LOVE", "CARE", "HAHA", "WOW", "SAD", "ANGRY")
        for value in canonical:
            with self.subTest(reaction=value):
                job = normalize_jobs_list(
                    TTCJobType.REACTION_VIP_RE.value,
                    [raw_job(TTCJobType.REACTION_VIP_RE.value, loaicx=value)],
                )[0]
                self.assertEqual(job["reaction_type"], value)

        invalid_reactions = (
            "thich", "yeu thich", "LIKE_REACT", "NOT_A_REACTION", "", "   ",
            [], {}, 123, 1.5, True, None,
        )
        for value in invalid_reactions:
            with self.subTest(reaction_type=type(value).__name__, reaction=value):
                with self.assertRaises(TTCResponseError):
                    normalize_jobs_list(
                        TTCJobType.REACTION_VIP_RE.value,
                        [raw_job(TTCJobType.REACTION_VIP_RE.value, loaicx=value)],
                    )

    def test_malformed_job_never_returns_a_partial_list(self):
        for field, value in (("idpost", []), ("link", {}), ("idpost", 1.5)):
            with self.subTest(field=field, value_type=type(value).__name__):
                invalid = raw_job(TTCJobType.COMMENT.value, **{field: value})
                with self.assertRaises(TTCResponseError):
                    normalize_jobs_list(
                        TTCJobType.COMMENT.value,
                        [raw_job(TTCJobType.COMMENT.value), invalid],
                    )

    def test_normalize_object_id_rejects_non_text_direct_inputs(self):
        for value in ({"id": "123456"}, ["123456"], 123, 123.5, True, b"123456"):
            with self.subTest(value_type=type(value).__name__):
                self.assertEqual(normalize_object_id(value), "")

        self.assertEqual(normalize_object_id("https://facebook.com/posts/123456"), "123456")

    def test_auth_user_accepts_text_and_rejects_malformed_types_without_state(self):
        for field in ("user", "username"):
            with self.subTest(valid_field=field):
                client = client_with_session(RecordingSession(FakeResponse({
                    "data": {field: " tester ", "sodu": 123},
                })))
                client.authenticate("synthetic-token")
                self.assertEqual(client.get_account_info()["user"], "tester")

        for value in ({}, [], True, 123, 123.5, b"tester"):
            with self.subTest(value_type=type(value).__name__):
                payload = {"data": {"user": value, "sodu": 123}}
                client = client_with_session(RecordingSession(FakeResponse(payload)))
                with (
                    patch("api_ttc._safe_json", return_value=payload),
                    self.assertRaises((TTCAuthError, TTCResponseError)),
                ):
                    client.authenticate("synthetic-token")
                self.assertEqual(client.get_account_info(), {
                    "user": "",
                    "balance": None,
                    "authenticated": False,
                })

    def test_facebook_uid_parser_and_ttc_uid_input_are_text_only(self):
        facebook = api_ttc.AUTO_Action()
        self.addCleanup(facebook._requests.close)
        for value in ({}, [], True, 123, 123.5):
            with self.subTest(parser_value_type=type(value).__name__):
                response = Mock()
                response.json.return_value = {"data": {"me": {"id": value}}}
                with patch.object(facebook._requests, "post", return_value=response):
                    uid, _message = facebook.get_info("synthetic-facebook-token")
                self.assertIsNone(uid)

        response = Mock()
        response.json.return_value = {"data": {"me": {"id": " 998877 "}}}
        with patch.object(facebook._requests, "post", return_value=response):
            uid, message = facebook.get_info("synthetic-facebook-token")
        self.assertEqual((uid, message), ("998877", "OK"))

        for value in ({}, [], True, 123, 123.5, b"998877"):
            with self.subTest(client_value_type=type(value).__name__):
                session = RecordingSession(FakeResponse(text="1", content_type="text/plain"))
                client = client_with_session(session)
                client._authenticated = True
                with self.assertRaises(ValueError):
                    client.configure_facebook_uid(value)
                self.assertEqual(session.post_calls, [])

        session = RecordingSession(FakeResponse(text="1", content_type="text/plain"))
        client = client_with_session(session)
        client._authenticated = True
        client.configure_facebook_uid(" 998877 ")
        self.assertEqual(session.post_calls[0][1]["data"], {"iddat[]": "998877", "loai": "fb"})

    def test_claim_identifier_and_reaction_are_validated_before_network(self):
        for value in ({}, [], 123, 123.5, True, None, "   "):
            with self.subTest(idpost_type=type(value).__name__, idpost=value):
                job = normalized_job()
                job["idpost"] = value
                session = RecordingSession(FakeResponse({"success": True, "message": "Thành công cộng 600 xu"}))
                client = client_with_session(session)
                client._authenticated = True
                with self.assertRaises(ValueError):
                    client.claim_reward(job)
                self.assertEqual(session.post_calls, [])

        for value in ({}, [], 123, 1.5, True, None, "NOT_A_REACTION", " love "):
            with self.subTest(reaction_type=type(value).__name__, reaction=value):
                job = normalized_job(TTCJobType.REACTION_VIP_RE.value)
                job["reaction_type"] = value
                session = RecordingSession(FakeResponse({"success": True, "message": "Thành công cộng 600 xu"}))
                client = client_with_session(session)
                client._authenticated = True
                with self.assertRaises(ValueError):
                    client.claim_reward(job)
                self.assertEqual(session.post_calls, [])

        job = normalized_job(TTCJobType.REACTION_VIP_RE.value)
        session = RecordingSession(FakeResponse({"success": True, "message": "Thành công cộng 600 xu"}))
        client = client_with_session(session)
        client._authenticated = True
        result = client.claim_reward(job)
        self.assertIs(result["success"], True)
        self.assertEqual(session.post_calls[0][1]["data"], {"id": "post-1", "loaicx": "LOVE"})

    def test_structured_claim_messages_cannot_manufacture_success(self):
        malformed_messages = (
            {"message": {"value": "Thành công cộng 600 xu"}},
            {"message": ["Thành công cộng 600 xu"]},
            {"status": "error", "message": {"text": "Thành công cộng 600 xu"}},
        )
        for payload in malformed_messages:
            with self.subTest(payload=payload):
                session = RecordingSession(FakeResponse(payload))
                client = client_with_session(session)
                client._authenticated = True
                result = client.claim_reward(normalized_job())
                self.assertIs(result["success"], False)
                self.assertEqual(result["earned_coin"], 0)

        session = RecordingSession(FakeResponse({"success": True, "message": "Thành công cộng 600 xu"}))
        client = client_with_session(session)
        client._authenticated = True
        result = client.claim_reward(normalized_job())
        self.assertIs(result["success"], True)
        self.assertEqual(result["earned_coin"], 600)

    def test_countdown_accepts_only_integer_or_integer_text(self):
        for value, expected in ((10, 10), ("10", 10), (0, 0), ("0", 0)):
            with self.subTest(valid=value):
                self.assertEqual(_parse_countdown({"countdown": value}), expected)

        for value in (True, False, 1.5, {}, [], None, "1.5", "bad"):
            with self.subTest(invalid_type=type(value).__name__, invalid=value):
                self.assertEqual(_parse_countdown({"countdown": value}), 0)

        self.assertEqual(_parse_countdown({"countdown": False, "time": "10"}), 0)
        self.assertEqual(_parse_countdown({"countdown": 1.5, "wait": "10"}), 0)

        self.assertEqual(TTCNoJobAvailable("none", "17").countdown, 17)
        for value in (True, False, 1.5, {}, []):
            with self.subTest(exception_invalid_type=type(value).__name__):
                self.assertEqual(TTCNoJobAvailable("none", value).countdown, 0)

        self.assertEqual(api_ttc._ttc_no_job_wait_seconds([{"countdown": "17"}]), 17)
        for value in (True, False, 1.5, {}, [], None, "1.5", "bad"):
            with self.subTest(runtime_invalid_type=type(value).__name__, runtime_invalid=value):
                self.assertEqual(api_ttc._ttc_no_job_wait_seconds([{"countdown": value}]), 10)

    def test_runtime_rejects_structured_facebook_uid_before_ttc_or_action_calls(self):
        api = Mock()
        action = Mock()
        action.get_info.return_value = ({"id": "998877"}, "malformed UID")
        with (
            patch.object(api_ttc, "AUTO_Action", return_value=action),
            patch.object(api_ttc, "print_error"),
            patch("builtins.print"),
        ):
            result = api_ttc.run_ttc_jobs(api, "synthetic-facebook-token", job_types={"like_facebook"})

        self.assertEqual(result, 0)
        api.set_facebook_uid.assert_not_called()
        api.fetch_jobs_for_legacy_type.assert_not_called()
        action.auto_reaction.assert_not_called()
        action.auto_cmt.assert_not_called()

    def test_runtime_earned_coin_accepts_only_integer_or_integer_text(self):
        self.assertEqual(api_ttc._normalize_ttc_earned_coin(25), 25)
        self.assertEqual(api_ttc._normalize_ttc_earned_coin("25"), 25)
        for value in (True, False, 1.5, {}, [], None, "1.5", "bad", -5):
            with self.subTest(value_type=type(value).__name__, value=value):
                self.assertEqual(api_ttc._normalize_ttc_earned_coin(value), 0)


if __name__ == "__main__":
    unittest.main()
