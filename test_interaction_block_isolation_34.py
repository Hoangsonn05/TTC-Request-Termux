import threading
import unittest
from datetime import datetime
from unittest.mock import patch

import requests

import GUI
import api_ttc as core


SECURITY_CHECK_MESSAGE = (
    "Sorry, this feature isn't available right now — "
    "A security check is required to proceed."
)


class FakeResponse:
    def __init__(self, payload, text=""):
        self._payload = payload
        self.text = text or str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def post(self, **_kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response

    def close(self):
        pass


def scheduler_job(job_id):
    original = {
        "job_id": job_id,
        "idpost": job_id,
        "object_id": job_id,
        "post_id": job_id,
        "reaction_type": "LIKE",
        "ttc_source": "ttc_like_post_vip_re",
    }
    return {
        "type": "like_facebook",
        "_ttc_job": original,
    }


class FakeApi:
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.fetch_count = 0
        self.claims = []
        self.bound_uid = None

    def fetch_jobs_for_legacy_type(self, _legacy_type):
        self.fetch_count += 1
        jobs, self.jobs = self.jobs, []
        return {
            "jobs": jobs,
            "no_job": [],
            "cooldowns": [],
            "permanent_skips": [],
        }

    def bind_facebook_uid(self, uid):
        self.bound_uid = uid

    def set_facebook_uid(self, uid):
        self.bound_uid = uid

    def submit_job(self, job):
        self.claims.append(job)
        return {"success": True, "earned_coin": 100}

    def get_account_info(self):
        return {}


class RecordingStopEvent:
    def __init__(self):
        self.wait_calls = []

    def is_set(self):
        return False

    def wait(self, seconds):
        self.wait_calls.append(seconds)
        return False


class BlockingAction:
    def __init__(self, blocked_at=None):
        self.blocked_at = blocked_at
        self.attempted = []

    def auto_reaction(self, post_id, _reaction, _token, _uid):
        self.attempted.append(str(post_id))
        raise core.FacebookInteractionBlocked(
            SECURITY_CHECK_MESSAGE,
            blocked_at=self.blocked_at,
        )


class OrdinaryFailureAction:
    def __init__(self):
        self.attempted = []
        self.last_reaction_evidence = {
            "success": False,
            "classification": "NON_RETRYABLE_RESPONSE",
            "attempts": 1,
        }

    def auto_reaction(self, post_id, _reaction, _token, _uid):
        self.attempted.append(str(post_id))
        return False


class InteractionClassifierTests(unittest.TestCase):
    def test_exact_and_punctuation_variants_are_blocked(self):
        variants = [
            SECURITY_CHECK_MESSAGE,
            "  Sorry,   this feature isn’t available right now -\n"
            "A security check is required to proceed.  ",
        ]
        for message in variants:
            with self.subTest(message=message):
                self.assertEqual(
                    core._facebook_interaction_block_message([
                        {"description": message}
                    ]),
                    message.strip(),
                )

    def test_ordinary_graphql_and_network_errors_are_not_blocked(self):
        ordinary = [
            {"summary": "Something went wrong", "description": "Please try again."},
            {"message": "Permission denied"},
            {"description": "A generic security error occurred"},
        ]
        self.assertIsNone(core._facebook_interaction_block_message(ordinary))

        action = core.AUTO_Action()
        action._requests = FakeSession(error=requests.Timeout("timeout"))
        self.assertFalse(action.auto_reaction("1", "LIKE", "token", "uid"))
        self.assertEqual(action.last_reaction_evidence["classification"], "NETWORK_UNKNOWN")

    def test_reaction_and_comment_raise_typed_block_with_action_timestamp(self):
        payload = {"errors": [{"description": SECURITY_CHECK_MESSAGE}]}
        for method_name, args in (
            ("auto_reaction", ("1", "LIKE", "token", "uid")),
            ("auto_cmt", ("1", "hello", "token", "uid")),
        ):
            with self.subTest(method=method_name):
                action = core.AUTO_Action()
                action._requests = FakeSession(FakeResponse(payload))
                with self.assertRaises(core.FacebookInteractionBlocked) as caught:
                    getattr(action, method_name)(*args)
                self.assertEqual(caught.exception.classification, core.INTERACTION_BLOCKED)
                self.assertIsInstance(caught.exception.blocked_at, datetime)
                self.assertEqual(action._requests.calls, 1)


class RuntimeContractTests(unittest.TestCase):
    def run_jobs(self, action, *, failed_delay=(1, 1)):
        api = FakeApi([scheduler_job("101"), scheduler_job("102")])
        stop_event = RecordingStopEvent()
        events = []
        transaction_sleeps = []
        kwargs = {
            "max_jobs": 2,
            "job_types": {"like_facebook"},
            "like_batch_min": 2,
            "like_batch_max": 2,
            "failed_delay_range": failed_delay,
            "stop_event": stop_event,
            "event_callback": events.append,
            "action": action,
            "transaction_sleep_fn": transaction_sleeps.append,
            "configure_remote_uid": False,
            "facebook_live_result": {
                "status": core.FACEBOOK_LIVE,
                "uid": "123",
            },
        }
        return api, stop_event, events, transaction_sleeps, kwargs

    def test_block_skips_claim_failed_delay_and_next_job(self):
        blocked_at = datetime.now().astimezone()
        action = BlockingAction(blocked_at)
        api, stop_event, events, transaction_sleeps, kwargs = self.run_jobs(action)

        with self.assertRaises(core.FacebookInteractionBlocked):
            core.run_ttc_jobs(api, "fb-token", **kwargs)

        self.assertEqual(action.attempted, ["101"])
        self.assertEqual(api.claims, [])
        self.assertEqual(stop_event.wait_calls, [])
        self.assertEqual(transaction_sleeps, [])
        self.assertFalse(any(event["type"] == "delay" for event in events))

    def test_ordinary_failure_keeps_failed_delay_and_next_job(self):
        action = OrdinaryFailureAction()
        api, stop_event, events, _transaction_sleeps, kwargs = self.run_jobs(action)
        core.run_ttc_jobs(api, "fb-token", **kwargs)

        self.assertEqual(action.attempted, ["101", "102"])
        self.assertEqual(api.claims, [])
        self.assertEqual(stop_event.wait_calls, [1])
        self.assertTrue(any(event["type"] == "delay" for event in events))

    def test_account_runtime_emits_block_event_without_completed_or_error(self):
        blocked_at = datetime.now().astimezone()
        events = []

        class RuntimeApi(FakeApi):
            def __init__(self, _token, **_kwargs):
                super().__init__([scheduler_job("201"), scheduler_job("202")])

            def login(self):
                return {"balance": 500}

            def close(self):
                pass

        class RuntimeAction(BlockingAction):
            def __init__(self):
                super().__init__(blocked_at)

            def check_live(self, _token):
                return core.FacebookLiveCheckResult(core.FACEBOOK_LIVE, uid="123")

            def close(self):
                pass

        config = dict(core.DEFAULT_RUN_CONFIG)
        config.update({
            "continuous": True,
            "max_jobs_per_acc": 2,
            "like_batch_min": 2,
            "like_batch_max": 2,
            "job_types": ["like_facebook"],
        })
        runtime = core.TTCAccountRuntime(
            "profile-1",
            "ttc-token",
            {"email": "a@example.com", "token": "fb-token"},
            config,
            event_callback=events.append,
            api_factory=RuntimeApi,
            action_factory=RuntimeAction,
            transaction_sleep_fn=lambda _seconds: None,
        )

        result = runtime.run()

        block_events = [
            event for event in events
            if event["type"] == core.INTERACTION_BLOCKED_EVENT
        ]
        self.assertEqual(len(block_events), 1)
        self.assertEqual(block_events[0]["blocked_at"], blocked_at.isoformat())
        self.assertEqual(
            block_events[0]["status"],
            f"Tài khoản bị block tương tác lúc {blocked_at.strftime('%d/%m/%Y %H:%M:%S')}",
        )
        self.assertEqual(result["interaction_blocked_at"], blocked_at.isoformat())
        self.assertFalse(any(event.get("status") == "Completed" for event in events))
        self.assertFalse(any(event["type"] == "error" for event in events))

    def test_cli_continuous_excludes_blocked_account_and_new_run_resets(self):
        accounts = [
            {"email": "a@example.com", "token": "A"},
            {"email": "b@example.com", "token": "B"},
            {"email": "c@example.com", "token": "C"},
        ]
        seen = []

        class EndSyntheticRun(BaseException):
            pass

        class MultiApi:
            def __init__(self, _token, **_kwargs):
                pass

            def use_fetch_state(self, _state):
                pass

            def login(self):
                return {}

            def close(self):
                pass

        def first_session(_api, token, **_kwargs):
            seen.append(token)
            if token == "A":
                raise core.FacebookInteractionBlocked(SECURITY_CHECK_MESSAGE)
            if token == "C" and seen.count("C") == 2:
                raise EndSyntheticRun()
            return 0

        with (
            patch.object(core, "API_TTC", MultiApi),
            patch.object(core, "run_ttc_jobs", side_effect=first_session),
            patch.object(core, "countdown"),
            self.assertRaises(EndSyntheticRun),
        ):
            core.run_multi_tokens(
                "ttc-token", accounts, continuous=True,
                account_delay_min=0, account_delay_max=0,
            )
        self.assertEqual(seen, ["A", "B", "C", "B", "C"])

        new_session_seen = []
        with (
            patch.object(core, "API_TTC", MultiApi),
            patch.object(
                core, "run_ttc_jobs",
                side_effect=lambda _api, token, **_kwargs:
                new_session_seen.append(token) or 0,
            ),
            patch.object(core, "countdown"),
        ):
            core.run_multi_tokens(
                "ttc-token", accounts, continuous=False,
                account_delay_min=0, account_delay_max=0,
            )
        self.assertEqual(new_session_seen, ["A", "B", "C"])


class FakeTimer:
    def __init__(self, *_args):
        self.started = []
        self.active = False

    def setSingleShot(self, _value):
        pass

    class Timeout:
        def connect(self, _callback):
            pass

    @property
    def timeout(self):
        return self.Timeout()

    def start(self, milliseconds):
        self.started.append(milliseconds)
        self.active = True

    def stop(self):
        self.active = False

    def isActive(self):
        return self.active

    def deleteLater(self):
        pass


class FakeLease:
    def acquire(self):
        pass

    def release(self):
        pass


class SchedulerHarness:
    _runtime_key = staticmethod(GUI.MainWindow._runtime_key)

    def __init__(self, run_state=None):
        self.profile_runs = {} if run_state is None else {"p": run_state}
        self.pending_starts = set()
        self.workers = {}
        self.profile_account_delays = {}
        self.account_statuses = {}
        self.account_widgets = {}
        self.view_profile_id = "not-p"
        self.active_delays = {}
        self.endpoint_cooldowns = {}
        self.logs = []
        self.profiles_by_id = {"p": {"name": "Profile P"}}
        self.core = type("Core", (), {"ProfileRunLease": lambda _profile_id: FakeLease()})

    def log(self, *args, **kwargs):
        self.logs.append((args, kwargs))

    def _clear_runtime_waits(self, _runtime_key):
        pass

    def _ensure_live_wait_timer(self):
        pass

    def _render_active_delays(self):
        pass

    def _update_aggregate(self):
        pass

    def _update_profile_card(self, _profile_id):
        pass

    def _update_controls(self):
        pass

    def _release_profile_if_idle(self, _profile_id):
        pass

    def _schedule_next_queued(self, _profile_id):
        return False


class GuiSessionIsolationTests(unittest.TestCase):
    def make_run_state(self):
        return {
            "lease": FakeLease(),
            "config": {"continuous": True, "account_delay_min": 0, "account_delay_max": 0},
            "account_template": (("p", "A"), ("p", "B"), ("p", "C")),
            "queue": [("p", "B"), ("p", "C")],
            "cycle": 1,
            "cycle_logged": 1,
            "stopping": False,
            "next_timer": FakeTimer(),
            "blocked_accounts": set(),
        }

    def test_block_event_excludes_only_account_from_all_later_cycles(self):
        harness = SchedulerHarness(self.make_run_state())
        event = {
            "type": core.INTERACTION_BLOCKED_EVENT,
            "message": "Tài khoản bị block tương tác lúc 31/08/2026 21:15:03",
            "status": "Tài khoản bị block tương tác lúc 31/08/2026 21:15:03",
            "blocked_at": "2026-08-31T21:15:03+07:00",
        }

        GUI.MainWindow._on_worker_event(harness, "p", "A", event)

        self.assertEqual(harness.profile_runs["p"]["blocked_accounts"], {"A"})
        self.assertFalse(harness.profile_runs["p"]["stopping"])
        self.assertEqual(harness.profile_runs["p"]["queue"], [("p", "B"), ("p", "C")])
        self.assertEqual(harness.account_statuses[("p", "A")], event["status"])

        harness.profile_runs["p"]["queue"].clear()
        self.assertTrue(GUI.MainWindow._schedule_next_queued(harness, "p"))
        self.assertEqual(harness.profile_runs["p"]["queue"], [("p", "B"), ("p", "C")])
        harness.profile_runs["p"]["queue"].clear()
        self.assertTrue(GUI.MainWindow._schedule_next_queued(harness, "p"))
        self.assertEqual(harness.profile_runs["p"]["queue"], [("p", "B"), ("p", "C")])

    def test_job_worker_records_structured_block_without_setting_stop(self):
        event = {
            "type": core.INTERACTION_BLOCKED_EVENT,
            "status": "Tài khoản bị block tương tác lúc 31/08/2026 21:15:03",
        }
        signal = type("Signal", (), {"emit": lambda self, *_args: None})()
        worker = type("WorkerHarness", (), {
            "profile_id": "p",
            "account_key": "A",
            "event_signal": signal,
            "_error_event_forwarded": False,
            "interaction_blocked_event": None,
            "stop_event": threading.Event(),
        })()

        GUI.JobWorker._forward_event(worker, event)

        self.assertEqual(worker.interaction_blocked_event, event)
        self.assertFalse(worker.stop_event.is_set())

    def test_new_profile_run_gets_a_fresh_blocked_set(self):
        harness = SchedulerHarness()
        with patch.object(GUI, "QTimer", FakeTimer):
            first = GUI.MainWindow._ensure_profile_run(harness, "p", {})
            first["blocked_accounts"].add("A")
            harness.profile_runs.pop("p")
            second = GUI.MainWindow._ensure_profile_run(harness, "p", {})
        self.assertEqual(second["blocked_accounts"], set())

    def test_worker_finish_preserves_blocked_status(self):
        run_state = self.make_run_state()
        run_state["blocked_accounts"].add("A")
        harness = SchedulerHarness(run_state)
        runtime_key = ("p", "A")
        status = "Tài khoản bị block tương tác lúc 31/08/2026 21:15:03"
        event = {
            "type": core.INTERACTION_BLOCKED_EVENT,
            "status": status,
            "message": status,
            "blocked_at": "2026-08-31T21:15:03+07:00",
        }
        worker = type("Worker", (), {})()
        worker.result = {}
        worker.error = None
        worker.stop_event = threading.Event()
        worker.interaction_blocked_event = event
        worker.deleteLater = lambda: None
        harness.workers[runtime_key] = worker
        harness.session_metrics = {runtime_key: {}}
        harness.account_last_metrics = {}
        harness.completed_profile_metrics = {}
        harness.profile_balances = {}
        harness._closing = False

        GUI.MainWindow._on_worker_finished(harness, "p", "A", worker)

        self.assertEqual(harness.account_statuses[runtime_key], status)


if __name__ == "__main__":
    unittest.main()
