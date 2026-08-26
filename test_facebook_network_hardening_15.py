import ast
import base64
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import requests

import api_ttc


class FakeResponse:
    def __init__(self, json_data):
        self._json_data = json_data
        self.text = json.dumps(json_data)

    def json(self):
        return self._json_data


class RecordingSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if not self.outcomes:
            raise AssertionError("Unexpected Facebook POST")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FacebookNetworkHardeningTests(unittest.TestCase):
    def test_batch_option_four_uses_visible_input(self):
        source = Path(api_ttc.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        account_manager = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "account_manager"
        )
        assignments = [
            node for node in ast.walk(account_manager)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "raw_credentials"
                for target in node.targets
            )
        ]

        self.assertEqual(1, len(assignments))
        call = assignments[0].value
        self.assertIsInstance(call, ast.Call)
        self.assertIsInstance(call.func, ast.Name)
        self.assertEqual("input", call.func.id)
        self.assertIn("secret_input", source)

    def test_get_info_retries_one_network_failure_then_succeeds(self):
        session = RecordingSession([
            requests.Timeout("first timeout"),
            FakeResponse({"data": {"me": {"id": "synthetic-uid"}}}),
        ])
        action = api_ttc.AUTO_Action()
        action._requests = session

        with mock.patch.object(api_ttc.time, "sleep") as sleep_mock:
            with contextlib.redirect_stdout(io.StringIO()):
                result = action.get_info("synthetic-token")

        self.assertEqual(("synthetic-uid", "OK"), result)
        self.assertEqual(2, len(session.calls))
        self.assertEqual(
            [api_ttc.FB_READ_TIMEOUT, api_ttc.FB_READ_TIMEOUT],
            [kwargs["timeout"] for _args, kwargs in session.calls],
        )
        self.assertEqual(session.calls[0][1]["headers"], session.calls[1][1]["headers"])
        self.assertEqual(session.calls[0][1]["data"], session.calls[1][1]["data"])
        sleep_mock.assert_called_once_with(api_ttc.NETWORK_RETRY_DELAY)

    def test_get_info_stops_after_second_network_failure(self):
        session = RecordingSession([
            requests.Timeout("first timeout"),
            requests.ConnectionError("final connection error"),
        ])
        action = api_ttc.AUTO_Action()
        action._requests = session

        with mock.patch.object(api_ttc.time, "sleep"):
            with contextlib.redirect_stdout(io.StringIO()):
                result = action.get_info("synthetic-token")

        self.assertIsNone(result[0])
        self.assertIn("Facebook get_info timeout/lỗi mạng", result[1])
        self.assertEqual(2, len(session.calls))

    def test_get_info_does_not_retry_facebook_semantic_error(self):
        session = RecordingSession([
            FakeResponse({"errors": [{"message": "Invalid access_token"}]}),
        ])
        action = api_ttc.AUTO_Action()
        action._requests = session

        result = action.get_info("synthetic-token")

        self.assertIsNone(result[0])
        self.assertIn("Token", result[1])
        self.assertEqual(1, len(session.calls))

    def test_reaction_network_failure_is_not_retried(self):
        session = RecordingSession([requests.Timeout("synthetic timeout")])
        action = api_ttc.AUTO_Action()
        action._requests = session

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = action.auto_reaction(
                "synthetic-post", "LIKE", "synthetic-token", "synthetic-actor"
            )

        self.assertFalse(result)
        self.assertEqual(1, len(session.calls))
        kwargs = session.calls[0][1]
        self.assertEqual(api_ttc.FB_ACTION_TIMEOUT, kwargs["timeout"])
        self.assertEqual("ViewerReactionsMutation", kwargs["headers"]["X-FB-Friendly-Name"])
        variables = json.loads(kwargs["data"]["variables"])
        self.assertEqual("synthetic-actor", variables["input"]["actor_id"])
        self.assertEqual(
            base64.b64encode(b"feedback:synthetic-post").decode(),
            variables["input"]["feedback_id"],
        )
        self.assertIn("outcome unknown", output.getvalue())

    def test_comment_network_failure_is_not_retried(self):
        session = RecordingSession([requests.Timeout("synthetic timeout")])
        action = api_ttc.AUTO_Action()
        action._requests = session

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = action.auto_cmt(
                "synthetic-post", "synthetic text", "synthetic-token", "synthetic-actor"
            )

        self.assertFalse(result)
        self.assertEqual(1, len(session.calls))
        kwargs = session.calls[0][1]
        self.assertEqual(api_ttc.FB_ACTION_TIMEOUT, kwargs["timeout"])
        self.assertEqual("CommentCreateMutation", kwargs["headers"]["X-FB-Friendly-Name"])
        variables = json.loads(kwargs["data"]["variables"])
        self.assertEqual("synthetic text", variables["input"]["message"]["text"])
        self.assertEqual("synthetic-actor", variables["input"]["actor_id"])
        self.assertIn("outcome unknown", output.getvalue())

    def test_valid_comment_keeps_evidence_and_pending_approval_semantics(self):
        comment_id = base64.b64encode(
            b"comment:synthetic-post_synthetic-comment"
        ).decode()
        response = FakeResponse({
            "data": {
                "comment_create": {
                    "comment": {
                        "id": comment_id,
                        "body": {"text": "synthetic text"},
                        "actor": {"id": "synthetic-actor"},
                        "comment_privacy_value": "PENDING_APPROVAL",
                    }
                }
            }
        })
        session = RecordingSession([response])
        action = api_ttc.AUTO_Action()
        action._requests = session

        with contextlib.redirect_stdout(io.StringIO()):
            result = action.auto_cmt(
                "synthetic-post", "synthetic text", "synthetic-token", "synthetic-actor"
            )

        self.assertTrue(result)
        self.assertEqual("synthetic-post", action.last_comment_evidence["parent_post_id"])
        self.assertEqual("synthetic text", action.last_comment_evidence["returned_text"])
        self.assertEqual("synthetic-actor", action.last_comment_evidence["returned_actor_id"])
        self.assertTrue(api_ttc._is_comment_pending_approval(action.last_comment_evidence))
        self.assertEqual(api_ttc.FB_ACTION_TIMEOUT, session.calls[0][1]["timeout"])

    def test_comment_evidence_still_rejects_mismatched_returned_actor(self):
        response = FakeResponse({
            "data": {
                "comment_create": {
                    "comment": {
                        "id": "synthetic-comment-id",
                        "body": {"text": "synthetic text"},
                        "actor": {"id": "different-actor"},
                    }
                }
            }
        })
        session = RecordingSession([response])
        action = api_ttc.AUTO_Action()
        action._requests = session

        with contextlib.redirect_stdout(io.StringIO()):
            result = action.auto_cmt(
                "synthetic-post", "synthetic text", "synthetic-token", "synthetic-actor"
            )

        self.assertFalse(result)
        self.assertEqual("different-actor", action.last_comment_evidence["returned_actor_id"])
        self.assertEqual(1, len(session.calls))


if __name__ == "__main__":
    unittest.main()
