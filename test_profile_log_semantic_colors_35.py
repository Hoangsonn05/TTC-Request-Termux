import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QTextEdit

import GUI
import api_ttc as core
import test_interaction_block_isolation_34 as task34


class SemanticEventHarness:
    _runtime_key = staticmethod(GUI.MainWindow._runtime_key)

    def __init__(self, profile_ids=("A",)):
        self.core = core
        self.profiles_by_id = {
            profile_id: {"profile_id": profile_id, "name": f"Profile {profile_id}", "fb_accounts": []}
            for profile_id in profile_ids
        }
        self.latest_profile_log = {}
        self.latest_profile_log_semantics = {}
        self.log_text = QTextEdit()
        self.profile_cards = {
            profile_id: GUI.ProfileSummaryCard(profile_id)
            for profile_id in profile_ids
        }
        self.profile_runs = {}
        self.account_widgets = {}
        self.view_profile_id = None
        self.active_delays = {}
        self.endpoint_cooldowns = {}
        self.account_statuses = {}
        self.account_live_states = {}
        self.resolved_uids = {}
        self.session_metrics = {}
        self.profile_balances = {}
        self.profile_errors = set()

    def _account_for_runtime(self, profile_id, account_key):
        return GUI.MainWindow._account_for_runtime(self, profile_id, account_key)

    def log(self, *args, **kwargs):
        return GUI.MainWindow.log(self, *args, **kwargs)

    def _clear_active_delay(self, runtime_key):
        self.active_delays.pop(runtime_key, None)

    def _clear_runtime_waits(self, runtime_key):
        self.active_delays.pop(runtime_key, None)

    def _ensure_live_wait_timer(self):
        pass

    def _render_active_delays(self):
        pass

    def _update_aggregate(self):
        pass

    def _update_profile_card(self, profile_id):
        card = self.profile_cards.get(profile_id)
        if card is None:
            return
        card.update_summary(
            name=self.profiles_by_id[profile_id]["name"],
            status="Idle",
            account_count=0,
            active_count=0,
            pending_count=0,
            jobs=0,
            coin_text="0",
            balance_text="—",
            identities="",
            latest_log=self.latest_profile_log.get(profile_id),
            latest_log_semantic=self.latest_profile_log_semantics.get(profile_id),
            live_status="",
            selected=False,
            run_text="▶ Chạy",
            run_enabled=False,
        )

    def emit(self, profile_id, event):
        GUI.MainWindow._on_worker_event(self, profile_id, "account", dict(event))


class ProfileLogSemanticColorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def assert_log_color(self, harness, profile_id, expected):
        self.assertEqual(
            harness.profile_cards[profile_id].log_label.styleSheet(),
            f"color: {GUI.PROFILE_LOG_SEMANTIC_COLORS[expected]};",
        )

    def test_structured_event_mapping_is_finite_and_does_not_parse_messages(self):
        cases = (
            ({"type": "job_succeeded"}, GUI.PROFILE_LOG_SUCCESS),
            ({"type": "completion", "jobs_succeeded": 1}, GUI.PROFILE_LOG_SUCCESS),
            ({"type": "completion", "jobs_succeeded": 0}, GUI.PROFILE_LOG_DEFAULT),
            ({"type": "delay", "remaining": 8}, GUI.PROFILE_LOG_DELAY),
            (
                {"type": "facebook_action_result", "success": False},
                GUI.PROFILE_LOG_FAILURE,
            ),
            (
                {"type": "facebook_action_result", "success": True},
                GUI.PROFILE_LOG_DEFAULT,
            ),
            (
                {"type": "error", "stage": "facebook_action"},
                GUI.PROFILE_LOG_FAILURE,
            ),
            (
                {"type": core.INTERACTION_BLOCKED_EVENT},
                GUI.PROFILE_LOG_INTERACTION_BLOCKED,
            ),
            (
                {"type": "status", "message": "thất bại block delay thành công"},
                GUI.PROFILE_LOG_DEFAULT,
            ),
        )
        for event, expected in cases:
            with self.subTest(event=event):
                self.assertEqual(
                    GUI._event_profile_log_semantic(event["type"], event),
                    expected,
                )

    def test_success_delay_and_info_reset_rendered_color(self):
        harness = SemanticEventHarness()
        harness.emit("A", {
            "type": "job_succeeded",
            "message": "Đã hoàn thành job 1",
            "jobs_succeeded": 1,
            "session_coin": 100,
        })
        self.assertEqual(harness.latest_profile_log_semantics["A"], GUI.PROFILE_LOG_SUCCESS)
        self.assert_log_color(harness, "A", GUI.PROFILE_LOG_SUCCESS)

        harness.emit("A", {"type": "delay", "message": "Delay", "remaining": 8})
        self.assertEqual(harness.latest_profile_log_semantics["A"], GUI.PROFILE_LOG_DELAY)
        self.assert_log_color(harness, "A", GUI.PROFILE_LOG_DELAY)

        harness.emit("A", {"type": "status", "message": "Đã lên kế hoạch 6 job"})
        self.assertEqual(harness.latest_profile_log_semantics["A"], GUI.PROFILE_LOG_DEFAULT)
        self.assert_log_color(harness, "A", GUI.PROFILE_LOG_DEFAULT)

    def test_failure_then_failed_delay_changes_red_to_blue(self):
        harness = SemanticEventHarness()
        harness.emit("A", {
            "type": "facebook_action_result",
            "message": "Thao tác Facebook thất bại",
            "success": False,
            "level": "error",
        })
        self.assertIn("Thao tác Facebook thất bại", harness.latest_profile_log["A"])
        self.assertEqual(harness.latest_profile_log_semantics["A"], GUI.PROFILE_LOG_FAILURE)
        self.assert_log_color(harness, "A", GUI.PROFILE_LOG_FAILURE)

        harness.emit("A", {"type": "delay", "message": "Chờ job 2", "remaining": 10})
        self.assertEqual(harness.latest_profile_log_semantics["A"], GUI.PROFILE_LOG_DELAY)
        self.assert_log_color(harness, "A", GUI.PROFILE_LOG_DELAY)

    def test_interaction_block_uses_event_message_and_resets_on_info(self):
        harness = SemanticEventHarness()
        message = "Tài khoản bị block tương tác lúc 31/08/2026 21:15:03"
        blocked_at = "2026-08-31T21:15:03+07:00"
        harness.emit("A", {
            "type": core.INTERACTION_BLOCKED_EVENT,
            "message": message,
            "status": message,
            "blocked_at": blocked_at,
        })
        self.assertIn(message, harness.latest_profile_log["A"])
        self.assertNotIn(blocked_at, harness.latest_profile_log["A"])
        self.assertEqual(
            harness.latest_profile_log_semantics["A"],
            GUI.PROFILE_LOG_INTERACTION_BLOCKED,
        )
        self.assert_log_color(harness, "A", GUI.PROFILE_LOG_INTERACTION_BLOCKED)

        harness.emit("A", {"type": "status", "message": "Thông tin bình thường"})
        self.assert_log_color(harness, "A", GUI.PROFILE_LOG_DEFAULT)

    def test_profile_colors_are_isolated(self):
        harness = SemanticEventHarness(("A", "B"))
        harness.emit("A", {
            "type": "job_succeeded",
            "message": "A success",
            "jobs_succeeded": 1,
        })
        harness.emit("B", {
            "type": "facebook_action_result",
            "message": "B failure",
            "success": False,
            "level": "error",
        })
        self.assert_log_color(harness, "A", GUI.PROFILE_LOG_SUCCESS)
        self.assert_log_color(harness, "B", GUI.PROFILE_LOG_FAILURE)

        harness.emit("A", {"type": "delay", "message": "A delay", "remaining": 3})
        self.assert_log_color(harness, "A", GUI.PROFILE_LOG_DELAY)
        self.assert_log_color(harness, "B", GUI.PROFILE_LOG_FAILURE)

    def test_actual_runtime_success_and_failure_events_drive_expected_sequences(self):
        failure_action = task34.OrdinaryFailureAction()
        helper = task34.RuntimeContractTests()
        api, stop_event, events, _transaction_sleeps, kwargs = helper.run_jobs(
            failure_action
        )
        core.run_ttc_jobs(api, "fb-token", **kwargs)

        self.assertEqual(failure_action.attempted, ["101", "102"])
        self.assertEqual(api.claims, [])
        self.assertEqual(stop_event.wait_calls, [1])
        failure_event = next(
            event
            for event in events
            if event["type"] == "facebook_action_result"
            and event["success"] is False
        )
        failed_delay = next(event for event in events if event["type"] == "delay")
        harness = SemanticEventHarness()
        harness.emit("A", failure_event)
        self.assert_log_color(harness, "A", GUI.PROFILE_LOG_FAILURE)
        harness.emit("A", failed_delay)
        self.assert_log_color(harness, "A", GUI.PROFILE_LOG_DELAY)

        class SuccessAction:
            last_reaction_evidence = {
                "success": True,
                "classification": "CONFIRMED",
                "attempts": 1,
            }

            def auto_reaction(self, *_args):
                return True

        success_action = SuccessAction()
        api, stop_event, events, _transaction_sleeps, kwargs = helper.run_jobs(
            success_action
        )
        core.run_ttc_jobs(api, "fb-token", **kwargs)

        self.assertEqual(len(api.claims), 2)
        success_event = next(event for event in events if event["type"] == "job_succeeded")
        normal_delay = next(event for event in events if event["type"] == "delay")
        harness = SemanticEventHarness()
        harness.emit("A", success_event)
        self.assert_log_color(harness, "A", GUI.PROFILE_LOG_SUCCESS)
        harness.emit("A", normal_delay)
        self.assert_log_color(harness, "A", GUI.PROFILE_LOG_DELAY)


if __name__ == "__main__":
    unittest.main()
