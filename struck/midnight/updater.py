from __future__ import annotations

import subprocess
from pathlib import Path


def check_updates(repo_path: Path) -> str:
    if not repo_path.exists():
        return f"repo not found: {repo_path}"
    result = subprocess.run(
        ["git", "-C", str(repo_path), "fetch", "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or result.stderr.strip() or "no updates reported"
