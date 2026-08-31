"""Config store for hsmcli — cookie header, base URL, output format."""

import json
import os
import sys
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlsplit


def _chmod(path: Union[Path, str], mode: int) -> None:
    """Best-effort chmod — some filesystems (SMB, some FUSE mounts) can't."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


# The config file holds the whole Supabase session — the access token *and*
# the long-lived refresh token. Anything another local user can read is an
# account takeover, so the dir and file stay owner-only.
DIR_MODE = 0o700
FILE_MODE = 0o600


class Config:
    def __init__(self, config_dir: Optional[str] = None):
        is_default_dir = config_dir is None
        self.config_dir = Path(config_dir) if config_dir else Path.home() / ".hsmcli"
        self.config_file = self.config_dir / "config.json"
        existed = self.config_dir.is_dir()
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ValueError(f"can't create config dir {self.config_dir}: {e}") from e
        # Tighten the directory only when it's ours: one we just created, or
        # the dedicated ~/.hsmcli. Chmod-ing a pre-existing --config-dir
        # would silently make a shared directory (or /tmp) owner-only.
        if is_default_dir or not existed:
            _chmod(self.config_dir, DIR_MODE)
        # Repair permissions left by an older version that wrote 0644.
        if self.config_file.exists():
            _chmod(self.config_file, FILE_MODE)
        self._config = self._load()

    def _load(self) -> dict:
        if self.config_file.exists():
            try:
                with open(self.config_file) as f:
                    return json.load(f)
            except json.JSONDecodeError:
                # Say so rather than silently treating the user as signed
                # out — "why is hsmcli asking me to log in again?" is a
                # config problem, not a session one.
                print(f"warning: {self.config_file} is corrupt and will be "
                      f"rewritten on the next config change", file=sys.stderr)
                return {}
            except IOError:
                return {}
        return {}

    def _save(self):
        # Atomic: write a 0600 sibling, then rename over the real file, so
        # an interrupted write can't truncate the stored session. os.open
        # with the mode creates the temp file 0600 outright — writing then
        # chmod-ing would leave the token world-readable in between.
        tmp = self.config_file.with_name(self.config_file.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._config, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.config_file)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _chmod(self.config_file, FILE_MODE)

    # ── cookie ────────────────────────────────────────────────────────────
    def get_cookie(self) -> Optional[str]:
        env = os.getenv("HSMCLI_COOKIE")
        if env:
            return env.strip()
        return self._config.get("cookie")

    def set_cookie(self, cookie: str):
        if not cookie or not cookie.strip():
            raise ValueError("Cookie cannot be empty")
        self._config["cookie"] = cookie.strip()
        self._save()

    def clear_cookie(self):
        if self._config.pop("cookie", None) is not None:
            self._save()

    # ── base URL ──────────────────────────────────────────────────────────
    def get_base_url(self) -> str:
        return self._config.get("base_url", "https://www.hacksmarter.org")

    def set_base_url(self, url: str, allow_insecure: bool = False):
        # The session cookie (access + refresh token) rides on every request
        # to this URL. Over http:// that's the whole account in cleartext,
        # so anything but https needs an explicit opt-in.
        if not url or not url.strip():
            raise ValueError("URL cannot be empty")
        cleaned = url.strip().rstrip("/")
        parts = urlsplit(cleaned)
        if not parts.hostname:
            raise ValueError(f"'{cleaned}' has no hostname — expected "
                             f"something like https://www.hacksmarter.org")
        if parts.username or parts.password:
            raise ValueError("the base URL must not embed credentials")
        if parts.scheme != "https" and not allow_insecure:
            raise ValueError(
                f"'{cleaned}' would send your session cookie unencrypted — "
                f"use https://, or pass --allow-insecure-http if you really "
                f"mean it (local development only)")
        if parts.scheme not in ("https", "http"):
            raise ValueError(f"unsupported URL scheme '{parts.scheme}'")
        self._config["base_url"] = cleaned
        self._save()

    # ── output format ─────────────────────────────────────────────────────
    def get_output_format(self) -> str:
        return self._config.get("output_format", "table")

    def set_output_format(self, fmt: str):
        if fmt not in ("table", "json", "yaml"):
            raise ValueError("output format must be one of: table, json, yaml")
        self._config["output_format"] = fmt
        self._save()

    def get_config_path(self) -> str:
        return str(self.config_file)

    def get_all(self) -> dict:
        return self._config.copy()

    def reset(self):
        self._config = {}
        self._save()
