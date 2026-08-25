import json
import unittest

import requests

from api_bumx import (
    TTC_AUTH_URL,
    TTC_ENDPOINTS,
    TTC_SET_UID_URL,
    TTCAuthError,
    TTCClient,
    TTCJobType,
    TTCNoJobAvailable,
    TTCRateLimitError,
    TTCResponseError,
)


_MISSING = object()


class FakeResponse:
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


class ScriptedSession:
    def __init__(self, *, auth_responses=None, uid_responses=None, get_responses=None):
        self._post_responses = {
            TTC_AUTH_URL: list(auth_responses or []),
            TTC_SET_UID_URL: list(uid_responses or []),
        }
        self._get_responses = list(get_responses or [])
        self.post_calls = []
        self.get_calls = []
        self.closed = False

    @staticmethod
    def _resolve(scripted):
        if isinstance(scripted, BaseException):
            raise scripted
        return scripted

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        try:
            response = self._post_responses[url].pop(0)
        except (KeyError, IndexError) as exc:
            raise AssertionError(f"Unexpected POST request: {url}") from exc
        return self._resolve(response)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        try:
            response = self._get_responses.pop(0)
        except IndexError as exc:
            raise AssertionError(f"Unexpected GET request: {url}") from exc
        return self._resolve(response)

    def close(self):
        self.closed = True


def auth_response(user="tester", balance=100):
    return FakeResponse({"data": {"user": user, "sodu": balance}})


class TTCUIDContractTests(unittest.TestCase):
    def make_authenticated_client(
        self,
        *,
        uid_responses=None,
        get_responses=None,
        auth_responses=None,
    ):
        session = ScriptedSession(
            auth_responses=auth_responses or [auth_response()],
            uid_responses=uid_responses,
            get_responses=get_responses,
        )
        client = TTCClient(worker_id="uid-contract", log_fn=lambda *_args: None)
        client._session.close()
        client._session = session
        client.authenticate("valid-token")
        return client, session

    def test_set_uid_posts_exact_endpoint_and_form_payload(self):
        client, session = self.make_authenticated_client(
            uid_responses=[FakeResponse(text="1")]
        )

        client.configure_facebook_uid(" 123456 ")

        self.assertEqual(len(session.post_calls), 2)
        url, kwargs = session.post_calls[-1]
        self.assertEqual(url, "https://tuongtaccheo.com/cauhinh/datnick.php")
        self.assertEqual(url, TTC_SET_UID_URL)
        self.assertEqual(kwargs["data"], {"iddat[]": "123456", "loai": "fb"})
        self.assertNotIn("json", kwargs)
        self.assertEqual(client._configured_facebook_uid, "123456")

    def test_exact_one_and_transport_whitespace_are_success(self):
        for body in ("1", " 1 \r\n"):
            with self.subTest(body=body):
                client, _session = self.make_authenticated_client(
                    uid_responses=[FakeResponse(text=body)]
                )

                client.set_uid("uid-ok")

                self.assertEqual(client._configured_facebook_uid, "uid-ok")

    def test_unknown_2xx_bodies_fail_closed(self):
        bodies = (
            "<!DOCTYPE html><html><body>TTC homepage</body></html>",
            "",
            "0",
            "success",
            "arbitrary text",
            '{"success":true}',
            "{}",
        )
        for body in bodies:
            with self.subTest(body=body):
                client, _session = self.make_authenticated_client(
                    uid_responses=[FakeResponse(text=body)]
                )

                with self.assertRaises(TTCResponseError):
                    client.configure_facebook_uid("uid-rejected")

                self.assertIsNone(client._configured_facebook_uid)

    def test_http_status_takes_precedence_over_success_body(self):
        cases = (
            (401, TTCAuthError),
            (403, TTCAuthError),
            (429, TTCRateLimitError),
            (400, TTCResponseError),
            (500, TTCResponseError),
            (503, TTCResponseError),
        )
        for status_code, expected_error in cases:
            with self.subTest(status_code=status_code):
                client, _session = self.make_authenticated_client(
                    uid_responses=[FakeResponse(status_code=status_code, text="1")]
                )

                with self.assertRaises(expected_error):
                    client.configure_facebook_uid("uid-http-error")

                self.assertIsNone(client._configured_facebook_uid)

    def test_fetch_before_confirmed_uid_is_blocked_without_job_request(self):
        client, session = self.make_authenticated_client()

        with self.assertRaises(TTCResponseError):
            client.fetch_jobs(TTCJobType.COMMENT.value)

        self.assertEqual(session.get_calls, [])

    def test_successful_set_uid_allows_fetch(self):
        client, session = self.make_authenticated_client(
            uid_responses=[FakeResponse(text="1")],
            get_responses=[FakeResponse([])],
        )
        client.set_uid("uid-ok")

        with self.assertRaises(TTCNoJobAvailable):
            client.get_jobs(TTCJobType.COMMENT)

        self.assertEqual(
            [call[0] for call in session.get_calls],
            [TTC_ENDPOINTS[TTCJobType.COMMENT.value][0]],
        )

    def test_new_authentication_attempt_resets_confirmed_uid(self):
        client, session = self.make_authenticated_client(
            auth_responses=[auth_response("first"), auth_response("second")],
            uid_responses=[FakeResponse(text="1")],
        )
        client.set_uid("uid-first")

        client.authenticate("second-token")

        self.assertIsNone(client._configured_facebook_uid)
        with self.assertRaises(TTCResponseError):
            client.fetch_jobs(TTCJobType.COMMENT.value)
        self.assertEqual(session.get_calls, [])

    def test_failed_reconfiguration_clears_previous_confirmation(self):
        client, session = self.make_authenticated_client(
            uid_responses=[FakeResponse(text="1"), FakeResponse(text="0")]
        )
        client.set_uid("uid-a")

        with self.assertRaises(TTCResponseError):
            client.set_uid("uid-b")

        self.assertIsNone(client._configured_facebook_uid)
        with self.assertRaises(TTCResponseError):
            client.fetch_jobs(TTCJobType.COMMENT.value)
        self.assertEqual(session.get_calls, [])

    def test_transport_failure_clears_previous_confirmation(self):
        client, _session = self.make_authenticated_client(
            uid_responses=[
                FakeResponse(text="1"),
                requests.exceptions.Timeout("synthetic timeout"),
            ]
        )
        client.set_uid("uid-a")

        with self.assertRaises(TTCResponseError):
            client.set_uid("uid-b")

        self.assertIsNone(client._configured_facebook_uid)

    def test_close_resets_confirmed_uid(self):
        client, session = self.make_authenticated_client(
            uid_responses=[FakeResponse(text="1")]
        )
        client.set_uid("uid-ok")

        client.close()

        self.assertTrue(session.closed)
        self.assertIsNone(client._configured_facebook_uid)


if __name__ == "__main__":
    unittest.main()
