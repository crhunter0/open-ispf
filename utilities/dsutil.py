from pathlib import Path

from app_config import BASE_DIR, TEXT_ENCODING
from utilities.base import UtilityActions, UtilityLayout, UtilityResult


def _default_dataset_relpath(dsn: str) -> str:
    parts = [p for p in dsn.split(".") if p]
    if not parts:
        return "data/UNKNOWN/DATA.dat"

    hlq = parts[0]
    rest = parts[1:] or ["DATA"]
    filename = "_".join(rest) + ".dat"
    return f"data/{hlq}/{filename}"


def _find_entry(catalog: list, norm_dsn: str, normalize_dsn) -> tuple[int, dict]:
    for i, entry in enumerate(catalog):
        if normalize_dsn(entry.get("dsn", "")) == norm_dsn:
            return i, entry
    return -1, None


def handle_dsutil(client_socket, actions: UtilityActions, layout: UtilityLayout) -> UtilityResult:
    option = ""
    dsn = ""
    new_dsn = ""
    msg = None

    while True:
        actions.send_ispf_dsutil(
            client_socket,
            option=option,
            dsn=dsn,
            new_dsn=new_dsn,
            short_msg=msg,
        )

        result = actions.read_client_input(client_socket)
        if result is None:
            return UtilityResult(message=None, disconnect=True)

        aid, cursor_addr, fields = result
        aid_str = actions.aid_to_string(aid)

        entered_opt = fields.get(layout.dsutil_option_addr, "").rstrip().upper()
        entered_dsn = fields.get(layout.dsutil_dsn_addr, "").strip().upper()
        entered_new_dsn = fields.get(layout.dsutil_new_dsn_addr, "").strip().upper()

        if entered_opt:
            option = entered_opt[:1]
        if entered_dsn:
            dsn = entered_dsn
        if entered_new_dsn:
            new_dsn = entered_new_dsn

        if option == "X" or aid_str in ("PF3", "PF15"):
            return UtilityResult(message=None)

        if aid_str != "Enter":
            msg = "PRESS ENTER TO PROCESS OPTION OR PF3 TO RETURN"
            continue

        if not option:
            msg = "ENTER A DATA SET UTILITY OPTION"
            continue
        if option not in {"A", "D", "R", "C", "U", "I", "M"}:
            msg = f"INVALID OPTION: {option}"
            continue
        if option in {"C", "U", "I", "M"}:
            msg = f"OPTION {option} NOT IMPLEMENTED YET (SUPPORTED: A R D)"
            continue

        norm_dsn = actions.normalize_dsn(dsn)
        if not norm_dsn:
            msg = "ENTER DATA SET NAME"
            continue

        catalog = list(actions.load_catalog())
        idx, entry = _find_entry(catalog, norm_dsn, actions.normalize_dsn)

        if option == "A":
            if entry is not None:
                msg = f"DATA SET ALREADY EXISTS: {norm_dsn}"
                continue

            rel_path = _default_dataset_relpath(norm_dsn)
            abs_path = BASE_DIR / rel_path
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            if not abs_path.exists():
                abs_path.write_bytes("".encode(TEXT_ENCODING))

            catalog.append(
                {
                    "dsn": norm_dsn,
                    "path": rel_path,
                    "org": "PS",
                    "recfm": "FB",
                    "lrecl": 80,
                    "content_mode": "text",
                    "text_ccsid": TEXT_ENCODING,
                }
            )
            save_err = actions.save_catalog(catalog)
            if save_err:
                msg = save_err
            else:
                msg = f"ALLOCATED {norm_dsn}"
            continue

        if option == "D":
            if entry is None:
                msg = f"DATA SET NOT FOUND: {norm_dsn}"
                continue

            try:
                file_path = BASE_DIR / str(entry.get("path", "")).strip()
                if file_path.exists() and file_path.is_file():
                    file_path.unlink()
            except Exception as e:
                msg = f"DELETE FAILED: {e}"
                continue

            del catalog[idx]
            save_err = actions.save_catalog(catalog)
            if save_err:
                msg = save_err
            else:
                msg = f"DELETED {norm_dsn}"
            continue

        # option == "R"
        norm_new = actions.normalize_dsn(new_dsn)
        if not norm_new:
            msg = "ENTER NEW DATA SET NAME"
            continue
        if entry is None:
            msg = f"DATA SET NOT FOUND: {norm_dsn}"
            continue

        new_idx, _ = _find_entry(catalog, norm_new, actions.normalize_dsn)
        if new_idx != -1:
            msg = f"TARGET ALREADY EXISTS: {norm_new}"
            continue

        old_path = BASE_DIR / str(entry.get("path", "")).strip()
        new_rel = _default_dataset_relpath(norm_new)
        new_path = BASE_DIR / new_rel

        try:
            if old_path.exists() and old_path.is_file():
                new_path.parent.mkdir(parents=True, exist_ok=True)
                old_path.rename(new_path)
                entry["path"] = new_rel
            entry["dsn"] = norm_new
        except Exception as e:
            msg = f"RENAME FAILED: {e}"
            continue

        save_err = actions.save_catalog(catalog)
        if save_err:
            msg = save_err
        else:
            dsn = norm_new
            new_dsn = ""
            msg = f"RENAMED TO {norm_new}"
