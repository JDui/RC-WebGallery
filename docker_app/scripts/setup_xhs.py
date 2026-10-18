from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(description='Create the private Docker secret for the optional XHS worker')
    parser.add_argument('--secrets-dir', type=Path, default=Path(__file__).resolve().parents[2] / '.secrets')
    args = parser.parse_args()
    args.secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.secrets_dir.chmod(0o700)
    path = args.secrets_dir / 'xhs_worker_token'
    if path.exists():
        if len(path.read_text().strip()) < 32:
            raise SystemExit('现有 Worker 密钥格式无效，请检查私有目录。')
        path.chmod(0o444)
        print('小红书 Worker 密钥已存在，保留原值。')
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        output.write(secrets.token_urlsafe(48) + '\n')
    # The enclosing directory is 0700; Docker mounts only this read-only inode for the non-root worker.
    path.chmod(0o444)
    print('已创建独立 Worker 密钥；不会输出密钥内容。')


if __name__ == '__main__':
    main()
