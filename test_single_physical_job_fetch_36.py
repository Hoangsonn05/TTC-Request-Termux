import unittest
from unittest.mock import Mock

import api_ttc


class SinglePhysicalJobFetchTests(unittest.TestCase):
    def make_api(self):
        adapter = object.__new__(api_ttc.API_TTC)
        adapter._client = Mock()
        adapter._fetch_state = api_ttc.TTCFetchState()
        adapter._facebook_uid = "facebook-uid"
        return adapter

    @staticmethod
    def job(job_type, suffix="1"):
        return {
            "idpost": f"post-{suffix}",
            "object_id": f"object-{suffix}",
            "type": job_type,
        }

    def test_success_fetches_only_one_physical_endpoint(self):
        adapter = self.make_api()
        first_type = api_ttc.API_TTC.REACTION_JOB_TYPES[0]
        first_job = self.job(first_type, "success")
        adapter._client.get_jobs.return_value = [first_job]

        result = adapter.fetch_jobs_for_legacy_type("like_facebook")

        adapter._client.get_jobs.assert_called_once_with(first_type)
        self.assertEqual([first_job], [job["_ttc_job"] for job in result["jobs"]])

    def test_cooldown_stops_current_invocation_and_next_fetch_uses_another_endpoint(self):
        adapter = self.make_api()
        first_type, second_type = api_ttc.API_TTC.REACTION_JOB_TYPES[:2]
        second_job = self.job(second_type, "after-cooldown")
        adapter._client.get_jobs.side_effect = [
            api_ttc.TTCJobCooldown(
                "wait",
                retry_after=22,
                server_raw_text='{"countdown":22}',
            ),
            [second_job],
        ]

        first_result = adapter.fetch_jobs_for_legacy_type("like_facebook")

        self.assertEqual([first_type], [call.args[0] for call in adapter._client.get_jobs.call_args_list])
        self.assertEqual([], first_result["jobs"])
        self.assertEqual(first_type.value, first_result["cooldowns"][0]["job_type"])
        self.assertIn(
            first_type.value,
            adapter._fetch_state.temporary_retry_at,
        )

        second_result = adapter.fetch_jobs_for_legacy_type("like_facebook")

        self.assertEqual(
            [first_type, second_type],
            [call.args[0] for call in adapter._client.get_jobs.call_args_list],
        )
        self.assertEqual([second_job], [job["_ttc_job"] for job in second_result["jobs"]])

    def test_vip_ineligible_stops_current_invocation_and_permanently_skips_only_selected_endpoint(self):
        adapter = self.make_api()
        first_type, second_type = api_ttc.API_TTC.REACTION_JOB_TYPES[:2]
        second_job = self.job(second_type, "after-skip")
        adapter._client.get_jobs.side_effect = [
            api_ttc.TTCVipIneligibleError(
                "ineligible",
                server_raw_text="Nick chua du dieu kien lam nhiem vu vip",
            ),
            [second_job],
        ]

        first_result = adapter.fetch_jobs_for_legacy_type("like_facebook")

        self.assertEqual([first_type], [call.args[0] for call in adapter._client.get_jobs.call_args_list])
        self.assertEqual([], first_result["jobs"])
        self.assertEqual(first_type.value, first_result["permanent_skips"][0]["job_type"])
        self.assertEqual(
            {("facebook-uid", first_type.value)},
            adapter._fetch_state.permanent_skip,
        )

        adapter.fetch_jobs_for_legacy_type("like_facebook")

        self.assertEqual(
            [first_type, second_type],
            [call.args[0] for call in adapter._client.get_jobs.call_args_list],
        )

    def test_no_job_returns_without_scanning_and_next_fetch_is_fair(self):
        adapter = self.make_api()
        first_type, second_type = api_ttc.API_TTC.REACTION_JOB_TYPES[:2]
        adapter._client.get_jobs.side_effect = [
            api_ttc.TTCNoJobAvailable("empty", countdown=0),
            api_ttc.TTCNoJobAvailable("empty", countdown=0),
        ]

        first_result = adapter.fetch_jobs_for_legacy_type("like_facebook")
        self.assertEqual([first_type], [call.args[0] for call in adapter._client.get_jobs.call_args_list])
        self.assertEqual(first_type.value, first_result["no_job"][0]["job_type"])

        adapter.fetch_jobs_for_legacy_type("like_facebook")

        self.assertEqual(
            [first_type, second_type],
            [call.args[0] for call in adapter._client.get_jobs.call_args_list],
        )

    def test_repeated_successful_fetches_rotate_and_never_fan_out(self):
        adapter = self.make_api()
        physical_types = api_ttc.API_TTC.REACTION_JOB_TYPES
        adapter._client.get_jobs.side_effect = [
            [self.job(job_type, str(index))]
            for index, job_type in enumerate(physical_types, start=1)
        ]

        invocation_traces = []
        for _ in physical_types:
            before = adapter._client.get_jobs.call_count
            adapter.fetch_jobs_for_legacy_type("like_facebook")
            after = adapter._client.get_jobs.call_count
            invocation_traces.append(after - before)

        self.assertEqual([1] * len(physical_types), invocation_traces)
        self.assertEqual(
            list(physical_types),
            [call.args[0] for call in adapter._client.get_jobs.call_args_list],
        )


if __name__ == "__main__":
    unittest.main()
