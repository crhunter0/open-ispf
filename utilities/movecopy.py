import shutil
from pathlib import Path

from app_config import BASE_DIR
from utilities.base import UtilityActions, UtilityLayout, UtilityResult


def _find_entry(catalog: list, norm_dsn: str, normalize_dsn) -> tuple[int, dict]:
    for i, entry in enumerate(catalog):
        if normalize_dsn(entry.get("dsn", "")) == norm_dsn:
            return i, entry
    return -1, None


def _entry_abs_path(entry: dict) -> Path:
    raw = str(entry.get("path", "")).strip()
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return BASE_DIR / candidate


def _normalize_member_name(raw: str) -> str:
    member = (raw or "").strip().upper()
    if not member:
        return ""
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@$#-_")
    if any(ch not in allowed for ch in member):
        return ""
    return member[:35]


def _split_dsn_member(raw: str, normalize_dsn) -> tuple[str, str]:
    text = (raw or "").strip().upper()
    if not text:
        return "", ""

    if "(" in text and text.endswith(")"):
        dsn_part, member_part = text[:-1].split("(", 1)
        return normalize_dsn(dsn_part), _normalize_member_name(member_part)

    return normalize_dsn(text), ""


def _default_dataset_relpath(dsn: str, dsorg: str = "PS") -> str:
    parts = [p for p in dsn.split(".") if p]
    if not parts:
        return "data/UNKNOWN/DATA" if dsorg == "PO" else "data/UNKNOWN/DATA.dat"

    hlq = parts[0]
    rest = parts[1:] or ["DATA"]
    if dsorg == "PO":
        return f"data/{hlq}/{'_'.join(rest)}"
    return f"data/{hlq}/{'_'.join(rest)}.dat"


def _copy_or_move_file(source: Path, target: Path, move: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if move:
        source.rename(target)
    else:
        shutil.copy2(source, target)


def _copy_or_move_tree(source: Path, target: Path, move: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if move:
        source.rename(target)
    else:
        shutil.copytree(source, target)


def _extract_jump_option(*values: str) -> str:
    for value in values:
        text = str(value or "").strip().upper()
        if text.startswith("=") and len(text) > 1:
            return text[1:]
    return ""


def handle_movecopy(client_socket, actions: UtilityActions, layout: UtilityLayout) -> UtilityResult:
    option = "C"
    from_dsn = ""
    from_member = ""
    to_dsn = ""
    to_member = ""
    msg = None

    while True:
        actions.send_ispf_movecopy(
            client_socket,
            option=option,
            from_dsn=from_dsn,
            from_member=from_member,
            to_dsn=to_dsn,
            to_member=to_member,
            short_msg=msg,
        )

        result = actions.read_client_input(client_socket)
        if result is None:
            return UtilityResult(message=None, disconnect=True)

        aid, cursor_addr, fields = result
        aid_str = actions.aid_to_string(aid)

        entered_opt = fields.get(layout.movecopy_option_addr, "").strip().upper()
        entered_from_dsn = fields.get(layout.movecopy_from_dsn_addr, "").strip().upper()
        entered_from_member = fields.get(layout.movecopy_from_member_addr, "").strip().upper()
        entered_to_dsn = fields.get(layout.movecopy_to_dsn_addr, "").strip().upper()
        entered_to_member = fields.get(layout.movecopy_to_member_addr, "").strip().upper()

        jump_option = _extract_jump_option(
            entered_opt,
            entered_from_dsn,
            entered_from_member,
            entered_to_dsn,
            entered_to_member,
        )
        if jump_option:
            return UtilityResult(message=None, jump_option=jump_option)

        if entered_opt:
            option = entered_opt[:2]
        if entered_from_dsn:
            from_dsn = entered_from_dsn
        if entered_from_member:
            from_member = entered_from_member
        if entered_to_dsn:
            to_dsn = entered_to_dsn
        if entered_to_member:
            to_member = entered_to_member

        if option == "X" or aid_str in ("PF3", "PF15"):
            return UtilityResult(message=None)

        # 3270 emulator/key mapping differences can send non-Enter AIDs for
        # what is effectively a submit action on this panel. For robustness,
        # treat any non-exit key as submit.
        aid_str = "Enter"

        op = option
        if op in {"CP", "C"}:
            move_flag = False
        elif op in {"MP", "M"}:
            move_flag = True
        else:
            msg = f"INVALID OPTION: {option}"
            continue

        norm_from_dsn, inline_from_member = _split_dsn_member(from_dsn, actions.normalize_dsn)
        norm_to_dsn, inline_to_member = _split_dsn_member(to_dsn, actions.normalize_dsn)

        eff_from_member = _normalize_member_name(inline_from_member or from_member)
        eff_to_member = _normalize_member_name(inline_to_member or to_member)

        if not norm_from_dsn:
            msg = "ENTER FROM DATA SET NAME"
            continue
        if not norm_to_dsn:
            msg = "ENTER TO DATA SET NAME"
            continue

        catalog = list(actions.load_catalog())
        src_idx, src_entry = _find_entry(catalog, norm_from_dsn, actions.normalize_dsn)
        if src_entry is None:
            msg = f"FROM DATA SET NOT FOUND: {norm_from_dsn}"
            continue

        src_is_pds = actions.is_pds_like(src_entry)

        if eff_from_member:
            if not src_is_pds:
                msg = "FROM MEMBER REQUIRES PARTITIONED DATA SET"
                continue

            src_dir = _entry_abs_path(src_entry)
            src_path = src_dir / eff_from_member
            if not src_path.exists() or not src_path.is_file():
                msg = f"FROM MEMBER NOT FOUND: {eff_from_member}"
                continue

            dst_idx, dst_entry = _find_entry(catalog, norm_to_dsn, actions.normalize_dsn)
            if dst_entry is None:
                rel = _default_dataset_relpath(norm_to_dsn, "PO")
                dst_entry = {
                    "dsn": norm_to_dsn,
                    "path": rel,
                    "org": "PO",
                    "recfm": src_entry.get("recfm", "FB"),
                    "lrecl": src_entry.get("lrecl", 80),
                    "content_mode": src_entry.get("content_mode", "text"),
                    "text_ccsid": src_entry.get("text_ccsid", "cp037"),
                }
                catalog.append(dst_entry)
                dst_idx = len(catalog) - 1
            elif not actions.is_pds_like(dst_entry):
                msg = "TO DATA SET MUST BE PARTITIONED FOR MEMBER COPY/MOVE"
                continue

            dst_dir = _entry_abs_path(dst_entry)
            dst_dir.mkdir(parents=True, exist_ok=True)
            member_name = eff_to_member or eff_from_member
            dst_path = dst_dir / member_name

            if src_path.resolve() == dst_path.resolve():
                msg = "FROM AND TO MEMBERS ARE THE SAME"
                continue
            if dst_path.exists():
                msg = f"TO MEMBER ALREADY EXISTS: {member_name}"
                continue

            try:
                _copy_or_move_file(src_path, dst_path, move_flag)
            except Exception as e:
                msg = f"MEMBER {'MOVE' if move_flag else 'COPY'} FAILED: {e}"
                continue

            save_err = actions.save_catalog(catalog)
            if save_err:
                msg = save_err
                continue

            verb = "MOVED" if move_flag else "COPIED"
            msg = f"{verb} MEMBER {eff_from_member} TO {norm_to_dsn}({member_name})"
            continue

        dst_idx, dst_entry = _find_entry(catalog, norm_to_dsn, actions.normalize_dsn)
        if dst_entry is not None:
            msg = f"TO DATA SET ALREADY EXISTS: {norm_to_dsn}"
            continue

        src_path = _entry_abs_path(src_entry)
        src_org = str(src_entry.get("org", "PS")).strip().upper() or "PS"
        dst_rel = _default_dataset_relpath(norm_to_dsn, "PO" if src_is_pds else "PS")
        dst_path = BASE_DIR / dst_rel

        try:
            if src_is_pds:
                if not src_path.exists() or not src_path.is_dir():
                    msg = f"FROM PDS PATH INVALID: {norm_from_dsn}"
                    continue
                _copy_or_move_tree(src_path, dst_path, move_flag)
            else:
                if not src_path.exists() or not src_path.is_file():
                    msg = f"FROM DATA SET PATH INVALID: {norm_from_dsn}"
                    continue
                _copy_or_move_file(src_path, dst_path, move_flag)
        except Exception as e:
            msg = f"DATA SET {'MOVE' if move_flag else 'COPY'} FAILED: {e}"
            continue

        if move_flag:
            src_entry["dsn"] = norm_to_dsn
            src_entry["path"] = dst_rel
        else:
            new_entry = dict(src_entry)
            new_entry["dsn"] = norm_to_dsn
            new_entry["path"] = dst_rel
            new_entry["org"] = src_org
            catalog.append(new_entry)

        save_err = actions.save_catalog(catalog)
        if save_err:
            msg = save_err
            continue

        verb = "MOVED" if move_flag else "COPIED"
        msg = f"{verb} {norm_from_dsn} TO {norm_to_dsn}"
