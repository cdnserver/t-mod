"""Provision one persistent 2FA key without exposing it in output or rotating it."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from persistence.core import connect_readonly


def configure(root: Path) -> dict:
    env = root / ".env"
    values = {}
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and name.strip() in {"TMOD_ACCOUNT_SECURITY_KEY", "TMOD_ACCOUNT_SECURITY_KEY_FILE"}:
            value = value.strip()
            if value[:1] in {"'", '"'}:
                quote = value[0]
                closing = value.find(quote, 1)
                if closing < 0:
                    raise RuntimeError("invalid_security_configuration")
                value = value[1:closing]
            else:
                value = value.split(" #", 1)[0].strip()
            values[name.strip()] = value
    inline = str(values.get("TMOD_ACCOUNT_SECURITY_KEY") or "").strip()
    selected = str(values.get("TMOD_ACCOUNT_SECURITY_KEY_FILE") or "").strip()
    key_file = Path(selected) if selected else root / "secrets" / "account-security.key"
    if inline:
        Fernet(inline.encode("ascii"))
        return {"ok": True, "status": "existing_inline_key_preserved"}
    if key_file.exists():
        Fernet(key_file.read_bytes().strip())
    else:
        # Never create a new key for existing encrypted enrollments.
        with connect_readonly() as con:
            count = con.execute("SELECT COUNT(*) AS n FROM account_security WHERE mfa_method <> '' OR pending_secret <> ''").fetchone()["n"]
        if count:
            raise RuntimeError("existing_enrollments_require_original_key")
        key_file.parent.mkdir(parents=True, exist_ok=True)
        with key_file.open("xb") as handle:
            os.chmod(key_file, 0o600)
            handle.write(Fernet.generate_key() + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    if not selected:
        # Append rather than rewrite existing deployment settings.
        with env.open("a", encoding="utf-8") as handle:
            handle.write(f"\nTMOD_ACCOUNT_SECURITY_KEY_FILE={key_file.as_posix()}\n")
            handle.flush()
            os.fsync(handle.fileno())
    return {"ok": True, "status": "persistent_key_ready", "restart_required": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--persistent-dir", type=Path, default=Path("/app/persistent"))
    args = parser.parse_args()
    try:
        print(json.dumps(configure(args.persistent_dir)))
    except Exception:
        # Neither exceptions from configuration nor secret paths reach logs.
        print(json.dumps({"ok": False, "error": "security_key_setup_failed_original_key_may_be_required"}))
        sys.exit(1)
