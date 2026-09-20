"""Run the ELMFIRE binary in a run directory via MPI."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .models import ElmfireFailed, ElmfireTimeout


@dataclass
class ElmfireResult:
    returncode: int
    seconds: float
    stdout_path: Path
    command: list[str]

    def stdout_tail(self, n_lines: int = 40) -> str:
        try:
            lines = self.stdout_path.read_text(errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-n_lines:])


def elmfire_available() -> bool:
    return shutil.which("elmfire") is not None


def build_command(nproc: int, members: int) -> list[str]:
    """One MPI rank per ensemble case, capped at ELMFIRE_NPROC. ``--mca btl`` is the user
    guide's boilerplate (shared-memory transport within the host); core binding is left
    out because it is incompatible with oversubscription in small containers."""
    np_ = max(1, min(nproc, members))
    cmd = ["elmfire", "elmfire.data"]
    if shutil.which("mpirun") and np_ > 1:
        cmd = ["mpirun", "--mca", "btl", "tcp,self,vader", "--oversubscribe", "-np", str(np_), *cmd]
    return cmd


async def run_elmfire(run_dir: Path, nproc: int, members: int, timeout_s: float) -> ElmfireResult:
    """Execute ELMFIRE in ``run_dir`` (needs elmfire.data, inputs/, weather/).

    stdout+stderr are streamed to ``run_dir/elmfire.out``. Raises ElmfireTimeout /
    ElmfireFailed; the caller decides whether to keep the directory.
    """
    if not elmfire_available():
        raise ElmfireFailed("elmfire binary not found on PATH (run inside the container)")
    (run_dir / "outputs").mkdir(exist_ok=True)
    (run_dir / "scratch").mkdir(exist_ok=True)
    cmd = build_command(nproc, members)
    out_path = run_dir / "elmfire.out"
    env = {**os.environ, "OMP_NUM_THREADS": "1"}
    t0 = time.perf_counter()
    with out_path.open("wb") as out:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=run_dir, stdout=out, stderr=asyncio.subprocess.STDOUT, env=env
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise ElmfireTimeout(f"ELMFIRE exceeded {timeout_s:.0f} s")
    res = ElmfireResult(proc.returncode or 0, time.perf_counter() - t0, out_path, cmd)
    if res.returncode != 0:
        raise ElmfireFailed(f"ELMFIRE exited with {res.returncode}:\n{res.stdout_tail()}")
    return res
