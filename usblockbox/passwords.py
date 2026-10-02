"""Password resolution. Per-agency profiles are reserved (profile id only) but NOT implemented."""
from __future__ import annotations

import re
import secrets
from typing import Callable, Optional

from .config import Settings

# Unambiguous characters only (no 0/O, 1/l/I) so a passphrase can be read over the phone.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
_SYMBOLS = "-_.+"


def generate_passphrase(length: int = 24) -> str:
    length = max(length, 12)
    while True:
        pw = "".join(secrets.choice(_ALPHABET + _SYMBOLS) for _ in range(length))
        if (re.search(r"[A-Z]", pw) and re.search(r"[a-z]", pw)
                and re.search(r"\d", pw) and re.search(r"[-_.+]", pw)):
            return pw


def has_complexity(pw: str) -> bool:
    classes = sum(bool(re.search(p, pw)) for p in (r"[A-Z]", r"[a-z]", r"\d", r"[^A-Za-z0-9]"))
    return classes >= 3


def validate_password(pw: str, settings: Settings, policy_min_len: int = 0,
                      policy_complexity: bool = False) -> Optional[str]:
    """Return an error string, or None if acceptable."""
    floor = max(settings.min_password_length, policy_min_len)
    if len(pw) < floor:
        return f"Password is {len(pw)} characters; the minimum in force is {floor}."
    if policy_complexity and not has_complexity(pw):
        return "Policy requires a complex password (3 of: upper, lower, digit, symbol)."
    return None


def resolve_password(settings: Settings,
                     prompt: Optional[Callable[[], Optional[str]]] = None) -> str:
    """Password to apply to the drive about to be processed."""
    mode = settings.password_mode
    if mode == "fixed":
        pw = settings.get_fixed_password()
        if not pw:
            raise ValueError("Fixed password mode is selected but no password is set (or it cannot be decrypted by this "
                             "Windows account). Re-enter it in Settings.")
        return pw
    if mode == "generated":
        return generate_passphrase(settings.generated_length)
    if mode == "prompt":
        pw = prompt() if prompt else None
        if not pw:
            raise ValueError("No password entered.")
        return pw
    raise ValueError(f"Unknown password mode: {mode}")
