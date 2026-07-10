import fnmatch
import json
from pathlib import Path

from app_config import CATALOG_FILE, TEXT_ENCODING, resolve_config_path


def normalize_dsn(dsn: str) -> str:
    return ".".join(p for p in dsn.strip().upper().split(".") if p)


def load_catalog() -> list:
    if not CATALOG_FILE.exists():
        return []
    try:
        data = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        return data.get("datasets", [])
    except Exception as e:
        print(f"Catalog load error: {e}")
        return []


def search_catalog(entries: list, pattern: str) -> list:
    patt = normalize_dsn(pattern)
    if not patt:
        return []
    out = [
        ds for ds in entries if fnmatch.fnmatchcase(normalize_dsn(ds.get("dsn", "")), patt)
    ]
    out.sort(key=lambda d: normalize_dsn(d.get("dsn", "")))
    return out


def catalog_entry_path(entry: dict) -> Path:
    return resolve_config_path(str(entry.get("path", "")).strip(), "")


def is_pds_like(entry: dict) -> bool:
    return str(entry.get("org", "")).strip().upper() in {"PO", "POE"}


def load_dataset_lines(entry: dict) -> tuple[list, str]:
    file_path = catalog_entry_path(entry)
    if not file_path.exists():
        dsn = normalize_dsn(entry.get("dsn", ""))
        return [], f"DATA SET NOT FOUND: {dsn}"
    if not file_path.is_file():
        dsn = normalize_dsn(entry.get("dsn", ""))
        return [], f"CATALOG PATH IS NOT A FILE: {dsn}"

    try:
        raw = file_path.read_bytes()
    except Exception as e:
        return [], f"UNABLE TO READ DATA SET: {e}"

    if str(entry.get("content_mode", "text")).strip().lower() == "binary":
        return [], "BINARY DATA SET NOT SUPPORTED YET"

    text_ccsid = str(entry.get("text_ccsid", TEXT_ENCODING)).strip() or TEXT_ENCODING
    recfm = str(entry.get("recfm", "")).strip().upper()
    is_fixed = recfm in {"F", "FB"}
    try:
        lrecl = int(entry.get("lrecl", 0))
    except Exception:
        lrecl = 0

    if is_fixed and lrecl > 0:
        lines = []
        try:
            for offset in range(0, len(raw), lrecl):
                record = raw[offset: offset + lrecl]
                if not record:
                    continue
                if len(record) < lrecl:
                    record = record.ljust(lrecl, b" ")
                lines.append(record.decode(text_ccsid))
        except Exception as e:
            return [], f"CCSID DECODE ERROR ({text_ccsid}): {e}"
        return lines, None

    try:
        text = raw.decode(text_ccsid)
    except Exception as e:
        return [], f"CCSID DECODE ERROR ({text_ccsid}): {e}"

    return text.splitlines(), None


def save_dataset_lines(entry: dict, lines: list) -> str:
    if str(entry.get("content_mode", "text")).strip().lower() == "binary":
        return "BINARY DATA SET SAVE NOT SUPPORTED"

    file_path = catalog_entry_path(entry)
    if not file_path.exists():
        return "DATA SET FILE NOT FOUND"

    text_ccsid = str(entry.get("text_ccsid", TEXT_ENCODING)).strip() or TEXT_ENCODING
    recfm = str(entry.get("recfm", "")).strip().upper()
    is_fixed = recfm in {"F", "FB"}
    try:
        lrecl = int(entry.get("lrecl", 0))
    except Exception:
        lrecl = 0

    try:
        if is_fixed and lrecl > 0:
            records = []
            for line in lines:
                record = str(line).encode(text_ccsid)
                if len(record) < lrecl:
                    record = record + (b" " * (lrecl - len(record)))
                elif len(record) > lrecl:
                    record = record[:lrecl]
                records.append(record)
            payload = b"".join(records)
        else:
            text = "\n".join(lines)
            payload = text.encode(text_ccsid)
        file_path.write_bytes(payload)
    except Exception as e:
        return f"SAVE FAILED: {e}"

    return None
