import copy
import inspect
import time
import unittest
from unittest.mock import patch

import api_bumx
from TTC_API import (
    TTCAuthError,
    TTCJobType,
    TTCNoJobAvailable,
    TTCRateLimitError,
    TTCResponseError,
)
from ttc_adapter import API_TTC


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
