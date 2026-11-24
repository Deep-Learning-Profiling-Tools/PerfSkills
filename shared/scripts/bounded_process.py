"""Stream subprocess logs and clean up the process group on timeout/cancellation."""

import os
import signal
import subprocess
import threading
import time


def run(argv, cwd, env, stdout, stderr, timeout=None):
    stdout.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    main_thread = threading.current_thread() is threading.main_thread()
    previous = signal.getsignal(signal.SIGTERM) if main_thread else None
    def interrupted(signum, frame):
        raise SystemExit(128 + signum)
    try:
        if main_thread:
            signal.signal(signal.SIGTERM, interrupted)
        with stdout.open("wb") as out, stderr.open("wb") as err:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=err, start_new_session=True)
            try:
                proc.wait(timeout=timeout)
            except BaseException as exc:
                # Kill the process group on deadline or cancellation; retain live logs.
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(proc.pid, sig)
                    except ProcessLookupError:
                        pass
                    if sig == signal.SIGTERM:
                        try:
                            proc.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            pass
                proc.wait()
                if isinstance(exc, subprocess.TimeoutExpired):
                    raise RuntimeError(f"command timed out after {timeout:g}s; stdout={stdout}; stderr={stderr}") from None
                raise
    finally:
        if main_thread:
            signal.signal(signal.SIGTERM, previous)
    result = subprocess.CompletedProcess(argv, proc.returncode, stdout.read_bytes(), stderr.read_bytes())
    result.elapsed_s = time.perf_counter() - started
    return result

