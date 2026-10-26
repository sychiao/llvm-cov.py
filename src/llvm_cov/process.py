"""Subprocess wrapper with stderr logging, timeout and failed-export rollback."""
import os
from pathlib import Path
import signal
import subprocess
import threading

from .archive import write_pickle
from .parser import CoverageReader


class ExportError(ValueError):
    pass


def export_pickle(command, destination, *, stderr_path, timeout=120, buffer_size=65536,
                  test_name=None, overwrite=False):
    if timeout <= 0:
        raise ValueError('timeout must be positive')
    command = tuple(map(str, command))
    log = Path(stderr_path).resolve()
    target = Path(destination).resolve()
    if log == target:
        raise ValueError('stderr log must differ from pickle output')
    if target.exists() and not overwrite:
        raise FileExistsError(target)
    log.parent.mkdir(parents=True, exist_ok=True)
    expired = threading.Event()
    env = dict(os.environ, LLVM_PROFILE_FILE=os.devnull)
    env.pop('__LLVM_PROFILE_RT_INIT_ONCE', None)
    with log.open('wb') as stderr:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr,
                                env=env, start_new_session=True)

        def kill():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        def expire():
            expired.set()
            kill()

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            reader = CoverageReader(proc.stdout, buffer_size=buffer_size)

            def finalize():
                code = proc.wait()
                timer.cancel()
                if expired.is_set():
                    raise ExportError(f'export timed out after {timeout} seconds')
                if code:
                    raise ExportError(f'export failed ({code}); see {log}')

            try:
                return write_pickle(reader, target, source='subprocess stdout', test_name=test_name,
                                    command=command, overwrite=overwrite, finalize=finalize)
            except Exception as exc:
                if expired.is_set():
                    raise ExportError(f'export timed out after {timeout} seconds') from exc
                raise
        finally:
            timer.cancel()
            if proc.poll() is None:
                kill()
            proc.wait()
            proc.stdout.close()
