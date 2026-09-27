import ast
import builtins
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
import uuid

import requests

import api_ttc


SOURCE_PATH = Path(api_ttc.__file__)


def _load_api_ttc_without(*blocked_modules):
    """Load an isolated module while simulating unavailable capabilities."""
    original_import = builtins.__import__
    blocked_modules = set(blocked_modules)

    def selective_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name in blocked_modules:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return original_import(name, globals, locals, fromlist, level)

    module_name = f"_api_ttc_compat_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, SOURCE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        with mock.patch("builtins.__import__", new=selective_import):
            spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(module_name, None)


class PythonCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(
            cls.source,
            filename=str(SOURCE_PATH),
            feature_version=(3, 9),
        )

    def test_entire_source_uses_python_39_grammar(self):
        self.assertIsInstance(self.tree, ast.Module)

    def test_minimum_version_is_a_floor_without_an_upper_pin(self):
        self.assertEqual(api_ttc.MINIMUM_PYTHON, (3, 9))
        version_checks = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Compare)
            and ast.unparse(node.left) == "sys.version_info"
        ]
        self.assertEqual(len(version_checks), 1)
        check = version_checks[0]
        self.assertEqual(len(check.ops), 1)
        self.assertIsInstance(check.ops[0], ast.Lt)
        self.assertEqual(ast.unparse(check.comparators[0]), "MINIMUM_PYTHON")

    def test_unsupported_python_exits_before_third_party_imports(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.version_info = (3, 8, 0); import api_ttc",
            ],
            cwd=SOURCE_PATH.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.strip(),
            "api_ttc.py requires Python 3.9 or newer.",
        )

    def test_postponed_claim_annotations_import_and_instantiate(self):
        result = api_ttc.TTCClaimResult(True, "SUCCESS", "ok")
        self.assertTrue(result.success)
        self.assertEqual(result.balance, None)
        self.assertEqual(
            api_ttc.TTCClaimResult.__annotations__["balance"],
            "int | None",
        )

    def test_missing_cloudscraper_uses_requests_session(self):
        module = _load_api_ttc_without("cloudscraper")
        self.assertFalse(module._HAS_CLOUDSCRAPER)
        client = module.TTCClient()
        try:
            self.assertIsInstance(client._session, requests.Session)
        finally:
            client.close()

    def test_missing_platform_lock_modules_does_not_break_import(self):
        module = _load_api_ttc_without("fcntl", "msvcrt")
        self.assertIsNone(module.fcntl)
        self.assertIsNone(module.msvcrt)


if __name__ == "__main__":
    unittest.main()
