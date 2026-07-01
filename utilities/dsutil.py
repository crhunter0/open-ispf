import shutil
from pathlib import Path

from app_config import BASE_DIR, TEXT_ENCODING
from utilities.base import UtilityActions, UtilityLayout, UtilityResult


def _default_dataset_relpath(dsn: str, dsorg: str = "PS") -> str:
    parts = [p for p in dsn.split(".") if p]
    if not parts:
        return "data/UNKNOWN/DATA" if dsorg == "PO" else "data/UNKNOWN/DATA.dat"

    hlq = parts[0]
    rest = parts[1:] or ["DATA"]
    if dsorg == "PO":
        return f"data/{hlq}/{'_'.join(rest)}"
    filename = "_".join(rest) + ".dat"
    return f"data/{hlq}/{filename}"


def _find_entry(catalog: list, norm_dsn: str, normalize_dsn) -> tuple[int, dict]:
    for i, entry in enumerate(catalog):
        if normalize_dsn(entry.get("dsn", "")) == norm_dsn:
            return i, entry
    return -1, None


def _extract_jump_option(*values: str) -> str:
    for value in values:
        text = str(value or "").strip().upper()
        if text.startswith("=") and len(text) > 1:
            return text[1:]
    return ""


def handle_dsutil(client_socket, actions: UtilityActions, layout: UtilityLayout) -> UtilityResult:
    option = ""
    dsn = ""
    new_dsn = ""
    dsorg = "PS"
    msg = None

    while True:
        actions.send_ispf_dsutil(
            client_socket,
            option=option,
            dsn=dsn,
            new_dsn=new_dsn,
            dsorg=dsorg,
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
        entered_type = fields.get(layout.dsutil_type_addr, "").strip().upper()

        jump_option = _extract_jump_option(entered_opt, entered_dsn, entered_new_dsn, entered_type)
        if jump_option:
            return UtilityResult(message=None, jump_option=jump_option)

        if entered_opt:
            option = entered_opt[:1]
        if entered_dsn:
            dsn = entered_dsn
        if entered_new_dsn:
            new_dsn = entered_new_dsn
        if entered_type:
            dsorg = entered_type[:2]

        if option == "X" or aid_str in ("PF3", "PF15"):
            return UtilityResult(message=None)

        # 3270 emulator/key mapping differences can send non-Enter AIDs for
        # what is effectively a submit action on this panel. For robustness,
        # treat any non-exit key as submit.
        aid_str = "Enter"

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

            if dsorg not in {"PS", "PO"}:
                msg = "INVALID DATA SET TYPE (USE PS OR PO)"
                continue

            rel_path = _default_dataset_relpath(norm_dsn, dsorg)
            abs_path = BASE_DIR / rel_path
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if dsorg == "PO":
                    abs_path.mkdir(parents=True, exist_ok=False)
                elif not abs_path.exists():
                    abs_path.write_bytes("".encode(TEXT_ENCODING))
            except FileExistsError:
                msg = f"TARGET PATH ALREADY EXISTS: {rel_path}"
                continue
            except Exception as e:
                msg = f"ALLOCATE FAILED: {e}"
                continue

            catalog.append(
                {
                    "dsn": norm_dsn,
                    "path": rel_path,
                    "org": dsorg,
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
                entry_path = BASE_DIR / str(entry.get("path", "")).strip()
                if entry_path.exists() and entry_path.is_dir():
                    shutil.rmtree(entry_path)
                elif entry_path.exists() and entry_path.is_file():
                    entry_path.unlink()
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
        entry_org = str(entry.get("org", "PS")).strip().upper() or "PS"
        new_rel = _default_dataset_relpath(norm_new, entry_org)
        new_path = BASE_DIR / new_rel

        try:
            if old_path.exists():
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
