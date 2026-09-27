import io
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from unittest.mock import patch

import api_ttc


class UX3MenuTests(unittest.TestCase):
    def setUp(self):
        self.old_session_profile_id = api_ttc.SESSION_PROFILE_ID
        self.profile = {
            "profile_id": "profile-1",
            "name": "Primary",
            "auth_token": "ttc-secret-token",
            "fb_accounts": [
                {
                    "email": "10001",
                    "token": "facebook-secret-token",
                    "secret": "two-factor-secret",
                    "cookie": "cookie-secret",
                },
                {
                    "email": "10002",
                    "token": "facebook-disabled-token",
                },
            ],
            "run_config": api_ttc.normalize_run_config({
                "selected_fb_account_keys": ["email:10001"],
            }),
        }
        self.data = {
            "version": api_ttc.DATA_VERSION,
            "active_profile_id": "profile-1",
            "profiles": [self.profile],
        }
        api_ttc.SESSION_PROFILE_ID = "profile-1"

    def tearDown(self):
        api_ttc.SESSION_PROFILE_ID = self.old_session_profile_id

    def test_facebook_menu_routes_every_operation_and_back(self):
        with patch("builtins.input", side_effect=[
            "1", "2", "3", "4", "5", "6", "0",
        ]), patch.object(
            api_ttc, "load_database", return_value=self.data
        ), patch.object(
            api_ttc, "account_list_menu"
        ) as account_list, patch.object(
            api_ttc, "add_account_menu"
        ) as add, patch.object(
            api_ttc, "toggle_account_selection"
        ) as toggle, patch.object(
            api_ttc, "relogin_account"
        ) as relogin, patch.object(
            api_ttc, "edit_account"
        ) as edit, patch.object(
            api_ttc, "delete_account"
        ) as delete:
            api_ttc.account_manager("profile-1")

        account_list.assert_called_once_with(self.profile)
        add.assert_called_once_with("profile-1")
        toggle.assert_called_once_with(self.profile)
        relogin.assert_called_once_with(self.profile)
        edit.assert_called_once_with(self.profile)
        delete.assert_called_once_with(self.profile)

    def test_add_menu_routes_batch_login_manual_token_and_back(self):
        with patch("builtins.input", side_effect=[
            "1", "10001|password|2fa", "2", "manual-token", "0",
        ]), patch.object(
            api_ttc, "batch_login_accounts"
        ) as batch, patch.object(
            api_ttc, "add_manual_account", return_value=True
        ) as manual:
            api_ttc.add_account_menu("profile-1")

        batch.assert_called_once_with("profile-1", "10001|password|2fa")
        manual.assert_called_once_with("profile-1", "manual-token")

    def test_back_from_add_returns_to_facebook_menu(self):
        output = io.StringIO()
        with patch("builtins.input", side_effect=["2", "0", "0"]), patch.object(
            api_ttc, "load_database", return_value=self.data
        ), redirect_stdout(output):
            api_ttc.account_manager("profile-1")

        self.assertEqual(output.getvalue().count("QUẢN LÝ FACEBOOK"), 2)

    def test_account_list_keeps_details_reachable_without_rendering_secrets(self):
        output = io.StringIO()
        with patch("builtins.input", side_effect=["1", "0"]), patch.object(
            api_ttc, "show_facebook_account_details"
        ) as details, redirect_stdout(output):
            api_ttc.account_list_menu(self.profile)

        details.assert_called_once_with(self.profile)
        rendered = output.getvalue()
        self.assertIn("[1] ✅ 10001", rendered)
        self.assertIn("[2] ❌ 10002", rendered)
        self.assertNotIn("facebook-secret-token", rendered)
        self.assertNotIn("two-factor-secret", rendered)
        self.assertNotIn("cookie-secret", rendered)

    def test_ttc_menu_routes_every_normal_operation_and_back(self):
        output = io.StringIO()
        with patch("builtins.input", side_effect=[
            "1", "2", "3", "4", "5", "6", "0",
        ]), patch.object(
            api_ttc, "load_database", return_value=self.data
        ), patch.object(
            api_ttc, "select_profile_menu"
        ) as select, patch.object(
            api_ttc, "create_profile"
        ) as create, patch.object(
            api_ttc, "view_ttc_token"
        ) as token, patch.object(
            api_ttc, "check_balance"
        ) as balance, patch.object(
            api_ttc, "show_jobs_for_profile"
        ) as jobs, patch.object(
            api_ttc, "profile_manager"
        ) as profiles, redirect_stdout(output):
            api_ttc.ttc_manager()

        select.assert_called_once_with(self.data)
        create.assert_called_once_with(self.data)
        token.assert_called_once_with(self.data)
        balance.assert_called_once_with(self.profile)
        jobs.assert_called_once_with(self.profile)
        profiles.assert_called_once_with(self.data)
        self.assertNotIn("ttc-secret-token", output.getvalue())

    def test_profile_management_routes_rename_copy_delete_and_back(self):
        with patch("builtins.input", side_effect=["1", "2", "3", "0"]), patch.object(
            api_ttc, "load_database", side_effect=lambda: deepcopy(self.data)
        ), patch.object(
            api_ttc, "rename_profile_menu"
        ) as rename, patch.object(
            api_ttc, "copy_facebook_accounts_from_profile"
        ) as copy, patch.object(
            api_ttc, "delete_profile_menu"
        ) as delete:
            api_ttc.profile_manager(self.data)

        rename.assert_called_once_with(self.data)
        copy.assert_called_once_with("profile-1")
        delete.assert_called_once_with(self.data)

    def test_select_rename_and_delete_helpers_preserve_core_routes(self):
        with patch("builtins.input", return_value="1"), patch.object(
            api_ttc, "select_profile_as_default"
        ) as select:
            api_ttc.select_profile_menu(self.data)
        select.assert_called_once_with("profile-1")

        with patch("builtins.input", side_effect=["1", "Renamed"]), patch.object(
            api_ttc, "rename_profile"
        ) as rename:
            api_ttc.rename_profile_menu(self.data)
        rename.assert_called_once_with("profile-1", "Renamed")

        with patch("builtins.input", side_effect=["1", "y"]), patch.object(
            api_ttc, "delete_profile", return_value=(self.data, None)
        ) as delete:
            api_ttc.delete_profile_menu(self.data)
        delete.assert_called_once_with("profile-1")

    def test_main_and_ttc_headers_only_render_secret_status(self):
        output = io.StringIO()
        with redirect_stdout(output):
            api_ttc.print_menu(self.profile)
        with patch("builtins.input", return_value="0"), patch.object(
            api_ttc, "load_database", return_value=self.data
        ), redirect_stdout(output):
            api_ttc.ttc_manager()

        rendered = output.getvalue()
        self.assertIn("TTC token : Có", rendered)
        self.assertIn("Facebook  : 1 bật / 2 tổng", rendered)
        self.assertNotIn("ttc-secret-token", rendered)
        self.assertNotIn("facebook-secret-token", rendered)


if __name__ == "__main__":
    unittest.main()
