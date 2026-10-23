from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def docker_available() -> bool:
    return shutil.which("docker") is not None


def check_docker() -> str:
    if not docker_available():
        return "docker not found"
    result = subprocess.run(
        ["docker", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or result.stderr.strip()


def clone_repo(target: Path) -> list[str]:
    steps: list[str] = []
    if target.exists():
        steps.append(f"target already exists: {target}")
        return steps
    steps.append(f"would run: git clone https://github.com/midnight-ntwrk/midnight-node-docker {target}")
    steps.append("not executed automatically")
    return steps
