"""Tiny JSON config store used for locally-persisted app state
(admin password hash, tokens, server registry, attached-device map).

This file lives on a docker volume mounted at /data and is never part of
the git repo (see .gitignore: data/).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable


class ConfigStore:
    def __init__(self, path: str | Path, defaults: Callable[[], dict[str, Any]]):
        self.path = Path(path)
        self._defaults = defaults
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(self._defaults())

    def _write(self, data: dict[str, Any]) -> None:
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def read(self) -> dict[str, Any]:
        with self._lock:
            try:
                with open(self.path) as f:
                    return json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = self._defaults()
                self._write(data)
                return data

    def update(self, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock:
            try:
                with open(self.path) as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = self._defaults()
            mutate(data)
            self._write(data)
            return data
