import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
DEFAULT_GLOBAL_CONFIG = {
    "catalog_path": "catalog.json",
    "text_encoding": "cp037",
}


def load_global_config() -> dict:
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_GLOBAL_CONFIG)

    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            print("Config load warning: config.json root must be an object; using defaults")
            return dict(DEFAULT_GLOBAL_CONFIG)
    except Exception as e:
        print(f"Config load error: {e}; using defaults")
        return dict(DEFAULT_GLOBAL_CONFIG)

    merged = dict(DEFAULT_GLOBAL_CONFIG)
    merged.update(loaded)
    return merged


GLOBAL_CONFIG = load_global_config()


def resolve_config_path(path_value: str, default_name: str) -> Path:
    cleaned = (path_value or "").strip()
    if not cleaned:
        cleaned = default_name
    candidate = Path(cleaned)
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    return candidate


TEXT_ENCODING = str(GLOBAL_CONFIG.get("text_encoding", "cp037"))
CATALOG_FILE = resolve_config_path(str(GLOBAL_CONFIG.get("catalog_path", "catalog.json")), "catalog.json")
