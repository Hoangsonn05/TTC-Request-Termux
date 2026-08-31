import unittest
from unittest.mock import Mock, patch

import api_ttc
import GUI


class ProfileFetchCooldownContinuityTests(unittest.TestCase):
    @staticmethod
    def make_api(fetch_state, facebook_uid):
        adapter = object.__new__(api_ttc.API_TTC)
        adapter._client = Mock()
        adapter._fetch_state = fetch_state
        adapter._facebook_uid = facebook_uid
        return adapter

    @staticmethod
    def job(job_type, suffix):
        return {
            "idpost": f"post-{suffix}",
            "object_id": f"object-{suffix}",
            "type": job_type,
        }

    @staticmethod
    def traced_fetch(adapter, client, trace, *, profile_id, account_key,
                     facebook_uid, legacy_type, invocation_id, timestamp):
        before = client.get_jobs.call_count
        with patch("api_ttc.time.monotonic", return_value=timestamp):
            result = adapter.fetch_jobs_for_legacy_type(legacy_type)
        after = client.get_jobs.call_count
        if result["jobs"]:
            classification = "SUCCESS"
        elif result["cooldowns"]:
            classification = "COOLDOWN"
        elif result["permanent_skips"]:
            classification = "VIP_INELIGIBLE"
        elif result["no_job"]:
            classification = "NO_JOB"
        else:
            classification = "ERROR"
        trace.append({
            "profile_id": profile_id,
            "account_key": account_key,
            "facebook_uid": facebook_uid,
            "legacy_type": legacy_type,
            "physical_endpoint": (
                client.get_jobs.call_args_list[-1].args[0].value
                if after - before == 1 else None
            ),
            "fetch_invocation_id": invocation_id,
            "monotonic_timestamp": timestamp,
            "result_classification": classification,
            "physical_get_delta": after - before,
        })
        return result

    def test_profile_handoff_preserves_exact_endpoint_cooldown_until_expiry(self):
        state = api_ttc.TTCFetchState()
        first_type, second_type = api_ttc.API_TTC.REACTION_JOB_TYPES[:2]
        client = Mock()
        client.get_jobs.side_effect = [
            api_ttc.TTCJobCooldown("wait", retry_after=22),
            [self.job(second_type, "account-b")],
            [self.job(first_type, "after-expiry")],
        ]
        trace = []

        account_a = self.make_api(state, "facebook-a")
        account_a._client = client
        first_result = self.traced_fetch(
            account_a, client, trace,
            profile_id="profile-p", account_key="account-a",
            facebook_uid="facebook-a", legacy_type="like_facebook",
            invocation_id="invocation-1", timestamp=100.0,
        )

        account_b = self.make_api(state, "facebook-b")
        account_b._client = client
        second_result = self.traced_fetch(
            account_b, client, trace,
            profile_id="profile-p", account_key="account-b",
            facebook_uid="facebook-b", legacy_type="like_facebook",
            invocation_id="invocation-2", timestamp=105.0,
        )

        account_c = self.make_api(state, "facebook-c")
        account_c._client = client
        third_result = self.traced_fetch(
            account_c, client, trace,
            profile_id="profile-p", account_key="account-c",
            facebook_uid="facebook-c", legacy_type="like_facebook",
            invocation_id="invocation-3", timestamp=123.0,
        )

        self.assertEqual(first_type.value, first_result["cooldowns"][0]["job_type"])
        self.assertEqual(22, first_result["cooldowns"][0]["remaining"])
        self.assertEqual(1, len(second_result["jobs"]))
        self.assertEqual(1, len(third_result["jobs"]))
        self.assertEqual(
            [first_type, second_type, first_type],
            [call.args[0] for call in client.get_jobs.call_args_list],
        )
        self.assertEqual(
            ["COOLDOWN", "SUCCESS", "SUCCESS"],
            [item["result_classification"] for item in trace],
        )
        self.assertEqual([1, 1, 1], [item["physical_get_delta"] for item in trace])
        self.assertEqual(
            [first_type.value, second_type.value, first_type.value],
            [item["physical_endpoint"] for item in trace],
        )

    def test_vip_permanent_skip_remains_facebook_uid_scoped(self):
        state = api_ttc.TTCFetchState()
        first_type = api_ttc.API_TTC.REACTION_JOB_TYPES[0]
        client = Mock()
        client.get_jobs.side_effect = [
            api_ttc.TTCVipIneligibleError("not eligible"),
            [self.job(first_type, "account-b")],
        ]

        account_a = self.make_api(state, "facebook-a")
        account_a._client = client
        account_a.fetch_jobs_for_legacy_type("like_facebook")

        account_b = self.make_api(state, "facebook-b")
        account_b._client = client
        result_b = account_b.fetch_jobs_for_legacy_type("like_facebook")

        self.assertEqual(1, len(result_b["jobs"]))
        self.assertEqual([first_type, first_type], [call.args[0] for call in client.get_jobs.call_args_list])

    def test_new_profile_session_does_not_inherit_endpoint_cooldown(self):
        first_type = api_ttc.API_TTC.REACTION_JOB_TYPES[0]
        client = Mock()
        client.get_jobs.side_effect = [
            api_ttc.TTCJobCooldown("wait", retry_after=22),
            [self.job(first_type, "new-session")],
        ]

        session_one = self.make_api(api_ttc.TTCFetchState(), "facebook-a")
        session_one._client = client
        session_one.fetch_jobs_for_legacy_type("like_facebook")

        session_two = self.make_api(api_ttc.TTCFetchState(), "facebook-b")
        session_two._client = client
        result = session_two.fetch_jobs_for_legacy_type("like_facebook")

        self.assertEqual(1, len(result["jobs"]))
        self.assertEqual([first_type, first_type], [call.args[0] for call in client.get_jobs.call_args_list])

    def test_like_cooldown_does_not_block_comment_endpoint(self):
        state = api_ttc.TTCFetchState()
        like_type = api_ttc.API_TTC.REACTION_JOB_TYPES[0]
        comment_type = api_ttc.API_TTC.COMMENT_JOB_TYPES[0]
        client = Mock()
        client.get_jobs.side_effect = [
            api_ttc.TTCJobCooldown("wait", retry_after=22),
            [self.job(comment_type, "comment")],
        ]

        account_a = self.make_api(state, "facebook-a")
        account_a._client = client
        account_a.fetch_jobs_for_legacy_type("like_facebook")

        account_b = self.make_api(state, "facebook-b")
        account_b._client = client
        result = account_b.fetch_jobs_for_legacy_type("like_poster")

        self.assertEqual(1, len(result["jobs"]))
        self.assertEqual([like_type, comment_type], [call.args[0] for call in client.get_jobs.call_args_list])

    def test_pure_cooldown_does_not_emit_no_job_log(self):
        endpoint = api_ttc.API_TTC.REACTION_JOB_TYPES[0].value
        events = []

        class CooldownApi:
            def bind_facebook_uid(self, _uid):
                pass

            def fetch_jobs_for_legacy_type(self, _legacy_type):
                return {
                    "jobs": [],
                    "no_job": [],
                    "cooldowns": [{
                        "job_type": endpoint,
                        "retry_after": 22,
                        "remaining": 22,
                        "retry_at": 122.0,
                        "server_raw_text": '{"countdown":22}',
                        "source": "server",
                    }],
                    "permanent_skips": [],
                }

            def get_account_info(self):
                return {"balance": 0}

        api_ttc.run_ttc_jobs(
            CooldownApi(),
            "facebook-token",
            max_jobs=1,
            job_types={"like_facebook"},
            event_callback=events.append,
            configure_remote_uid=False,
            facebook_live_result={"status": api_ttc.FACEBOOK_LIVE, "uid": "facebook-a"},
        )

        messages = [str(event.get("message", "")) for event in events]
        self.assertTrue(any("22 giây chờ TTC" in message for message in messages))
        self.assertFalse(any("Hiện chưa có job TTC phù hợp" in message for message in messages))


class FakeTimer:
    def setSingleShot(self, _value):
        pass

    class Timeout:
        def connect(self, _callback):
            pass

    @property
    def timeout(self):
        return self.Timeout()


class FakeLease:
    def acquire(self):
        pass


class ProfileRunFetchStateLifetimeTests(unittest.TestCase):
    def test_profile_run_reuses_state_and_new_session_gets_fresh_state(self):
        harness = type("Harness", (), {})()
        harness.profile_runs = {}
        harness.profiles_by_id = {"profile": {"name": "Profile"}}
        harness.core = type("Core", (), {"ProfileRunLease": lambda _profile_id: FakeLease()})
        harness.log = lambda *_args, **_kwargs: None

        with patch.object(GUI, "QTimer", lambda _parent: FakeTimer()):
            session_one = GUI.MainWindow._ensure_profile_run(harness, "profile", {})
            same_session = GUI.MainWindow._ensure_profile_run(harness, "profile", {})
            harness.profile_runs.pop("profile")
            session_two = GUI.MainWindow._ensure_profile_run(harness, "profile", {})

        self.assertIs(session_one["fetch_state"], same_session["fetch_state"])
        self.assertIsNot(session_one["fetch_state"], session_two["fetch_state"])

    def test_two_profiles_do_not_share_fetch_state(self):
        harness = type("Harness", (), {})()
        harness.profile_runs = {}
        harness.profiles_by_id = {
            "profile-a": {"name": "A"},
            "profile-b": {"name": "B"},
        }
        harness.core = type("Core", (), {"ProfileRunLease": lambda _profile_id: FakeLease()})
        harness.log = lambda *_args, **_kwargs: None

        with patch.object(GUI, "QTimer", lambda _parent: FakeTimer()):
            profile_a = GUI.MainWindow._ensure_profile_run(harness, "profile-a", {})
            profile_b = GUI.MainWindow._ensure_profile_run(harness, "profile-b", {})

        self.assertIsNot(profile_a["fetch_state"], profile_b["fetch_state"])

    def test_job_worker_forwards_profile_run_fetch_state_to_runtime(self):
        state = api_ttc.TTCFetchState()
        captured = []

        class Runtime:
            def __init__(self, **kwargs):
                captured.append(kwargs["fetch_state"])

            def run(self):
                return {}

        worker = GUI.JobWorker(
            "profile",
            "ttc-token",
            {"email": "a@example.com", "token": "facebook-token"},
            {},
            runtime_factory=Runtime,
            fetch_state=state,
        )
        worker.run()

        self.assertIsNone(worker.error)
        self.assertEqual([state], captured)


if __name__ == "__main__":
    unittest.main()
