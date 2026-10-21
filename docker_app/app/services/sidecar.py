from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


class SidecarRunner:
    def __init__(self, timeout_seconds: float = 45.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.media_worker = self._resolve_binary("APP_MEDIA_WORKER_BIN", "media-worker")
        self.indexer = self._resolve_binary("APP_INDEXER_BIN", "indexer")

    def _resolve_binary(self, env_name: str, binary_name: str) -> str | None:
        configured = os.environ.get(env_name)
        candidates = [
            configured,
            f"/opt/zzs/bin/{binary_name}",
            binary_name,
        ]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
            if path.name == candidate:
                return candidate
        return None

    def _run_json(self, binary: str | None, args: list[str]) -> dict[str, Any] | None:
        if not binary:
            return None
        try:
            result = subprocess.run(
                [binary, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return None
        if not payload.get("ok"):
            return None
        return payload

    def derive_image(
        self,
        source: Path,
        thumb: Path,
        small: Path,
        options: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        options = options or {}
        result = self._run_json(
            self.media_worker,
            [
                "derive-image",
                "--source",
                str(source),
                "--thumb",
                str(small),
                "--small",
                str(small),
                "--thumb-edge",
                str(options.get("small_edge", 368)),
                "--thumb-quality",
                str(options.get("small_quality", 50)),
                "--small-edge",
                str(options.get("small_edge", 368)),
                "--small-quality",
                str(options.get("small_quality", 50)),
            ],
        )
        # Older workers produce two tiers or ignore WebP quality. Use the local fallback.
        return result if result and result.get("thumbnail_policy") == "single-v1" else None

    def probe_image(self, source: Path) -> dict[str, Any] | None:
        return self._run_json(self.media_worker, ["probe-image", "--source", str(source)])

    def scan_folder(self, images_dir: Path, livephoto_dir: Path) -> dict[str, Any] | None:
        return self._run_json(
            self.indexer,
            [
                "scan",
                "--images-dir",
                str(images_dir),
                "--livephoto-dir",
                str(livephoto_dir),
            ],
        )
