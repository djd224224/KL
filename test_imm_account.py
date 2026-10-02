#!/usr/bin/env python3
"""Unit tests for imm_account.py — run: python -m unittest test_imm_account"""

import os
import tempfile
import unittest
from unittest import mock

import imm_account as ia


def _no_user_env(name):
    return None


def _pem_path() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    path = os.path.join(tempfile.mkdtemp(prefix="imm_account_"), "imm.pem")
    with open(path, "wb") as f:
        f.write(pem)
    return path


class TestResolve(unittest.TestCase):
    def test_nothing_set_is_the_fleet_key_exactly_as_before(self):
        a = ia.resolve({}, _no_user_env)
        self.assertEqual(a.key_id, ia.FLEET_KEY_ID_DEFAULT)
        self.assertIsNone(a.key_path)
        self.assertIsNone(a.error)
        self.assertFalse(a.is_imm)
        self.assertEqual(a.source, "fleet key")

    def test_the_fleet_key_id_still_honours_KALSHI_API_KEY_ID(self):
        a = ia.resolve({"KALSHI_API_KEY_ID": "fleet-2"}, _no_user_env)
        self.assertEqual(a.key_id, "fleet-2")
        self.assertFalse(a.is_imm)
        self.assertEqual(ia.fleet_key_id({"KALSHI_API_KEY_ID": "fleet-2"}), "fleet-2")
        self.assertEqual(ia.fleet_key_id({}), ia.FLEET_KEY_ID_DEFAULT)

    def test_both_set_in_the_environment(self):
        env = {ia.KEY_ID_VAR: "imm-id", ia.KEY_PATH_VAR: r"C:\keys\imm.txt",
               "KALSHI_API_KEY_ID": "fleet-2"}
        a = ia.resolve(env, _no_user_env)
        self.assertEqual((a.key_id, a.key_path), ("imm-id", r"C:\keys\imm.txt"))
        self.assertTrue(a.is_imm)
        self.assertIsNone(a.error)
        self.assertEqual(a.source, "IMM key from env")

    def test_a_stale_environment_still_finds_the_user_variables(self):
        # a Task Scheduler session or an old shell: nothing in the process env
        user = {ia.KEY_ID_VAR: "imm-id", ia.KEY_PATH_VAR: "imm.txt"}
        a = ia.resolve({}, user.get)
        self.assertEqual((a.key_id, a.key_path), ("imm-id", "imm.txt"))
        self.assertEqual(a.source, "IMM key from HKCU")

    def test_the_process_environment_wins_per_variable(self):
        user = {ia.KEY_ID_VAR: "registry-id", ia.KEY_PATH_VAR: "registry.txt"}
        a = ia.resolve({ia.KEY_ID_VAR: "env-id"}, user.get)
        self.assertEqual((a.key_id, a.key_path), ("env-id", "registry.txt"))
        self.assertEqual(a.source, "IMM key from HKCU+env")

    def test_blank_values_count_as_unset(self):
        a = ia.resolve({ia.KEY_ID_VAR: "  ", ia.KEY_PATH_VAR: ""}, _no_user_env)
        self.assertFalse(a.is_imm)
        self.assertEqual(a.key_id, ia.FLEET_KEY_ID_DEFAULT)

    def test_half_set_is_an_error_and_never_the_fleet_key(self):
        for env, missing in (({ia.KEY_ID_VAR: "imm-id"}, ia.KEY_PATH_VAR),
                             ({ia.KEY_PATH_VAR: "imm.txt"}, ia.KEY_ID_VAR)):
            a = ia.resolve(env, _no_user_env)
            self.assertTrue(a.is_imm)
            self.assertIn(missing, a.error)
            self.assertNotEqual(a.key_id, ia.FLEET_KEY_ID_DEFAULT)
            with self.assertRaisesRegex(RuntimeError, "misconfigured"):
                ia.load_imm_key(a)


class TestLoadImmKey(unittest.TestCase):
    def test_loads_the_configured_pem(self):
        a = ia.resolve({ia.KEY_ID_VAR: "imm-id", ia.KEY_PATH_VAR: _pem_path()},
                       _no_user_env)
        self.assertEqual(ia.load_imm_key(a).key_size, 2048)

    def test_a_missing_file_raises(self):
        missing = os.path.join(tempfile.mkdtemp(prefix="imm_account_"), "nope.pem")
        a = ia.resolve({ia.KEY_ID_VAR: "imm-id", ia.KEY_PATH_VAR: missing},
                       _no_user_env)
        with self.assertRaises(FileNotFoundError):
            ia.load_imm_key(a)

    def test_the_fleet_account_has_no_imm_key(self):
        with self.assertRaises(RuntimeError):
            ia.load_imm_key(ia.resolve({}, _no_user_env))


class TestUserEnv(unittest.TestCase):
    def test_off_windows_there_is_no_registry(self):
        with mock.patch.object(ia.sys, "platform", "linux"):
            self.assertIsNone(ia.user_env("PATH"))

    @unittest.skipUnless(os.name == "nt", "Windows registry")
    def test_an_unset_name_reads_none(self):
        self.assertIsNone(ia.user_env("IMM_ACCOUNT_TEST_NEVER_SET_7F3C"))

    @unittest.skipUnless(os.name == "nt", "Windows registry")
    def test_an_expandable_value_is_expanded(self):
        import winreg
        with mock.patch("winreg.OpenKey", return_value=mock.MagicMock()), \
                mock.patch("winreg.QueryValueEx",
                           return_value=(r"%SystemRoot%\imm.txt",
                                         winreg.REG_EXPAND_SZ)):
            got = ia.user_env(ia.KEY_PATH_VAR)
        self.assertEqual(got, os.path.expandvars(r"%SystemRoot%\imm.txt"))
        self.assertNotIn("%", got)

    @unittest.skipUnless(os.name == "nt", "Windows registry")
    def test_a_non_string_value_reads_none(self):
        import winreg
        with mock.patch("winreg.OpenKey", return_value=mock.MagicMock()), \
                mock.patch("winreg.QueryValueEx",
                           return_value=(7, winreg.REG_DWORD)):
            self.assertIsNone(ia.user_env(ia.KEY_ID_VAR))


if __name__ == "__main__":
    unittest.main()
