import ast
import contextlib
import copy
import inspect
import textwrap
import unittest
from unittest.mock import MagicMock, call, patch

import api_ttc
from api_ttc import TTCAuthError, TTCRateLimitError, TTCResponseError


def make_profile(*, with_facebook=True):
    profile = {
        "profile_id": "profile-1",
        "name": "Primary",
        "auth_token": "  ttc-token  ",
        "fb_accounts": [],
        "run_config": api_ttc.normalize_run_config(),
    }
    if with_facebook:
        profile["fb_accounts"] = [
            {"email": "first", "token": "facebook-token", "secret": ""}
        ]
    return profile


def preview_job(kind, suffix="1"):
    is_comment = kind == "ttc_comment"
    return {
        "job_id": f"{kind}:{suffix}",
        "buff_id": f"{kind}:{suffix}",
        "type": "like_poster" if is_comment else "like_facebook",
        "action_group": "comment" if is_comment else "reaction",
        "object_id": f"object-{suffix}",
        "link": f"https://facebook.com/object-{suffix}",
        "reaction_type": "" if is_comment else "LOVE",
        "comment_text": "hello TTC" if is_comment else "",
        "ttc_source": kind,
        "idpost": f"post-{suffix}",
        "_ttc_job": {"idpost": f"post-{suffix}"},
    }


class TTCAuxiliaryRuntimeTests(unittest.TestCase):
    def test_balance_uses_fresh_ttc_account_info_without_facebook_or_profile_mutation(self):
        profile = make_profile(with_facebook=False)
        snapshot = copy.deepcopy(profile)
        api = MagicMock()
        api.get_account_info.return_value = {
            "user": "tester",
            "balance": 321,
            "authenticated": True,
        }

        with (
            patch.object(api_ttc, "API_TTC", return_value=api) as ttc_class,
            patch.object(api_ttc, "profile_run_lock", return_value=contextlib.nullcontext()),
            patch.object(api_ttc, "print_info") as print_info,
            patch.object(api_ttc, "print_error") as print_error,
        ):
            api_ttc.check_balance(profile)

        ttc_class.assert_called_once_with("ttc-token", worker_id="profile-1:balance")
        api.login.assert_called_once_with()
        api.get_account_info.assert_called_once_with()
        api.close.assert_called_once_with()
        print_error.assert_not_called()
        self.assertIn("321", print_info.call_args.args[0])
        self.assertEqual(profile, snapshot)

    def test_balance_reports_ttc_errors_and_closes_without_fake_zero(self):
        for error in (
            TTCAuthError("bad token"),
            TTCRateLimitError("slow down"),
            TTCResponseError("bad response"),
        ):
            with self.subTest(error=type(error).__name__):
                api = MagicMock()
                api.get_account_info.side_effect = error
                with (
                    patch.object(api_ttc, "API_TTC", return_value=api),
                    patch.object(api_ttc, "profile_run_lock", return_value=contextlib.nullcontext()),
                    patch.object(api_ttc, "print_info") as print_info,
                    patch.object(api_ttc, "print_error") as print_error,
                ):
                    api_ttc.check_balance(make_profile(with_facebook=False))

                api.login.assert_called_once_with()
                api.close.assert_called_once_with()
                print_info.assert_not_called()
                self.assertIn(str(error), print_error.call_args.args[0])
                self.assertNotIn("0 xu", print_error.call_args.args[0])

    def test_show_jobs_uses_uid_and_only_supported_ttc_groups_without_actions(self):
        profile = make_profile()
        reaction = preview_job("ttc_reaction_vip_re", "1")
        comment = preview_job("ttc_comment", "2")
        api = MagicMock()
        action = MagicMock()
        events = []
        api.login.side_effect = lambda: events.append("login")
        action.get_info.side_effect = lambda token: events.append("facebook_info") or ("998877", "OK")
        api.set_facebook_uid.side_effect = lambda uid: events.append("set_uid")
        fetch_results = [
            {"jobs": [reaction], "no_job": []},
            {"jobs": [comment], "no_job": []},
        ]

        def record_fetch(group):
            events.append(f"fetch:{group}")
            return fetch_results.pop(0)

        api.fetch_jobs_for_legacy_type.side_effect = record_fetch

        with (
            patch.object(api_ttc, "API_TTC", return_value=api) as ttc_class,
            patch.object(api_ttc, "AUTO_Action", return_value=action),
            patch.object(api_ttc, "profile_run_lock", return_value=contextlib.nullcontext()),
            patch.object(api_ttc.time, "sleep") as sleep,
            patch.object(api_ttc, "countdown") as countdown,
            patch.object(api_ttc, "print_info") as print_info,
            patch("builtins.print") as output,
        ):
            api_ttc.show_jobs_for_profile(profile)

        ttc_class.assert_called_once_with("ttc-token", worker_id="profile-1:preview")
        action.get_info.assert_called_once_with("facebook-token")
        api.set_facebook_uid.assert_called_once_with("998877")
        self.assertEqual(
            api.fetch_jobs_for_legacy_type.call_args_list,
            [call("like_facebook"), call("like_poster")],
        )
        self.assertEqual(
            events,
            ["login", "facebook_info", "set_uid", "fetch:like_facebook", "fetch:like_poster"],
        )
        api.submit_job.assert_not_called()
        action.auto_reaction.assert_not_called()
        action.auto_cmt.assert_not_called()
        sleep.assert_not_called()
        countdown.assert_not_called()
        api.close.assert_called_once_with()
        self.assertIn("2", print_info.call_args.args[0])
        rendered = "\n".join(str(item) for item in output.call_args_list)
        self.assertIn("ttc_reaction_vip_re", rendered)
        self.assertIn("LOVE", rendered)
        self.assertIn("ttc_comment", rendered)
        self.assertIn("hello TTC", rendered)
        self.assertNotIn("_ttc_job", rendered)

    def test_show_jobs_partial_and_all_no_job_are_one_shot(self):
        for results, expected_count in (
            (
                [
                    {"jobs": [], "no_job": [{"job_type": "reaction", "countdown": 30}]},
                    {"jobs": [preview_job("ttc_comment")], "no_job": []},
                ],
                1,
            ),
            (
                [
                    {"jobs": [], "no_job": [{"job_type": "reaction", "countdown": 30}]},
                    {"jobs": [], "no_job": [{"job_type": "comment", "countdown": 40}]},
                ],
                0,
            ),
        ):
            with self.subTest(expected_count=expected_count):
                api = MagicMock()
                api.fetch_jobs_for_legacy_type.side_effect = results
                action = MagicMock()
                action.get_info.return_value = ("uid", "OK")
                with (
                    patch.object(api_ttc, "API_TTC", return_value=api),
                    patch.object(api_ttc, "AUTO_Action", return_value=action),
                    patch.object(api_ttc, "profile_run_lock", return_value=contextlib.nullcontext()),
                    patch.object(api_ttc.time, "sleep") as sleep,
                    patch.object(api_ttc, "countdown") as countdown,
                    patch.object(api_ttc, "print_info") as print_info,
                    patch("builtins.print"),
                ):
                    api_ttc.show_jobs_for_profile(make_profile())

                sleep.assert_not_called()
                countdown.assert_not_called()
                api.close.assert_called_once_with()
                messages = [item.args[0] for item in print_info.call_args_list]
                if expected_count:
                    self.assertTrue(any(str(expected_count) in message for message in messages))
                    self.assertFalse(any("Không có job" in message for message in messages))
                else:
                    self.assertTrue(any("Không có job" in message for message in messages))

    def test_show_jobs_reports_real_ttc_errors_and_closes(self):
        for error in (
            TTCAuthError("bad token"),
            TTCRateLimitError("slow down"),
            TTCResponseError("bad response"),
        ):
            with self.subTest(error=type(error).__name__):
                api = MagicMock()
                api.fetch_jobs_for_legacy_type.side_effect = error
                action = MagicMock()
                action.get_info.return_value = ("uid", "OK")
                with (
                    patch.object(api_ttc, "API_TTC", return_value=api),
                    patch.object(api_ttc, "AUTO_Action", return_value=action),
                    patch.object(api_ttc, "profile_run_lock", return_value=contextlib.nullcontext()),
                    patch.object(api_ttc, "print_info") as print_info,
                    patch.object(api_ttc, "print_error") as print_error,
                    patch("builtins.print"),
                ):
                    api_ttc.show_jobs_for_profile(make_profile())

                api.close.assert_called_once_with()
                self.assertFalse(
                    any("Không có job" in item.args[0] for item in print_info.call_args_list)
                )
                self.assertIn(str(error), print_error.call_args.args[0])

    def test_platform_labels_change_but_persistence_contract_stays_compatible(self):
        display_functions = (
            api_ttc.create_profile,
            api_ttc.view_ttc_token,
            api_ttc.profile_manager,
            api_ttc.run_active_profile,
            api_ttc.check_balance,
            api_ttc.show_jobs_for_profile,
            api_ttc.print_menu,
            api_ttc.main,
        )
        for function in display_functions:
            with self.subTest(function=function.__name__):
                tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
                display_strings = [
                    node.value
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                ]
                self.assertFalse(
                    any("bumx" in value.casefold() for value in display_strings)
                )

        source = inspect.getsource(api_ttc)
        self.assertIn('profile.get(\'auth_token\')', source)
        self.assertIn('"BUMX_TERMUX_DATA_FILE"', source)
        self.assertIn('"bumx_termux_data.json"', source)
        self.assertIn('".bumx_runtime"', source)


if __name__ == "__main__":
    unittest.main()
