from __future__ import annotations

import os
import subprocess
import time


def main():
    display = subprocess.Popen(['Xvfb', ':99', '-screen', '0', '1280x900x24', '-nolisten', 'tcp'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if os.path.exists('/tmp/.X11-unix/X99'):
                break
            time.sleep(0.1)
        import uvicorn
        uvicorn.run('xhs_worker.main:app', host=os.environ.get('XHS_WORKER_HOST', '127.0.0.1'),
                    port=int(os.environ.get('XHS_WORKER_PORT', '8091')), access_log=False, log_level='warning')
    finally:
        display.terminate()
        display.wait(timeout=10)


if __name__ == '__main__':
    main()
