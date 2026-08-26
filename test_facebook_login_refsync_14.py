import contextlib
import io
import json
import os
import unittest
from unittest import mock

import api_ttc


class FakeFacebookResponse:
    def __init__(self, *, json_data=None, text="", status_code=200):
        self._json_data = json_data
        self.text = text
        self.status_code = status_code

    def json(self):
        return self._json_data


class RecordingFacebookSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected Facebook POST")
        return self.responses.pop(0)


class FacebookLoginReferenceSyncTests(unittest.TestCase):
    def test_login_without_runtime_oauth_config_matches_three_post_contract(self):
        responses = [
            FakeFacebookResponse(json_data={
                "data": {
                    "xfb_fb_key_attestation_create_one_time_challenge_nonce": {
                        "challenge_nonce": "synthetic-challenge"
                    }
                }
            }),
            FakeFacebookResponse(text=json.dumps({
                "two_step_verification_context": "synthetic-2fa-context"
            })),
            FakeFacebookResponse(text=json.dumps({
                "access_token": "synthetic-access-token"
            })),
        ]
        session = RecordingFacebookSession(responses)
        client = api_ttc.Requests_facebook()
        client._requests = session

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TTC_FB_APP_OAUTH", None)
            with contextlib.redirect_stdout(io.StringIO()):
                token = client.login_and_get_token(
                    "synthetic-uid", "synthetic-password", "JBSWY3DPEHPK3PXP"
                )

        self.assertEqual("synthetic-access-token", token)
        self.assertEqual(3, len(session.calls))
        self.assertFalse(hasattr(api_ttc, "FB_APP_OAUTH_ENV"))
        self.assertFalse(hasattr(api_ttc, "FB_APP_OAUTH_FILE"))
        self.assertFalse(hasattr(api_ttc, "load_facebook_app_oauth"))
        self.assertFalse(hasattr(api_ttc, "FacebookOAuthConfigurationError"))

        calls = [kwargs for _args, kwargs in session.calls]
        self.assertTrue(all(call["url"] == client._url_api for call in calls))
        self.assertTrue(all(call["timeout"] == (10, 45) for call in calls))

        authorizations = [call["headers"].get("Authorization") for call in calls]
        self.assertTrue(all(
            isinstance(value, str) and value.startswith("OAuth ")
            for value in authorizations
        ))
        self.assertEqual(1, len(set(authorizations)))

        challenge, password, two_factor = calls
        device = challenge["headers"]["X-ZERO-F-DEVICE-ID"]
        self.assertTrue(device)
        self.assertEqual(
            "CreateOneTimeChallengeNonceCreate",
            challenge["headers"]["X-FB-Friendly-Name"],
        )
        self.assertEqual(
            "14372771303129768114139595837",
            challenge["data"]["client_doc_id"],
        )
        self.assertEqual(
            json.dumps({"input": {"app_scoped_device_id": device}},
                       separators=(",", ":")),
            challenge["data"]["variables"],
        )

        self.assertEqual(
            "FbBloksActionRootQuery-com.bloks.www.bloks.caa.login.async.send_login_request",
            password["headers"]["X-FB-Friendly-Name"],
        )
        self.assertEqual(
            "119940804212792425089909966247",
            password["data"]["client_doc_id"],
        )
        self.assertIn("#PWD_FB4A:0:", password["data"]["variables"])
        self.assertIn("synthetic-uid", password["data"]["variables"])

        self.assertEqual(
            "FbBloksActionRootQuery-com.bloks.www.two_step_verification.verify_code.async",
            two_factor["headers"]["X-FB-Friendly-Name"],
        )
        self.assertEqual(
            "119940804212792425089909966247",
            two_factor["data"]["client_doc_id"],
        )
        self.assertIn("synthetic-2fa-context", two_factor["data"]["variables"])


if __name__ == "__main__":
    unittest.main()
