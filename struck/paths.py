"""Where Struck's files live, on Linux, macOS and Windows (stdlib only; the
CLIs must not import Qt).

* Repo files (config/struck.json, logs/, receipts/, plans/): resolved from
  THIS package's location (``Path(__file__)``), never from the current
  working directory.
* Per-user settings (llm.json, network.json; secrets are NOT files: they
  live in the encrypted store secrets.cfg, struck.secrets): ``user_config_dir()``, the OS's per-user application config
  directory, computed from ``Path.home()`` (which itself reads HOME on POSIX
  and USERPROFILE on Windows; Struck reads no other variable for it):
    - Linux:   ~/.config/struck
    - macOS:   ~/Library/Application Support/Struck
    - Windows: %USERPROFILE%\\AppData\\Roaming\\Struck  (owner-only by OS default;
      Struck still sets and verifies an owner-only DACL, see private_file)
"""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent


def user_config_dir(platform: str = sys.platform, home: Path | None = None) -> Path:
    base = Path.home() if home is None else home
    if platform == "win32":
        return base / "AppData" / "Roaming" / "Struck"
    if platform == "darwin":
        return base / "Library" / "Application Support" / "Struck"
    return base / ".config" / "struck"
