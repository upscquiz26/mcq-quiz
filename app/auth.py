"""
Auth helpers: password hashing, signup validation, session secret.

Users live in the `users` table. The first admin is created on startup from
these environment variables (or a .env file) if no admin exists yet:

    APP_USERNAME   defaults to "admin"
    APP_PASSWORD   the admin's initial password; after the first run the
                   password lives (hashed) in the database and this is ignored
    SECRET_KEY     signs the session cookie; if unset a random key is generated,
                   which just means everyone is logged out when the app restarts
"""
import base64
import hashlib
import os
import re
import secrets

DEFAULT_ADMIN_USERNAME = "admin"

MIN_PASSWORD_LENGTH = 8
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,30}$")

# scrypt cost parameters (~16 MiB, tens of milliseconds per hash).
_N, _R, _P = 2**14, 8, 1


def get_secret_key() -> str:
    return os.environ.get("SECRET_KEY") or secrets.token_hex(32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return "$".join(
        ["scrypt", str(_N), str(_R), str(_P),
         base64.b64encode(salt).decode(), base64.b64encode(digest).decode()]
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(actual, expected)


# Verified against when the username doesn't exist, so a missing user costs the
# same time as a wrong password.
DUMMY_HASH = hash_password(secrets.token_hex(8))


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def validate_signup(username: str, password: str, confirm: str) -> str | None:
    """Returns an error message, or None if the signup fields are acceptable."""
    if not USERNAME_RE.match(username):
        return "Username must be 3–30 characters: letters, numbers, dot, dash or underscore."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password != confirm:
        return "Passwords don't match."
    return None


def validate_new_password(new: str, confirm: str, current: str) -> str | None:
    """Rules for choosing a new password. Returns an error message, or None if it's acceptable."""
    if len(new) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if new != confirm:
        return "The new passwords don't match."
    if new == current:
        return "Choose a password different from your current one."
    return None


# No 0/O, 1/l/I: a temporary password is read out or typed by hand.
_TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def generate_temp_password(length: int = 12) -> str:
    return "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(length))


def validate_display_name(name: str) -> str | None:
    if not 2 <= len(name) <= 30:
        return "Display name must be 2–30 characters."
    if any(ord(c) < 32 for c in name):
        return "Display name contains invalid characters."
    return None


def safe_next(target: str | None) -> str:
    """Only allow same-site relative paths, so /login?next= can't bounce off-site."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"
