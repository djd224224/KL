#!/usr/bin/env python3
"""Which Kalshi account the incentive MM signs as -- the bot and every IMM
report (2026-10-02, Jack: "switch my IMM bot to a different kalshi account").

The fleet (crypto, weather, gas, rain bots) shares ONE key: KALSHI_API_KEY_ID,
set nowhere, so every bot falls back to FLEET_KEY_ID_DEFAULT, plus the PEM at
KALSHI_PRIVATE_KEY / KALSHI_PRIVATE_KEY_PATH / Lisa_Kalshi.txt. Changing
either moves the whole fleet. To move only the IMM, set BOTH

    IMM_KALSHI_API_KEY_ID        the other account's API key id
    IMM_KALSHI_PRIVATE_KEY_PATH  that key's private key (PEM) file

as user environment variables. Each is read from the process environment
first, then straight from HKCU\\Environment: a Task Scheduler session, or a
shell opened before the change, may not carry a new user variable, and a
stale environment must never mean a silent fall back to the fleet account.

    neither set   the fleet key, exactly as before
    both set      the IMM's own account
    one set       misconfigured: load_imm_key raises, so nothing signs at
                  all -- never a half-and-half pair, never the fleet key

incentive_mm.py and kalshi_reads.py resolve this once, at import; every IMM
report reaches Kalshi through one of the two. Read-only, no network.
"""

import os
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional

FLEET_KEY_ID_DEFAULT = "c3204983-77fc-491b-99f7-136600698178"
KEY_ID_VAR = "IMM_KALSHI_API_KEY_ID"
KEY_PATH_VAR = "IMM_KALSHI_PRIVATE_KEY_PATH"


@dataclass(frozen=True)
class Account:
    key_id: str
    key_path: Optional[str]          # None = the fleet key, found the fleet's way
    source: str                      # for the startup log line
    error: Optional[str] = None      # set = half-configured; load_imm_key raises

    @property
    def is_imm(self) -> bool:
        """True when the IMM override is set at all, even half-set: the
        caller must then load through load_imm_key and never fall back."""
        return self.key_path is not None or self.error is not None


def fleet_key_id(environ: Optional[Mapping[str, str]] = None) -> str:
    """The key id every fleet bot signs with."""
    env = os.environ if environ is None else environ
    return env.get("KALSHI_API_KEY_ID", FLEET_KEY_ID_DEFAULT)


def user_env(name: str) -> Optional[str]:
    """HKCU\\Environment\\<name> as a new process would see it (REG_EXPAND_SZ
    expanded); None off Windows, when unset, or on any registry error."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, name)
        if not isinstance(value, str):
            return None
        if kind == winreg.REG_EXPAND_SZ:
            value = winreg.ExpandEnvironmentStrings(value)
        return value
    except OSError:
        return None


def resolve(environ: Optional[Mapping[str, str]] = None,
            read_user_env: Optional[Callable[[str], Optional[str]]] = None
            ) -> Account:
    """The account the IMM signs as (see the module docstring)."""
    env = os.environ if environ is None else environ
    read_user = user_env if read_user_env is None else read_user_env
    found: Dict[str, str] = {}
    origins: List[str] = []
    for name in (KEY_ID_VAR, KEY_PATH_VAR):
        value, origin = (env.get(name) or "").strip(), "env"
        if not value:
            value, origin = (read_user(name) or "").strip(), "HKCU"
        if value:
            found[name] = value
            origins.append(origin)
    if not found:
        return Account(fleet_key_id(env), None, "fleet key")
    if len(found) == 1:
        have = next(iter(found))
        missing = KEY_PATH_VAR if have == KEY_ID_VAR else KEY_ID_VAR
        return Account(found.get(KEY_ID_VAR, ""), None, "IMM key, half-set",
                       error=f"{have} is set but {missing} is not -- set both "
                             f"or neither")
    return Account(found[KEY_ID_VAR], found[KEY_PATH_VAR],
                   "IMM key from " + "+".join(sorted(set(origins))))


def load_imm_key(account: Account):
    """The IMM account's private key. Raises on a half-set account, a missing
    file or an unreadable key; the caller must not fall back to the fleet."""
    if account.error:
        raise RuntimeError(f"IMM Kalshi account misconfigured: {account.error}")
    if not account.key_path:
        raise RuntimeError("IMM Kalshi account not configured: "
                           f"{KEY_ID_VAR} / {KEY_PATH_VAR} are unset")
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    with open(account.key_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None,
                                                  backend=default_backend())
