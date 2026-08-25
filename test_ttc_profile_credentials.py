import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import patch

import api_ttc


class TTCProfileCredentialSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        data_file = os.path.join(self.temp_dir.name, "profiles.json")
        legacy_file = os.path.join(self.temp_dir.name, "legacy.json")
        for attribute, value in (
            ("DATA_FILE", data_file),
            ("LEGACY_CONFIG_FILE", legacy_file),
            ("SESSION_PROFILE_ID", None),
        ):
            patcher = patch.object(api_ttc, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _create_record(self, token):
        return api_ttc.create_profile_record("Profile an toàn", token)

    def test_create_uses_visible_input_displays_and_persists_raw_token(self):
        raw_token = "TTC_TEST_SECRET_123456789"
        data = api_ttc._empty_database()
        output = io.StringIO()

        with (
            patch("builtins.input", side_effect=["Profile an toàn", raw_token, "y"]),
            contextlib.redirect_stdout(output),
        ):
            profile = api_ttc.create_profile(data)

        rendered = output.getvalue()
        self.assertIn(raw_token, rendered)
        self.assertEqual(profile["auth_token"], raw_token)
        self.assertEqual(api_ttc.load_database()["profiles"][0]["auth_token"], raw_token)

    def test_view_displays_stored_token(self):
        raw_token = "TTC_VIEW_SECRET_123456789"
        self._create_record(raw_token)
        data = api_ttc.load_database()
        output = io.StringIO()

        with (
            patch("builtins.input", side_effect=["1", "1"]),
            contextlib.redirect_stdout(output),
        ):
            api_ttc.view_ttc_token(data)

        rendered = output.getvalue()
        self.assertIn(raw_token, rendered)

    def test_update_displays_old_and_new_tokens_and_persists_new_raw_token(self):
        old_token = "TTC_OLD_SECRET_123456789"
        new_token = "TTC_NEW_SECRET_987654321"
        self._create_record(old_token)
        data = api_ttc.load_database()
        output = io.StringIO()

        with (
            patch("builtins.input", side_effect=["1", "2", new_token, "y"]),
            contextlib.redirect_stdout(output),
        ):
            api_ttc.view_ttc_token(data)

        rendered = output.getvalue()
        self.assertIn(old_token, rendered)
        self.assertIn(new_token, rendered)
        self.assertEqual(api_ttc.load_database()["profiles"][0]["auth_token"], new_token)

    def test_delete_confirmation_masks_token(self):
        raw_token = "TTC_DELETE_SECRET_123456789"
        self._create_record(raw_token)
        data = api_ttc.load_database()
        output = io.StringIO()

        with (
            patch("builtins.input", side_effect=["6", "1", "y", "7"]),
            contextlib.redirect_stdout(output),
        ):
            api_ttc.profile_manager(data)

        rendered = output.getvalue()
        self.assertNotIn(raw_token, rendered)
        self.assertIn("Profile an toàn", rendered)
        self.assertIn(api_ttc.preview_token(raw_token), rendered)
        self.assertEqual(api_ttc.load_database()["profiles"], [])

    def test_profile_list_omits_token(self):
        raw_token = "TTC_LIST_SECRET_123456789"
        self._create_record(raw_token)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            api_ttc.list_profiles(api_ttc.load_database())

        rendered = output.getvalue()
        self.assertNotIn(raw_token, rendered)
        self.assertNotIn(api_ttc.preview_token(raw_token), rendered)
        self.assertIn("Profile an toàn", rendered)

    def test_short_token_is_fully_masked(self):
        raw_token = "short"
        preview = api_ttc.preview_token(raw_token)

        self.assertEqual(preview, "*" * len(raw_token))
        self.assertNotIn(raw_token, preview)

    def test_runtime_receives_exact_persisted_auth_token(self):
        raw_token = "TTC_RUNTIME_SECRET_123456789"
        profile = self._create_record(raw_token)
        facebook_accounts = [{"email": "uid", "token": "facebook-token"}]

        with (
            patch.object(api_ttc, "selected_accounts", return_value=facebook_accounts),
            patch.object(api_ttc, "profile_run_lock", return_value=contextlib.nullcontext()),
            patch.object(api_ttc, "run_multi_tokens") as run_multi_tokens,
            patch.object(api_ttc, "print_info"),
            patch.object(api_ttc, "print_error"),
        ):
            api_ttc.run_active_profile(profile)

        self.assertEqual(run_multi_tokens.call_args.args[0], raw_token)
        self.assertEqual(set(api_ttc.load_database()["profiles"][0]), {
            "profile_id", "name", "auth_token", "fb_accounts", "run_config"
        })


if __name__ == "__main__":
    unittest.main()
