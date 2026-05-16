from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Civ6Paths:
    autosaves: Path
    civ6_user_data: Path

    @property
    def logs_dir(self) -> Path:
        return self.civ6_user_data / "Logs"

    @property
    def cache_dir(self) -> Path:
        return self.civ6_user_data / "Cache"


def load_paths(config_path: str | Path = "config.json") -> Civ6Paths:
    path = Path(config_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    paths = Civ6Paths(
        autosaves=Path(data["autosaves"]),
        civ6_user_data=Path(data["civ6_user_data"]),
    )
    if not paths.logs_dir.exists():
        raise FileNotFoundError(f"Logs directory not found: {paths.logs_dir}")
    if not paths.autosaves.exists():
        raise FileNotFoundError(f"Autosave directory not found: {paths.autosaves}")
    return paths
