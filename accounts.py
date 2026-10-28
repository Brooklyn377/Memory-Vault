"""Login credentials. A salted scrypt hash lives in credentials.json; until it exists, web.env is used."""
import hashlib
import json
import os
import secrets
from pathlib import Path

FILE = Path("/var/lib/memory-agent/credentials.json")


def _hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def load():
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return None


def current_user(env_user):
    c = load()
    return c["user"] if c else env_user


def verify(username, password, env_user, env_pass):
    """Returns (user_ok, password_ok). Both checks always run, so timing does not reveal which one failed."""
    c = load()
    if c:
        pw_ok = secrets.compare_digest(_hash(password, bytes.fromhex(c["salt"])), bytes.fromhex(c["hash"]))
        user_ok = secrets.compare_digest(username.encode(), c["user"].encode())
    else:
        pw_ok = secrets.compare_digest(password.encode(), env_pass.encode())
        user_ok = secrets.compare_digest(username.encode(), env_user.encode())
    return user_ok, pw_ok


def save(user, password):
    salt = secrets.token_bytes(16)
    tmp = FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"user": user, "salt": salt.hex(), "hash": _hash(password, salt).hex()}))
    os.replace(tmp, FILE)
