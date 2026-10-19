from __future__ import annotations

import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time

from app.services.xhs.worker_client import XhsWorkerClient, WorkerError


def prepare_private(config_dir: Path, uid: int, gid: int) -> Path:
    root = config_dir / 'xhs-private'
    if root.is_symlink():
        raise RuntimeError('PRIVATE_DIRECTORY_INVALID')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    token = root / 'rpc-token'
    if token.is_symlink():
        raise RuntimeError('PRIVATE_TOKEN_INVALID')
    try:
        descriptor = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if len(token.read_text().strip()) < 32:
            raise RuntimeError('PRIVATE_TOKEN_INVALID') from None
    else:
        with os.fdopen(descriptor, 'w') as output:
            output.write(secrets.token_urlsafe(48))
    token.chmod(0o600)
    if os.getuid() == 0:
        os.chown(root, uid, gid)
        os.chown(token, uid, gid)
    return root


class EmbeddedWorker:
    """Supervise the private browser process without publishing another container port."""

    def __init__(self, config):
        self.config = config
        self.enabled = os.environ.get('XHS_EMBEDDED', '') == '1' and not os.environ.get('XHS_WORKER_URL')
        self.process = None
        self.client = None
        self.root = None
        self._stopped = threading.Event()
        self._monitor = None
        self._lock = threading.Lock()

    def start(self):
        uid = 1000 if os.getuid() == 0 else os.getuid()
        gid = 1000 if os.getuid() == 0 else os.getgid()
        self.root = prepare_private(self.config.config_dir, uid, gid)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        self.client = XhsWorkerClient(f'http://127.0.0.1:{port}', self.root / 'rpc-token')
        self._environment = {key: value for key, value in os.environ.items() if 'proxy' not in key.lower()}
        self._environment.update({
            'PYTHONPATH': os.pathsep.join((str(self.config.app_root.parent), self._environment.get('PYTHONPATH', ''))),
            'XHS_EMBEDDED': '1', 'XHS_PRIVATE_ROOT': '/proc/self/cwd',
            'XHS_WORKER_TOKEN_FILE': '/proc/self/cwd/rpc-token',
            'XHS_WORKER_HOST': '127.0.0.1', 'XHS_WORKER_PORT': str(port),
            'HOME': '/proc/self/cwd', 'DISPLAY': ':99',
        })
        self._identity = {'user': uid, 'group': gid, 'extra_groups': []} if os.getuid() == 0 else {}
        self._stopped.clear()
        self._spawn()
        self._monitor = threading.Thread(target=self._supervise, name='xhs-browser-supervisor', daemon=True)
        self._monitor.start()
        # No browser or external website is opened during application startup.
        for _ in range(50):
            try:
                self.client.status()
                break
            except WorkerError:
                if self._stopped.wait(0.1):
                    break
        return self.client

    def _spawn(self):
        with self._lock:
            if self._stopped.is_set():
                return
            self.process = subprocess.Popen(
                [sys.executable, '-m', 'xhs_worker.entrypoint'], cwd=self.root,
                env=self._environment, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **self._identity,
            )

    @staticmethod
    def _terminate(process):
        if not process:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)

    def _supervise(self):
        while not self._stopped.wait(1):
            with self._lock:
                process = self.process
            if process is not None and process.poll() is None:
                continue
            self._terminate(process)
            if not self._stopped.wait(2):
                try:
                    self._spawn()
                except OSError:
                    continue

    def stop(self):
        self._stopped.set()
        if self._monitor:
            self._monitor.join(timeout=4)
        with self._lock:
            process, self.process = self.process, None
        self._terminate(process)
