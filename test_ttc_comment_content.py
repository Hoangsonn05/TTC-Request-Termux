import base64
import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, call, patch

import api_ttc
from api_ttc import (
    TTCClient,
    TTCJobType,
    TTCResponseError,
    _decode_response_body,
    normalize_jobs_list,
    parse_ttc_comment_text,
)


class FakeResponse:
    def __init__(
        self,
        content,
        *,
        content_type="application/json; charset=utf-8",
        json_result=None,
        reject_json=False,
    ):
        self.status_code = 200
        self.content = content
        self.headers = {"content-type": content_type}
        self.encoding = "ISO-8859-1"
        self.apparent_encoding = "utf-8"
        self._json_result = json_result
        self._reject_json = reject_json
        self.json_calls = 0

    @property
    def text(self):
        return self.content.decode(self.encoding, errors="replace")

    def json(self):
        self.json_calls += 1
        if self._reject_json:
            raise AssertionError("fetch_jobs must not call response.json()")
        return self._json_result


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.response

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.response


def raw_comment(nd, *, idpost="42"):
    return {
        "idpost": idpost,
        "link": f"https://facebook.com/posts/{idpost}",
        "nd": nd,
    }


class TTCCommentCodecTests(unittest.TestCase):
    def test_lossless_comment_vectors(self):
        cases = (
            ("HÃƒÂ´m nay sÃƒÂ¡ng quÃƒÂ¡", "Hôm nay sáng quá"),
            ("H&#244;m nay &amp; ng&#224;y mai", "Hôm nay & ngày mai"),
            (r"H\u00f4m nay", "Hôm nay"),
            ("H%C3%B4m%20nay", "Hôm nay"),
            (
                '"Anh ấy nói \\"xin chào\\" hôm nay"',
                'Anh ấy nói "xin chào" hôm nay',
            ),
            (
                '["Đăng ký mạng càng sớm càng tốt"]',
                "Đăng ký mạng càng sớm càng tốt",
            ),
            (
                '"[\\"Đăng ký mạng càng sớm càng tốt\\"]"',
                "Đăng ký mạng càng sớm càng tốt",
            ),
            (
                '["Anh ấy nói \\"xin chào\\""]',
                'Anh ấy nói "xin chào"',
            ),
            ("[VIP] Sản phẩm rất đẹp", "[VIP] Sản phẩm rất đẹp"),
            ("h0m/nay/add", "h0m/nay/add"),
            ("đẹp/xinh/tốt", "đẹp/xinh/tốt"),
            ("Đẹp quá ❤️🔥", "Đẹp quá ❤️🔥"),
            (
                'TTC wrapper: "hello &amp; goodbye"',
                'TTC wrapper: "hello & goodbye"',
            ),
            ("A" * 501, "A" * 501),
        )

        for raw, expected in cases:
            with self.subTest(raw=raw[:80]):
                self.assertEqual(parse_ttc_comment_text(raw), expected)

    def test_malformed_percent_escape_is_preserved(self):
        self.assertEqual(
            parse_ttc_comment_text("giữ-nguyên-%FF"),
            "giữ-nguyên-%FF",
        )

    def test_non_string_comment_values_remain_rejected(self):
        for value in ([], {}, 123, 1.5, True, None):
            with self.subTest(value=value):
                with self.assertRaises(TTCResponseError):
                    normalize_jobs_list(
                        TTCJobType.COMMENT.value,
                        [raw_comment(value)],
                    )

    def test_raw_nd_is_not_mutated_by_normalization(self):
        raw_nd = '["Đẹp quá ❤️🔥"]'
        raw = raw_comment(raw_nd)

        job = normalize_jobs_list(TTCJobType.COMMENT.value, [raw])[0]

        self.assertEqual(job["comment_text"], "Đẹp quá ❤️🔥")
        self.assertEqual(raw["nd"], raw_nd)
        self.assertEqual(job["raw_data"]["nd"], raw_nd)


class TTCCommentFetchEncodingTests(unittest.TestCase):
    def test_utf8_wire_body_wins_over_wrong_iso_8859_1_header(self):
        expected = "Hôm nay sản phẩm rất đẹp ❤️"
        payload = [raw_comment(expected)]
        wire_text = json.dumps(payload, ensure_ascii=False)
        response = FakeResponse(
            wire_text.encode("utf-8"),
            content_type="application/json; charset=ISO-8859-1",
            reject_json=True,
        )
        client = TTCClient(worker_id="comment-encoding", log_fn=lambda *_: None)
        client._session.close()
        client._session = FakeSession(response)
        client._authenticated = True
        client._configured_facebook_uid = "fb-uid"

        jobs = client.fetch_jobs(TTCJobType.COMMENT.value)

        self.assertEqual(response.json_calls, 0)
        self.assertEqual(jobs[0]["raw_data"]["nd"], expected)
        self.assertEqual(jobs[0]["comment_text"], expected)
        self.assertEqual(jobs[0]["server_raw_text"], wire_text)
        decoded_text, metadata = _decode_response_body(response)
        self.assertEqual(json.loads(decoded_text), payload)
        self.assertEqual(metadata["encoding"].lower(), "utf-8")


class TTCFacebookCommentBoundaryTests(unittest.TestCase):
    @staticmethod
    def _encoded_comment_id(parent_post_id="42", comment_id="99"):
        raw = f"comment:{parent_post_id}_{comment_id}".encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _run_auto_cmt(
        self,
        comment_data,
        *,
        post_id="42",
        text="uy tín nha",
        actor_id="actor-1",
    ):
        captured = {}

        class GraphResponse:
            text = ""

            def json(self):
                return {
                    "data": {
                        "comment_create": {
                            "comment": comment_data,
                        },
                    },
                }

        class GraphSession:
            headers = {}

            def post(self, **kwargs):
                captured.update(kwargs)
                return GraphResponse()

        action = api_ttc.AUTO_Action.__new__(api_ttc.AUTO_Action)
        action.url = "mock://graphql"
        action._requests = GraphSession()
        with contextlib.redirect_stdout(io.StringIO()):
            result = action.auto_cmt(
                post_id,
                text,
                "not-a-real-token",
                actor_id,
            )
        return result, action.last_comment_evidence, captured

    def test_parent_post_match_is_claimable_and_mismatch_is_blocked(self):
        matching, evidence, _captured = self._run_auto_cmt(
            {"id": self._encoded_comment_id("42")},
        )
        mismatching, mismatch_evidence, _captured = self._run_auto_cmt(
            {"id": self._encoded_comment_id("999")},
        )
        opaque, opaque_evidence, _captured = self._run_auto_cmt({
            "id": "opaque-comment-id",
        })

        self.assertTrue(matching)
        self.assertEqual(evidence["parent_post_id"], "42")
        self.assertFalse(mismatching)
        self.assertEqual(mismatch_evidence["parent_post_id"], "999")
        self.assertTrue(opaque)
        self.assertIsNone(opaque_evidence["parent_post_id"])

    def test_returned_text_is_exact_nfc_when_present_and_optional_when_absent(self):
        comment_id = self._encoded_comment_id()
        equal, equal_evidence, _captured = self._run_auto_cmt({
            "id": comment_id,
            "body": {"text": "uy ti\u0301n nha"},
        })
        different, _evidence, _captured = self._run_auto_cmt({
            "id": comment_id,
            "body": {"text": "uy tín nha!"},
        })
        absent, absent_evidence, _captured = self._run_auto_cmt({
            "id": comment_id,
        })

        self.assertTrue(equal)
        self.assertEqual(equal_evidence["returned_text"], "uy ti\u0301n nha")
        self.assertFalse(different)
        self.assertTrue(absent)
        self.assertIsNone(absent_evidence["returned_text"])

    def test_returned_actor_is_verified_when_present_and_optional_when_absent(self):
        comment_id = self._encoded_comment_id()
        matching, _evidence, _captured = self._run_auto_cmt({
            "id": comment_id,
            "author": {"id": "actor-1"},
        })
        mismatching, _evidence, _captured = self._run_auto_cmt({
            "id": comment_id,
            "actor": {"id": "actor-2"},
        })
        absent, absent_evidence, _captured = self._run_auto_cmt({
            "id": comment_id,
        })

        self.assertTrue(matching)
        self.assertFalse(mismatching)
        self.assertTrue(absent)
        self.assertIsNone(absent_evidence["returned_actor_id"])

    def test_safe_evidence_captures_available_response_fields_only(self):
        comment_data = {
            "id": self._encoded_comment_id(),
            "body": {"text": "uy tín nha"},
            "author": {"id": "actor-1"},
            "feedback": {"id": "feedback-42"},
            "is_hidden": False,
            "is_pending": True,
            "visibility_state": "VISIBLE",
        }

        result, evidence, _captured = self._run_auto_cmt(comment_data)

        self.assertTrue(result)
        self.assertEqual(evidence["comment_keys"], tuple(sorted(comment_data)))
        self.assertEqual(evidence["feedback_id"], "feedback-42")
        self.assertFalse(evidence["hidden"])
        self.assertTrue(evidence["pending"])
        self.assertEqual(
            evidence["visibility_fields"],
            {
                "is_hidden": False,
                "is_pending": True,
                "visibility_state": "VISIBLE",
            },
        )

    def test_visibility_evidence_keeps_pending_safety_signal(self):
        comment_data = {
            "id": self._encoded_comment_id(),
            "comment_privacy_value": "DEFAULT",
            "spam_display_mode": "VISIBLE",
            "is_viewer_comment_poster": True,
            "is_declined_by_group_admin_assistant": False,
            "can_viewer_delete": True,
            "can_viewer_edit": False,
            "created_time": 123456,
            "legacy_fbid": "99",
            "url": "https://facebook.example/comment/99",
            "depth": 0,
            "group_comment_info": {"is_pending": True, "author_name": "not logged"},
            "comment_tombstone_upsell_config": {"is_hidden": False},
        }

        evidence = api_ttc._facebook_comment_response_evidence(comment_data)

        self.assertEqual(evidence["visibility_scalars"]["comment_privacy_value"], "DEFAULT")
        self.assertTrue(evidence["visibility_structures"]["group_comment_info"]["safe_scalar_flags"]["is_pending"])
        self.assertNotIn("author_name", evidence["visibility_structures"]["group_comment_info"]["safe_scalar_flags"])

    def test_auto_cmt_graphql_message_text_is_exact(self):
        expected = 'Anh ấy nói "xin chào" hôm nay ❤️ / 100%'
        captured = {}

        class GraphResponse:
            text = ""

            def json(self):
                return {"errors": []}

        class GraphSession:
            headers = {}

            def post(self, **kwargs):
                captured.update(kwargs)
                return GraphResponse()

        action = api_ttc.AUTO_Action.__new__(api_ttc.AUTO_Action)
        action.url = "mock://graphql"
        action._requests = GraphSession()

        with contextlib.redirect_stdout(io.StringIO()):
            action.auto_cmt("42", expected, "not-a-real-token", "actor-1")

        variables = json.loads(captured["data"]["variables"])
        self.assertEqual(variables["input"]["message"]["text"], expected)


class TTCCommentClaimLockTests(unittest.TestCase):
    def test_comment_claim_payload_and_rejection_remain_locked(self):
        raw_body = json.dumps(
            {"error": "Bạn chưa comment đúng nội dung"},
            ensure_ascii=False,
        )
        response = FakeResponse(
            raw_body.encode("utf-8"),
            json_result={"error": "Bạn chưa comment đúng nội dung"},
        )
        session = FakeSession(response)
        client = TTCClient(worker_id="claim-lock", log_fn=lambda *_: None)
        client._session.close()
        client._session = session
        client._authenticated = True
        job = normalize_jobs_list(
            TTCJobType.COMMENT.value,
            [raw_comment("Nội dung")],
        )[0]

        result = client.claim_reward(job)

        self.assertEqual(session.post_calls[0][1]["data"], {"id": "42"})
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "TTC_REJECTED")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["server_raw_text"], raw_body)

if __name__ == "__main__":
    unittest.main()
