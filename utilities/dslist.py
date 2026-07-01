import fnmatch
from pathlib import Path
from typing import Optional

from app_config import BASE_DIR, TEXT_ENCODING

from utilities.base import UtilityActions, UtilityLayout, UtilityResult


def _dataset_display_geometry(layout: UtilityLayout, show_cols: bool, hex_mode: bool) -> tuple[int, int, int]:
    panel_first_row = layout.dataset_lines_first_row + (1 if show_cols else 0)
    data_row_start = panel_first_row + 1
    content_rows = max(1, layout.dataset_lines_max_rows - 2)
    rows_per_record = 4 if hex_mode else 1
    records_per_page = max(1, content_rows // rows_per_record)
    return data_row_start, rows_per_record, records_per_page


def _dataset_scroll_amount(
    scroll_mode: str,
    cursor_addr: Optional[int],
    data_row_start: int,
    rows_per_record: int,
    records_per_page: int,
) -> int:
    if scroll_mode == "CSR" and cursor_addr is not None:
        cursor_row = cursor_addr // 80
        if cursor_row >= data_row_start:
            cursor_offset = (cursor_row - data_row_start) // rows_per_record
            return max(1, cursor_offset + 1)
    return records_per_page


def _has_edit_row_input(
    fields: dict,
    data_row_start: int,
    rows_per_record: int,
    records_per_page: int,
    hex_mode: bool,
    layout: UtilityLayout,
) -> bool:
    prefix_width = 6
    for i in range(records_per_page):
        row = data_row_start + (i * rows_per_record)
        if hex_mode:
            row += 3

        prefix_start = row * 80
        prefix_end = prefix_start + prefix_width
        text_start = row * 80 + (layout.dataset_edit_text_sf_col + 1)
        text_end = text_start + layout.dataset_edit_text_width

        for addr, value in fields.items():
            if not isinstance(addr, int):
                continue
            fragment = str(value)
            if not fragment:
                continue

            frag_start = addr
            frag_end = addr + len(fragment)

            prefix_hit = frag_start < prefix_end and frag_end > prefix_start
            text_hit = frag_start < text_end and frag_end > text_start
            if prefix_hit or text_hit:
                return True

    return False


def _entry_abs_path(entry: dict) -> Path:
    raw_path = str(entry.get("path", "")).strip()
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    return BASE_DIR / candidate


def _list_pds_members(pds_entry: dict, member_pattern: str) -> tuple[list, str]:
    pds_path = _entry_abs_path(pds_entry)
    if not pds_path.exists():
        dsn = str(pds_entry.get("dsn", "")).strip().upper()
        return [], f"PDS NOT FOUND: {dsn}"
    if not pds_path.is_dir():
        dsn = str(pds_entry.get("dsn", "")).strip().upper()
        return [], f"PDS PATH IS NOT A DIRECTORY: {dsn}"

    patt = (member_pattern or "*").strip().upper() or "*"
    rows = []
    for child in sorted(pds_path.iterdir(), key=lambda p: p.name.upper()):
        if not child.is_file():
            continue
        member = child.name.upper()
        if not fnmatch.fnmatchcase(member, patt):
            continue
        rows.append(
            {
                "dsn": member,
                "org": "MBR",
                "recfm": str(pds_entry.get("recfm", "FB"))[:5],
                "lrecl": pds_entry.get("lrecl", 80),
                "content_mode": str(pds_entry.get("content_mode", "text")),
                "text_ccsid": str(pds_entry.get("text_ccsid", TEXT_ENCODING)),
                "path": str(child),
                "_member_name": member,
            }
        )

    return rows, None


def _normalize_member_name(raw: str) -> str:
    member = (raw or "").strip().upper()
    if not member:
        return ""
    if any(ch in member for ch in ("*", "?", "/", "\\")):
        return ""
    if ".." in member:
        return ""
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@$#-_")
    if any(ch not in allowed for ch in member):
        return ""
    return member[:35]


def _extract_member_operand(raw: str) -> str:
    value = (raw or "").strip().upper()
    if not value:
        return ""
    parts = value.split()
    if len(parts) >= 2 and parts[0] in {"N", "R"}:
        return _normalize_member_name(parts[1])
    return _normalize_member_name(value)


def _next_auto_member_name(pds_path: Path) -> str:
    for i in range(1, 10000):
        candidate = f"NEW{i:04d}"
        if not (pds_path / candidate).exists():
            return candidate
    return "NEW9999"


def _extract_jump_option(value: str) -> str:
    text = str(value or "").strip().upper()
    if text.startswith("=") and len(text) > 1:
        return text[1:]
    return ""


def _run_pds_member_list_session(
    client_socket,
    actions: UtilityActions,
    layout: UtilityLayout,
    pds_entry: dict,
    default_cmd: str = "E",
) -> UtilityResult:
    pds_dsn = actions.normalize_dsn(pds_entry.get("dsn", ""))
    member_filter = "*"
    member_msg = None
    pds_path = _entry_abs_path(pds_entry)

    while True:
        member_rows, load_error = _list_pds_members(pds_entry, member_filter)
        if load_error:
            return UtilityResult(message=load_error)

        panel_msg = f"PDS {pds_dsn}  {member_msg}" if member_msg else f"PDS {pds_dsn}"
        actions.send_ispf_dslist(
            client_socket,
            level=member_filter,
            rows=member_rows,
            short_msg=panel_msg,
            footer_hint="Line cmds: B/V/E open  D delete  R rename  N new (use level field as target)",
        )

        member_result = actions.read_client_input(client_socket)
        if member_result is None:
            return UtilityResult(message=None, disconnect=True)

        member_aid, member_cursor_addr, member_fields = member_result
        member_aid_str = actions.aid_to_string(member_aid)
        entered_level = member_fields.get(layout.dslist_level_addr, "").strip().upper()

        jump_option = _extract_jump_option(entered_level)
        if jump_option:
            return UtilityResult(message=None, jump_option=jump_option)

        if entered_level == "X" or member_aid_str in ("PF3", "PF15"):
            return UtilityResult(message=None)

        # 3270 emulator/key mapping differences can send non-Enter AIDs for
        # what is effectively a submit action on this panel. For robustness,
        # treat any non-exit key as submit.
        member_aid_str = "Enter"

        selected = None
        selected_cmd = None
        cmd_count = 0
        for i, member in enumerate(member_rows[: layout.dslist_results_max_rows]):
            cmd_addr = (layout.dslist_results_first_row + i) * 80 + (layout.dslist_cmd_sf_col + 1)
            cmd = member_fields.get(cmd_addr, "").strip().upper()
            if not cmd:
                continue
            cmd_count += 1
            if cmd in {"B", "V", "E", "N", "R", "D"}:
                if selected is None:
                    selected = member
                    selected_cmd = cmd
            else:
                member_msg = f"INVALID LINE CMD: {cmd} (USE B, V, E, N, R, OR D)"
                selected = None
                selected_cmd = None
                break

        if selected is not None:
            if cmd_count > 1:
                member_msg = "ENTER ONLY ONE LINE COMMAND"
                continue

            if selected_cmd == "D":
                member_path = Path(str(selected.get("path", "")).strip())
                try:
                    if member_path.exists() and member_path.is_file():
                        member_path.unlink()
                        member_msg = f"DELETED {selected.get('_member_name', '')}"
                    else:
                        member_msg = f"MEMBER NOT FOUND: {selected.get('_member_name', '')}"
                except Exception as e:
                    member_msg = f"DELETE FAILED: {e}"
                continue

            if selected_cmd in {"N", "R"}:
                target_member = _extract_member_operand(entered_level)
                if selected_cmd == "N" and not target_member:
                    target_member = _next_auto_member_name(pds_path)
                if selected_cmd == "R" and not target_member:
                    member_msg = "ENTER TARGET MEMBER IN DSNAME LEVEL FIELD"
                    continue

                target_path = pds_path / target_member
                try:
                    if selected_cmd == "N":
                        if target_path.exists():
                            member_msg = f"MEMBER ALREADY EXISTS: {target_member}"
                            continue
                        target_path.write_bytes("".encode(str(pds_entry.get("text_ccsid", TEXT_ENCODING))))
                        if entered_level.strip():
                            member_msg = f"CREATED {target_member}"
                        else:
                            member_msg = f"CREATED {target_member} (AUTO NAME)"
                    else:
                        source_path = Path(str(selected.get("path", "")).strip())
                        source_member = str(selected.get("_member_name", "")).strip().upper()
                        if not source_path.exists() or not source_path.is_file():
                            member_msg = f"MEMBER NOT FOUND: {source_member}"
                            continue
                        if target_path.exists():
                            member_msg = f"TARGET MEMBER EXISTS: {target_member}"
                            continue
                        source_path.rename(target_path)
                        member_msg = f"RENAMED {source_member} TO {target_member}"
                except Exception as e:
                    member_msg = f"MEMBER UPDATE FAILED: {e}"
                continue

            member_entry = {
                "dsn": f"{pds_dsn}({selected.get('_member_name', '')})",
                "path": selected.get("path", ""),
                "org": "PS",
                "recfm": selected.get("recfm", "FB"),
                "lrecl": selected.get("lrecl", 80),
                "content_mode": selected.get("content_mode", "text"),
                "text_ccsid": selected.get("text_ccsid", TEXT_ENCODING),
            }
            launched = _run_dataset_editor_session(
                client_socket=client_socket,
                actions=actions,
                layout=layout,
                selected=member_entry,
                selected_cmd=selected_cmd,
            )
            if launched.disconnect:
                return launched
            if launched.jump_option:
                return launched
            member_msg = launched.message
            continue

        if member_aid_str != "Enter":
            member_msg = "USE ENTER OR LINE CMD B/V/E/N/R/D; PF3 TO RETURN"
            continue

        if entered_level.startswith("N "):
            target_member = _extract_member_operand(entered_level)
            if not target_member:
                member_msg = "INVALID MEMBER NAME"
                continue

            target_path = pds_path / target_member
            try:
                if target_path.exists():
                    member_msg = f"MEMBER ALREADY EXISTS: {target_member}"
                    continue
                target_path.write_bytes("".encode(str(pds_entry.get("text_ccsid", TEXT_ENCODING))))
                member_msg = f"CREATED {target_member}"
            except Exception as e:
                member_msg = f"MEMBER UPDATE FAILED: {e}"
            continue

        if entered_level:
            member_filter = entered_level
        member_msg = f"{len(member_rows)} MEMBER(S) LISTED"


def _run_dataset_editor_session(
    client_socket,
    actions: UtilityActions,
    layout: UtilityLayout,
    selected: dict,
    selected_cmd: str = "E",
) -> UtilityResult:
    if actions.is_pds_like(selected):
        return _run_pds_member_list_session(
            client_socket=client_socket,
            actions=actions,
            layout=layout,
            pds_entry=selected,
            default_cmd="E",
        )

    lines, load_error = actions.load_dataset_lines(selected)
    if load_error:
        return UtilityResult(message=load_error)

    dsn = actions.normalize_dsn(selected.get("dsn", ""))
    page = 0
    ds_msg = None
    ds_cmd = ""
    dslist_scroll = "PAGE"
    dslist_show_cols = False
    dslist_hex_mode = False
    pending_copy_source = None
    pending_copy_block_start = None
    pending_copy_block_range = None
    pending_rr_start = None
    pending_dd_start = None
    return_msg = None

    while True:
        line_cmd_overrides = {}
        if pending_copy_source is not None:
            line_cmd_overrides[pending_copy_source] = "C"
        if pending_copy_block_start is not None:
            line_cmd_overrides[pending_copy_block_start] = "CC"
        if pending_copy_block_range is not None:
            cc_start, cc_end = pending_copy_block_range
            line_cmd_overrides[cc_start] = "CC"
            line_cmd_overrides[cc_end] = "CC"
        if pending_rr_start is not None:
            line_cmd_overrides[pending_rr_start] = "RR"
        if pending_dd_start is not None:
            line_cmd_overrides[pending_dd_start] = "DD"

        actions.send_dataset_panel(
            client_socket,
            dsn=dsn,
            mode=selected_cmd,
            lines=lines,
            page=page,
            command=ds_cmd,
            scroll=dslist_scroll,
            show_cols=dslist_show_cols,
            hex_mode=dslist_hex_mode,
            lrecl=selected.get("lrecl", layout.dataset_line_width),
            short_msg=ds_msg,
            line_cmd_overrides=line_cmd_overrides,
        )
        ds_result = actions.read_client_input(client_socket)
        if ds_result is None:
            return UtilityResult(message=None, disconnect=True)

        ds_aid, ds_cursor_addr, ds_fields = ds_result
        ds_aid_str = actions.aid_to_string(ds_aid)
        ds_cmd = ds_fields.get(layout.dataset_cmd_addr, "").strip().upper()
        ds_scroll = ds_fields.get(layout.dataset_scroll_addr, "").strip().upper()

        jump_option = _extract_jump_option(ds_cmd)
        if jump_option:
            return UtilityResult(message=None, jump_option=jump_option)

        if selected_cmd == "E" and ds_aid_str != "Enter":
            data_row_start, rows_per_record, records_per_page = _dataset_display_geometry(
                layout,
                dslist_show_cols,
                dslist_hex_mode,
            )
            if _has_edit_row_input(
                ds_fields,
                data_row_start,
                rows_per_record,
                records_per_page,
                dslist_hex_mode,
                layout,
            ):
                ds_aid_str = "Enter"

        if ds_scroll in {"PAGE", "CSR"}:
            if ds_scroll != dslist_scroll:
                dslist_scroll = ds_scroll
        elif ds_scroll and not ds_cmd:
            ds_cmd = ds_scroll

        if ds_scroll in {"PAGE", "CSR"} and not ds_cmd:
            ds_msg = None
            ds_cmd = ""

        cmd_parts = ds_cmd.split()
        cmd_root = cmd_parts[0] if cmd_parts else ""

        if cmd_root == "COLS":
            if len(cmd_parts) == 1:
                dslist_show_cols = not dslist_show_cols
            elif len(cmd_parts) > 1 and cmd_parts[1] in {"ON", "1"}:
                dslist_show_cols = True
            elif len(cmd_parts) > 1 and cmd_parts[1] in {"OFF", "0"}:
                dslist_show_cols = False
            else:
                ds_msg = f"UNKNOWN COMMAND: {ds_cmd}"
                ds_cmd = ""
                continue
            ds_msg = f"COLS {'ON' if dslist_show_cols else 'OFF'}"
            ds_cmd = ""
            continue

        if cmd_root == "HEX":
            if len(cmd_parts) == 1:
                dslist_hex_mode = not dslist_hex_mode
            elif len(cmd_parts) > 1 and cmd_parts[1] in {"ON", "1"}:
                dslist_hex_mode = True
            elif len(cmd_parts) > 1 and cmd_parts[1] in {"OFF", "0"}:
                dslist_hex_mode = False
            else:
                ds_msg = f"UNKNOWN COMMAND: {ds_cmd}"
                ds_cmd = ""
                continue
            ds_msg = f"HEX {'ON' if dslist_hex_mode else 'OFF'}"
            ds_cmd = ""
            continue

        if cmd_root == "SCROLL" and len(cmd_parts) == 2 and cmd_parts[1] in {"PAGE", "CSR"}:
            dslist_scroll = cmd_parts[1]
            ds_msg = None
            ds_cmd = ""
            continue

        if ds_cmd in {"PAGE", "CSR"}:
            dslist_scroll = ds_cmd
            ds_msg = None
            ds_cmd = ""
            continue

        if ds_aid_str == "Enter" and ds_cmd in {"X", "END", "CANCEL", "EXIT"}:
            if selected_cmd == "E":
                save_error = actions.save_dataset_lines(selected, lines)
                if save_error:
                    ds_msg = save_error
                    continue
                return_msg = f"{dsn} SAVED"
            else:
                return_msg = None
            break

        if ds_aid_str == "Enter" and ds_cmd in {
            "UP",
            "DOWN",
            "SCROLL UP",
            "SCROLL DOWN",
            "S",
            "S UP",
            "S DOWN",
        }:
            data_row_start, rows_per_record, records_per_page = _dataset_display_geometry(
                layout,
                dslist_show_cols,
                dslist_hex_mode,
            )
            scroll_amount = _dataset_scroll_amount(
                dslist_scroll,
                ds_cursor_addr,
                data_row_start,
                rows_per_record,
                records_per_page,
            )
            max_page = max(0, len(lines) - records_per_page)
            if "DOWN" in ds_cmd or ds_cmd.endswith("D"):
                page = min(max_page, page + scroll_amount)
            else:
                page = max(0, page - scroll_amount)
            ds_msg = None
            ds_cmd = ""
            continue

        if ds_aid_str in ("PF7",):
            data_row_start, rows_per_record, records_per_page = _dataset_display_geometry(
                layout,
                dslist_show_cols,
                dslist_hex_mode,
            )
            scroll_amount = _dataset_scroll_amount(
                dslist_scroll,
                ds_cursor_addr,
                data_row_start,
                rows_per_record,
                records_per_page,
            )
            page = max(0, page - scroll_amount)
            ds_msg = None
            ds_cmd = ""
            continue

        if ds_aid_str in ("PF8",):
            data_row_start, rows_per_record, records_per_page = _dataset_display_geometry(
                layout,
                dslist_show_cols,
                dslist_hex_mode,
            )
            scroll_amount = _dataset_scroll_amount(
                dslist_scroll,
                ds_cursor_addr,
                data_row_start,
                rows_per_record,
                records_per_page,
            )
            max_page = max(0, len(lines) - records_per_page)
            page = min(max_page, page + scroll_amount)
            ds_msg = None
            ds_cmd = ""
            continue

        if ds_aid_str in ("PF3", "PF15"):
            if selected_cmd == "E":
                save_error = actions.save_dataset_lines(selected, lines)
                if save_error:
                    ds_msg = save_error
                    continue
                return_msg = f"{dsn} SAVED"
            else:
                return_msg = None
            break

        if selected_cmd == "E" and ds_aid_str == "Enter":
            def _overlay_field(start_addr: int, width: int, seed: str) -> str:
                merged = list((seed or "").ljust(width)[:width])
                end_addr = start_addr + width
                for addr, value in sorted(ds_fields.items(), key=lambda item: item[0] if isinstance(item[0], int) else -1):
                    if not isinstance(addr, int):
                        continue
                    fragment = str(value)
                    frag_start = addr
                    frag_end = addr + len(fragment)
                    if frag_end <= start_addr or frag_start >= end_addr:
                        continue

                    overlap_start = max(start_addr, frag_start)
                    overlap_end = min(end_addr, frag_end)
                    src_offset = overlap_start - frag_start
                    dst_offset = overlap_start - start_addr
                    segment = fragment[src_offset : src_offset + (overlap_end - overlap_start)]
                    for j, ch in enumerate(segment):
                        merged[dst_offset + j] = ch

                return "".join(merged)

            def _field_touched(start_addr: int, width: int) -> bool:
                end_addr = start_addr + width
                for addr, value in ds_fields.items():
                    if not isinstance(addr, int):
                        continue
                    fragment = str(value)
                    if not fragment:
                        continue
                    frag_start = addr
                    frag_end = addr + len(fragment)
                    if frag_end <= start_addr or frag_start >= end_addr:
                        continue
                    return True
                return False

            data_row_start, rows_per_record, records_per_page = _dataset_display_geometry(
                layout,
                dslist_show_cols,
                dslist_hex_mode,
            )
            start_idx = page
            prefix_width = 6
            line_cmds = []
            invalid_line_cmd = None
            edited_rows = []
            for i in range(records_per_page):
                row = data_row_start + (i * rows_per_record)
                if dslist_hex_mode:
                    row += 3

                row_idx = start_idx + i
                prefix_start = row * 80
                text_start = row * 80 + (layout.dataset_edit_text_sf_col + 1)

                current_line = lines[row_idx] if row_idx < len(lines) else ""
                default_seq = f"{row_idx + 1:06d}"
                prefix_seed = default_seq
                if pending_copy_source == row_idx:
                    prefix_seed = "C"
                # Some emulators/reporting paths can shift modified
                # field addresses by one column around col 1. Parse
                # both alignments and choose the stronger signal.
                raw_prefix0 = _overlay_field(prefix_start, prefix_width, prefix_seed)
                raw_prefix1 = _overlay_field(prefix_start + 1, prefix_width, prefix_seed)
                cmd_scan0 = raw_prefix0.upper()
                cmd_scan1 = raw_prefix1.upper()
                prefix_touched0 = _field_touched(prefix_start, prefix_width)
                prefix_touched1 = _field_touched(prefix_start + 1, prefix_width)

                # Parse command letters from the reconstructed 6-char
                # prefix value for deterministic behavior across
                # emulator fragment/update differences.
                alpha_token0 = "".join(ch for ch in cmd_scan0 if ch.isalpha())
                alpha_token1 = "".join(ch for ch in cmd_scan1 if ch.isalpha())
                use_alt = prefix_touched1 and len(alpha_token1) > len(alpha_token0)
                cmd_scan = cmd_scan1 if use_alt else cmd_scan0
                raw_prefix = raw_prefix1 if use_alt else raw_prefix0
                alpha_token = alpha_token1 if use_alt else alpha_token0
                prefix_touched = prefix_touched0 or prefix_touched1
                parsed_line_cmd = None

                if alpha_token:
                    if set(alpha_token) == {"C"} and len(alpha_token) >= 2:
                        parsed_line_cmd = "CC"
                    elif set(alpha_token) == {"D"} and len(alpha_token) >= 2:
                        parsed_line_cmd = "DD"
                    elif set(alpha_token) == {"R"} and len(alpha_token) >= 2:
                        parsed_line_cmd = "RR"
                    elif len(alpha_token) == 1 and alpha_token in {"I", "D", "R", "C", "A", "B"}:
                        parsed_line_cmd = alpha_token
                    else:
                        invalid_line_cmd = alpha_token
                        break

                if parsed_line_cmd:
                    line_cmds.append((row_idx, parsed_line_cmd))

                text_seed = current_line[: layout.dataset_edit_text_width]
                merged_text = _overlay_field(text_start, layout.dataset_edit_text_width, text_seed)
                text_touched = _field_touched(text_start, layout.dataset_edit_text_width)
                edited_rows.append((row_idx, merged_text[: layout.dataset_edit_text_width], text_touched))

            if invalid_line_cmd:
                ds_msg = f"INVALID LINE CMD: {invalid_line_cmd}"
            elif line_cmds:
                cmd_types = {cmd for _, cmd in line_cmds}
                has_copy_cmd = any(cmd in {"C", "CC", "A", "B"} for _, cmd in line_cmds)
                has_direct_cmd = any(cmd in {"I", "D", "R"} for _, cmd in line_cmds)
                has_rr_cmd = any(cmd == "RR" for _, cmd in line_cmds)
                has_dd_cmd = any(cmd == "DD" for _, cmd in line_cmds)

                family_count = int(has_copy_cmd) + int(has_direct_cmd) + int(has_rr_cmd) + int(has_dd_cmd)
                if family_count > 1:
                    ds_msg = "DO NOT MIX COMMAND FAMILIES"
                else:
                    # Apply staged text edits first so R duplicates what
                    # the user currently sees on screen.
                    for idx, text_value, touched in edited_rows:
                        if idx < len(lines):
                            lines[idx] = text_value
                        elif touched:
                            lines.extend([""] * (idx - len(lines) + 1))
                            lines[idx] = text_value

                    if has_copy_cmd:
                        c_rows = sorted({idx for idx, cmd in line_cmds if cmd == "C"})
                        cc_rows = sorted({idx for idx, cmd in line_cmds if cmd == "CC"})
                        target_rows = [(idx, cmd) for idx, cmd in line_cmds if cmd in {"A", "B"}]

                        if len(c_rows) > 1:
                            ds_msg = "ENTER ONLY ONE C LINE COMMAND"
                        elif len(cc_rows) > 2:
                            ds_msg = "ENTER NO MORE THAN TWO CC COMMANDS"
                        elif len(target_rows) > 1:
                            ds_msg = "ENTER ONLY ONE A OR B TARGET"
                        elif c_rows and cc_rows:
                            ds_msg = "USE C OR CC, NOT BOTH"
                        else:
                            if c_rows:
                                pending_copy_source = c_rows[0]
                                pending_copy_block_start = None
                                pending_copy_block_range = None

                            if cc_rows:
                                pending_copy_source = None
                                if len(cc_rows) == 2:
                                    pending_copy_block_range = (cc_rows[0], cc_rows[1])
                                    pending_copy_block_start = None
                                else:
                                    if pending_copy_block_start is None:
                                        pending_copy_block_start = cc_rows[0]
                                        pending_copy_block_range = None
                                    else:
                                        pending_copy_block_range = (
                                            min(pending_copy_block_start, cc_rows[0]),
                                            max(pending_copy_block_start, cc_rows[0]),
                                        )
                                        pending_copy_block_start = None

                            if target_rows:
                                target_idx, target_cmd = target_rows[0]
                                if pending_copy_block_range is not None:
                                    block_start, block_end = pending_copy_block_range
                                    if not (0 <= block_start <= block_end < len(lines)):
                                        pending_copy_block_range = None
                                        ds_msg = "CC BLOCK NO LONGER VALID"
                                    else:
                                        block = lines[block_start:block_end + 1]
                                        insert_at = target_idx if target_cmd == "B" else target_idx + 1
                                        insert_at = max(0, min(insert_at, len(lines)))
                                        lines[insert_at:insert_at] = block
                                        pending_copy_block_start = None
                                        pending_copy_block_range = None
                                        ds_msg = f"{len(block)} COPIED"
                                elif pending_copy_source is None:
                                    ds_msg = "ENTER C OR CC BEFORE A OR B"
                                elif not (0 <= pending_copy_source < len(lines)):
                                    pending_copy_source = None
                                    ds_msg = "COPY SOURCE NO LONGER VALID"
                                else:
                                    source_text = lines[pending_copy_source]
                                    insert_at = target_idx if target_cmd == "B" else target_idx + 1
                                    insert_at = max(0, min(insert_at, len(lines)))
                                    lines.insert(insert_at, source_text)
                                    pending_copy_source = None
                                    ds_msg = "1 COPIED"
                            else:
                                if pending_copy_block_range is not None:
                                    ds_msg = "CC BLOCK MARKED - ENTER A OR B"
                                elif pending_copy_block_start is not None:
                                    ds_msg = "CC MARKED - ENTER SECOND CC"
                                else:
                                    ds_msg = "C MARKED - ENTER A OR B"
                    elif has_rr_cmd:
                        rr_rows = sorted({idx for idx, cmd in line_cmds if cmd == "RR"})
                        if len(rr_rows) > 2:
                            ds_msg = "ENTER NO MORE THAN TWO RR COMMANDS"
                        elif len(rr_rows) == 2:
                            rr_start, rr_end = rr_rows[0], rr_rows[1]
                            if 0 <= rr_start <= rr_end < len(lines):
                                block = lines[rr_start:rr_end + 1]
                                lines[rr_end + 1:rr_end + 1] = block
                                ds_msg = f"{len(block)} REPLICATED"
                                pending_rr_start = None
                            else:
                                ds_msg = "RR BLOCK NO LONGER VALID"
                                pending_rr_start = None
                        else:
                            rr_row = rr_rows[0]
                            if pending_rr_start is None:
                                pending_rr_start = rr_row
                                ds_msg = "RR MARKED - ENTER SECOND RR"
                            else:
                                rr_start = min(pending_rr_start, rr_row)
                                rr_end = max(pending_rr_start, rr_row)
                                if 0 <= rr_start <= rr_end < len(lines):
                                    block = lines[rr_start:rr_end + 1]
                                    lines[rr_end + 1:rr_end + 1] = block
                                    ds_msg = f"{len(block)} REPLICATED"
                                else:
                                    ds_msg = "RR BLOCK NO LONGER VALID"
                                pending_rr_start = None
                    elif has_dd_cmd:
                        dd_rows = sorted({idx for idx, cmd in line_cmds if cmd == "DD"})
                        if len(dd_rows) > 2:
                            ds_msg = "ENTER NO MORE THAN TWO DD COMMANDS"
                        elif len(dd_rows) == 2:
                            dd_start, dd_end = dd_rows[0], dd_rows[1]
                            if 0 <= dd_start <= dd_end < len(lines):
                                deleted_count = dd_end - dd_start + 1
                                del lines[dd_start:dd_end + 1]
                                ds_msg = f"{deleted_count} DELETED"
                                pending_dd_start = None
                            else:
                                ds_msg = "DD BLOCK NO LONGER VALID"
                                pending_dd_start = None
                        else:
                            dd_row = dd_rows[0]
                            if pending_dd_start is None:
                                pending_dd_start = dd_row
                                ds_msg = "DD MARKED - ENTER SECOND DD"
                            else:
                                dd_start = min(pending_dd_start, dd_row)
                                dd_end = max(pending_dd_start, dd_row)
                                if 0 <= dd_start <= dd_end < len(lines):
                                    deleted_count = dd_end - dd_start + 1
                                    del lines[dd_start:dd_end + 1]
                                    ds_msg = f"{deleted_count} DELETED"
                                else:
                                    ds_msg = "DD BLOCK NO LONGER VALID"
                                pending_dd_start = None
                    else:
                        if len(cmd_types) > 1:
                            ds_msg = "USE ONE LINE CMD TYPE AT A TIME"
                        else:
                            cmd_type = next(iter(cmd_types))
                            if cmd_type == "D":
                                deletes = sorted({idx for idx, _ in line_cmds}, reverse=True)
                                deleted_count = 0
                                for idx in deletes:
                                    if 0 <= idx < len(lines):
                                        del lines[idx]
                                        deleted_count += 1
                                ds_msg = f"{deleted_count} DELETED"
                            elif cmd_type == "I":
                                inserts = sorted(idx for idx, _ in line_cmds)
                                inserted_count = 0
                                for idx in inserts:
                                    insert_at = max(0, min(idx, len(lines)))
                                    lines.insert(insert_at, "")
                                    inserted_count += 1
                                ds_msg = f"{inserted_count} INSERTED"
                            else:  # R
                                replicates = sorted({idx for idx, _ in line_cmds}, reverse=True)
                                replicated_count = 0
                                for idx in replicates:
                                    if 0 <= idx < len(lines):
                                        lines.insert(idx + 1, lines[idx])
                                        replicated_count += 1
                                ds_msg = f"{replicated_count} REPLICATED"
                            pending_copy_source = None
                            pending_copy_block_start = None
                            pending_copy_block_range = None
                            pending_rr_start = None
                            pending_dd_start = None

                    max_start = max(0, len(lines) - records_per_page)
                    page = min(page, max_start)
            else:
                for idx, text_value, touched in edited_rows:
                    if idx < len(lines):
                        lines[idx] = text_value
                    elif touched:
                        lines.extend([""] * (idx - len(lines) + 1))
                        lines[idx] = text_value
                ds_msg = "CHANGES STAGED - PF3 TO SAVE"
            ds_cmd = ""
        elif ds_aid_str == "Enter" and ds_cmd:
            ds_msg = f"UNKNOWN COMMAND: {ds_cmd}"
            ds_cmd = ""
        else:
            ds_msg = "USE PF7/PF8 TO SCROLL, PF3 TO EXIT"
            ds_cmd = ""

    return UtilityResult(message=return_msg)


def edit_dataset_by_name(client_socket, actions: UtilityActions, layout: UtilityLayout, dsn_input: str) -> UtilityResult:
    dsn = actions.normalize_dsn(dsn_input)
    if not dsn:
        return UtilityResult(message="ENTER A DATA SET NAME")

    catalog = actions.load_catalog()
    selected = None
    for entry in catalog:
        if actions.normalize_dsn(entry.get("dsn", "")) == dsn:
            selected = entry
            break

    if selected is None:
        return UtilityResult(message=f"DATA SET NOT FOUND: {dsn}")

    return _run_dataset_editor_session(
        client_socket=client_socket,
        actions=actions,
        layout=layout,
        selected=selected,
        selected_cmd="E",
    )


def handle_dslist(client_socket, actions: UtilityActions, layout: UtilityLayout) -> UtilityResult:
    dslist_level = ""
    dslist_rows = []
    dslist_msg = None
    dslist_scroll = "PAGE"
    dslist_show_cols = False
    dslist_hex_mode = False
    catalog = actions.load_catalog()

    while True:
        actions.send_ispf_dslist(
            client_socket,
            level=dslist_level,
            rows=dslist_rows,
            short_msg=dslist_msg,
        )
        dl_result = actions.read_client_input(client_socket)
        if dl_result is None:
            return UtilityResult(message=None, disconnect=True)

        dl_aid, dl_cursor_addr, dl_fields = dl_result
        print(f"AID={hex(dl_aid)}, fields={dl_fields}")

        dl_aid_str = actions.aid_to_string(dl_aid)
        dl_entered = dl_fields.get(layout.dslist_level_addr, "").strip().upper()

        jump_option = _extract_jump_option(dl_entered)
        if jump_option:
            return UtilityResult(message=None, jump_option=jump_option)

        if dl_entered == "X" or dl_aid_str in ("PF3", "PF15"):
            return UtilityResult(message=None)

        selected = None
        selected_cmd = None
        cmd_count = 0
        for i, ds in enumerate(dslist_rows[: layout.dslist_results_max_rows]):
            cmd_addr = (layout.dslist_results_first_row + i) * 80 + (layout.dslist_cmd_sf_col + 1)
            cmd = dl_fields.get(cmd_addr, "").strip().upper()
            if cmd:
                cmd_count += 1
                if cmd in {"B", "V", "E"} and selected is None:
                    selected = ds
                    selected_cmd = cmd
                elif cmd not in {"B", "V", "E"}:
                    dslist_msg = f"INVALID LINE CMD: {cmd} (USE B, V, OR E)"
                    selected = None
                    selected_cmd = None
                    break

        if selected is not None:
            if cmd_count > 1:
                dslist_msg = "ENTER ONLY ONE LINE COMMAND"
                continue

            if actions.is_pds_like(selected):
                launched = _run_pds_member_list_session(
                    client_socket=client_socket,
                    actions=actions,
                    layout=layout,
                    pds_entry=selected,
                    default_cmd=selected_cmd,
                )
                if launched.disconnect:
                    return launched
                if launched.jump_option:
                    return launched
                dslist_msg = launched.message
                continue

            lines, load_error = actions.load_dataset_lines(selected)
            if load_error:
                dslist_msg = load_error
                continue

            dsn = actions.normalize_dsn(selected.get("dsn", ""))
            page = 0
            ds_msg = None
            ds_cmd = ""
            pending_copy_source = None
            pending_copy_block_start = None
            pending_copy_block_range = None
            pending_rr_start = None
            pending_dd_start = None
            while True:
                line_cmd_overrides = {}
                if pending_copy_source is not None:
                    line_cmd_overrides[pending_copy_source] = "C"
                if pending_copy_block_start is not None:
                    line_cmd_overrides[pending_copy_block_start] = "CC"
                if pending_copy_block_range is not None:
                    cc_start, cc_end = pending_copy_block_range
                    line_cmd_overrides[cc_start] = "CC"
                    line_cmd_overrides[cc_end] = "CC"
                if pending_rr_start is not None:
                    line_cmd_overrides[pending_rr_start] = "RR"
                if pending_dd_start is not None:
                    line_cmd_overrides[pending_dd_start] = "DD"

                actions.send_dataset_panel(
                    client_socket,
                    dsn=dsn,
                    mode=selected_cmd,
                    lines=lines,
                    page=page,
                    command=ds_cmd,
                    scroll=dslist_scroll,
                    show_cols=dslist_show_cols,
                    hex_mode=dslist_hex_mode,
                    lrecl=selected.get("lrecl", layout.dataset_line_width),
                    short_msg=ds_msg,
                    line_cmd_overrides=line_cmd_overrides,
                )
                ds_result = actions.read_client_input(client_socket)
                if ds_result is None:
                    return UtilityResult(message=None, disconnect=True)

                ds_aid, ds_cursor_addr, ds_fields = ds_result
                ds_aid_str = actions.aid_to_string(ds_aid)
                ds_cmd = ds_fields.get(layout.dataset_cmd_addr, "").strip().upper()
                ds_scroll = ds_fields.get(layout.dataset_scroll_addr, "").strip().upper()

                jump_option = _extract_jump_option(ds_cmd)
                if jump_option:
                    return UtilityResult(message=None, jump_option=jump_option)

                # Some emulators can report a non-Enter AID even when editable
                # data/line-command fields changed. If we detect edit-row
                # changes, treat it as Enter so line commands execute reliably.
                if selected_cmd == "E" and ds_aid_str != "Enter":
                    data_row_start, rows_per_record, records_per_page = _dataset_display_geometry(
                        layout,
                        dslist_show_cols,
                        dslist_hex_mode,
                    )
                    if _has_edit_row_input(
                        ds_fields,
                        data_row_start,
                        rows_per_record,
                        records_per_page,
                        dslist_hex_mode,
                        layout,
                    ):
                        ds_aid_str = "Enter"

                if ds_scroll in {"PAGE", "CSR"}:
                    if ds_scroll != dslist_scroll:
                        dslist_scroll = ds_scroll
                elif ds_scroll and not ds_cmd:
                    ds_cmd = ds_scroll

                if ds_scroll in {"PAGE", "CSR"} and not ds_cmd:
                    # Do not short-circuit Enter here. The scroll field is
                    # prefilled (PAGE/CSR), and users may have entered row
                    # commands or data edits without a command-line value.
                    ds_msg = None
                    ds_cmd = ""

                cmd_parts = ds_cmd.split()
                cmd_root = cmd_parts[0] if cmd_parts else ""

                if cmd_root == "COLS":
                    if len(cmd_parts) == 1:
                        dslist_show_cols = not dslist_show_cols
                    elif len(cmd_parts) > 1 and cmd_parts[1] in {"ON", "1"}:
                        dslist_show_cols = True
                    elif len(cmd_parts) > 1 and cmd_parts[1] in {"OFF", "0"}:
                        dslist_show_cols = False
                    else:
                        ds_msg = f"UNKNOWN COMMAND: {ds_cmd}"
                        ds_cmd = ""
                        continue
                    ds_msg = f"COLS {'ON' if dslist_show_cols else 'OFF'}"
                    ds_cmd = ""
                    continue

                if cmd_root == "HEX":
                    if len(cmd_parts) == 1:
                        dslist_hex_mode = not dslist_hex_mode
                    elif len(cmd_parts) > 1 and cmd_parts[1] in {"ON", "1"}:
                        dslist_hex_mode = True
                    elif len(cmd_parts) > 1 and cmd_parts[1] in {"OFF", "0"}:
                        dslist_hex_mode = False
                    else:
                        ds_msg = f"UNKNOWN COMMAND: {ds_cmd}"
                        ds_cmd = ""
                        continue
                    ds_msg = f"HEX {'ON' if dslist_hex_mode else 'OFF'}"
                    ds_cmd = ""
                    continue

                if cmd_root == "SCROLL" and len(cmd_parts) == 2 and cmd_parts[1] in {"PAGE", "CSR"}:
                    dslist_scroll = cmd_parts[1]
                    ds_msg = None
                    ds_cmd = ""
                    continue

                if ds_cmd in {"PAGE", "CSR"}:
                    dslist_scroll = ds_cmd
                    ds_msg = None
                    ds_cmd = ""
                    continue

                if ds_aid_str == "Enter" and ds_cmd in {"X", "END", "CANCEL", "EXIT"}:
                    if selected_cmd == "E":
                        save_error = actions.save_dataset_lines(selected, lines)
                        if save_error:
                            ds_msg = save_error
                            continue
                        dslist_msg = f"{dsn} SAVED"
                    else:
                        dslist_msg = None
                    break

                if ds_aid_str == "Enter" and ds_cmd in {
                    "UP",
                    "DOWN",
                    "SCROLL UP",
                    "SCROLL DOWN",
                    "S",
                    "S UP",
                    "S DOWN",
                }:
                    data_row_start, rows_per_record, records_per_page = _dataset_display_geometry(
                        layout,
                        dslist_show_cols,
                        dslist_hex_mode,
                    )
                    scroll_amount = _dataset_scroll_amount(
                        dslist_scroll,
                        ds_cursor_addr,
                        data_row_start,
                        rows_per_record,
                        records_per_page,
                    )
                    max_page = max(0, len(lines) - records_per_page)
                    if "DOWN" in ds_cmd or ds_cmd.endswith("D"):
                        page = min(max_page, page + scroll_amount)
                    else:
                        page = max(0, page - scroll_amount)
                    ds_msg = None
                    ds_cmd = ""
                    continue

                if ds_aid_str in ("PF7",):
                    data_row_start, rows_per_record, records_per_page = _dataset_display_geometry(
                        layout,
                        dslist_show_cols,
                        dslist_hex_mode,
                    )
                    scroll_amount = _dataset_scroll_amount(
                        dslist_scroll,
                        ds_cursor_addr,
                        data_row_start,
                        rows_per_record,
                        records_per_page,
                    )
                    page = max(0, page - scroll_amount)
                    ds_msg = None
                    ds_cmd = ""
                    continue
                if ds_aid_str in ("PF8",):
                    data_row_start, rows_per_record, records_per_page = _dataset_display_geometry(
                        layout,
                        dslist_show_cols,
                        dslist_hex_mode,
                    )
                    scroll_amount = _dataset_scroll_amount(
                        dslist_scroll,
                        ds_cursor_addr,
                        data_row_start,
                        rows_per_record,
                        records_per_page,
                    )
                    max_page = max(0, len(lines) - records_per_page)
                    page = min(max_page, page + scroll_amount)
                    ds_msg = None
                    ds_cmd = ""
                    continue
                if ds_aid_str in ("PF3", "PF15"):
                    if selected_cmd == "E":
                        save_error = actions.save_dataset_lines(selected, lines)
                        if save_error:
                            ds_msg = save_error
                            continue
                        dslist_msg = f"{dsn} SAVED"
                    else:
                        dslist_msg = None
                    break

                if selected_cmd == "E" and ds_aid_str == "Enter":
                    def _overlay_field(start_addr: int, width: int, seed: str) -> str:
                        merged = list((seed or "").ljust(width)[:width])
                        end_addr = start_addr + width
                        for addr, value in sorted(ds_fields.items(), key=lambda item: item[0] if isinstance(item[0], int) else -1):
                            if not isinstance(addr, int):
                                continue
                            fragment = str(value)
                            frag_start = addr
                            frag_end = addr + len(fragment)
                            if frag_end <= start_addr or frag_start >= end_addr:
                                continue

                            overlap_start = max(start_addr, frag_start)
                            overlap_end = min(end_addr, frag_end)
                            src_offset = overlap_start - frag_start
                            dst_offset = overlap_start - start_addr
                            segment = fragment[src_offset : src_offset + (overlap_end - overlap_start)]
                            for j, ch in enumerate(segment):
                                merged[dst_offset + j] = ch

                        return "".join(merged)

                    def _field_touched(start_addr: int, width: int) -> bool:
                        end_addr = start_addr + width
                        for addr, value in ds_fields.items():
                            if not isinstance(addr, int):
                                continue
                            fragment = str(value)
                            if not fragment:
                                continue
                            frag_start = addr
                            frag_end = addr + len(fragment)
                            if frag_end <= start_addr or frag_start >= end_addr:
                                continue
                            return True
                        return False

                    data_row_start, rows_per_record, records_per_page = _dataset_display_geometry(
                        layout,
                        dslist_show_cols,
                        dslist_hex_mode,
                    )
                    start_idx = page
                    prefix_width = 6
                    line_cmds = []
                    invalid_line_cmd = None
                    edited_rows = []
                    for i in range(records_per_page):
                        row = data_row_start + (i * rows_per_record)
                        if dslist_hex_mode:
                            row += 3

                        row_idx = start_idx + i
                        prefix_start = row * 80
                        text_start = row * 80 + (layout.dataset_edit_text_sf_col + 1)

                        current_line = lines[row_idx] if row_idx < len(lines) else ""
                        default_seq = f"{row_idx + 1:06d}"
                        prefix_seed = default_seq
                        if pending_copy_source == row_idx:
                            prefix_seed = "C"
                        # Some emulators/reporting paths can shift modified
                        # field addresses by one column around col 1. Parse
                        # both alignments and choose the stronger signal.
                        raw_prefix0 = _overlay_field(prefix_start, prefix_width, prefix_seed)
                        raw_prefix1 = _overlay_field(prefix_start + 1, prefix_width, prefix_seed)
                        cmd_scan0 = raw_prefix0.upper()
                        cmd_scan1 = raw_prefix1.upper()
                        prefix_touched0 = _field_touched(prefix_start, prefix_width)
                        prefix_touched1 = _field_touched(prefix_start + 1, prefix_width)

                        # Parse command letters from the reconstructed 6-char
                        # prefix value for deterministic behavior across
                        # emulator fragment/update differences.
                        alpha_token0 = "".join(ch for ch in cmd_scan0 if ch.isalpha())
                        alpha_token1 = "".join(ch for ch in cmd_scan1 if ch.isalpha())
                        use_alt = prefix_touched1 and len(alpha_token1) > len(alpha_token0)
                        cmd_scan = cmd_scan1 if use_alt else cmd_scan0
                        raw_prefix = raw_prefix1 if use_alt else raw_prefix0
                        alpha_token = alpha_token1 if use_alt else alpha_token0
                        prefix_touched = prefix_touched0 or prefix_touched1
                        parsed_line_cmd = None

                        if alpha_token:
                            if set(alpha_token) == {"C"} and len(alpha_token) >= 2:
                                parsed_line_cmd = "CC"
                            elif set(alpha_token) == {"D"} and len(alpha_token) >= 2:
                                parsed_line_cmd = "DD"
                            elif set(alpha_token) == {"R"} and len(alpha_token) >= 2:
                                parsed_line_cmd = "RR"
                            elif len(alpha_token) == 1 and alpha_token in {"I", "D", "R", "C", "A", "B"}:
                                parsed_line_cmd = alpha_token
                            else:
                                invalid_line_cmd = alpha_token
                                break

                        if parsed_line_cmd:
                            line_cmds.append((row_idx, parsed_line_cmd))

                        text_seed = current_line[: layout.dataset_edit_text_width]
                        merged_text = _overlay_field(text_start, layout.dataset_edit_text_width, text_seed)
                        text_touched = _field_touched(text_start, layout.dataset_edit_text_width)
                        edited_rows.append((row_idx, merged_text[: layout.dataset_edit_text_width], text_touched))

                    if invalid_line_cmd:
                        ds_msg = f"INVALID LINE CMD: {invalid_line_cmd}"
                    elif line_cmds:
                        cmd_types = {cmd for _, cmd in line_cmds}
                        has_copy_cmd = any(cmd in {"C", "CC", "A", "B"} for _, cmd in line_cmds)
                        has_direct_cmd = any(cmd in {"I", "D", "R"} for _, cmd in line_cmds)
                        has_rr_cmd = any(cmd == "RR" for _, cmd in line_cmds)
                        has_dd_cmd = any(cmd == "DD" for _, cmd in line_cmds)

                        family_count = int(has_copy_cmd) + int(has_direct_cmd) + int(has_rr_cmd) + int(has_dd_cmd)
                        if family_count > 1:
                            ds_msg = "DO NOT MIX COMMAND FAMILIES"
                        else:
                            # Apply staged text edits first so R duplicates what
                            # the user currently sees on screen.
                            for idx, text_value, touched in edited_rows:
                                if idx < len(lines):
                                    lines[idx] = text_value
                                elif touched:
                                    lines.extend([""] * (idx - len(lines) + 1))
                                    lines[idx] = text_value

                            if has_copy_cmd:
                                c_rows = sorted({idx for idx, cmd in line_cmds if cmd == "C"})
                                cc_rows = sorted({idx for idx, cmd in line_cmds if cmd == "CC"})
                                target_rows = [(idx, cmd) for idx, cmd in line_cmds if cmd in {"A", "B"}]

                                if len(c_rows) > 1:
                                    ds_msg = "ENTER ONLY ONE C LINE COMMAND"
                                elif len(cc_rows) > 2:
                                    ds_msg = "ENTER NO MORE THAN TWO CC COMMANDS"
                                elif len(target_rows) > 1:
                                    ds_msg = "ENTER ONLY ONE A OR B TARGET"
                                elif c_rows and cc_rows:
                                    ds_msg = "USE C OR CC, NOT BOTH"
                                else:
                                    if c_rows:
                                        pending_copy_source = c_rows[0]
                                        pending_copy_block_start = None
                                        pending_copy_block_range = None

                                    if cc_rows:
                                        pending_copy_source = None
                                        if len(cc_rows) == 2:
                                            pending_copy_block_range = (cc_rows[0], cc_rows[1])
                                            pending_copy_block_start = None
                                        else:
                                            if pending_copy_block_start is None:
                                                pending_copy_block_start = cc_rows[0]
                                                pending_copy_block_range = None
                                            else:
                                                pending_copy_block_range = (
                                                    min(pending_copy_block_start, cc_rows[0]),
                                                    max(pending_copy_block_start, cc_rows[0]),
                                                )
                                                pending_copy_block_start = None

                                    if target_rows:
                                        target_idx, target_cmd = target_rows[0]
                                        if pending_copy_block_range is not None:
                                            block_start, block_end = pending_copy_block_range
                                            if not (0 <= block_start <= block_end < len(lines)):
                                                pending_copy_block_range = None
                                                ds_msg = "CC BLOCK NO LONGER VALID"
                                            else:
                                                block = lines[block_start:block_end + 1]
                                                insert_at = target_idx if target_cmd == "B" else target_idx + 1
                                                insert_at = max(0, min(insert_at, len(lines)))
                                                lines[insert_at:insert_at] = block
                                                pending_copy_block_start = None
                                                pending_copy_block_range = None
                                                ds_msg = f"{len(block)} COPIED"
                                        elif pending_copy_source is None:
                                            ds_msg = "ENTER C OR CC BEFORE A OR B"
                                        elif not (0 <= pending_copy_source < len(lines)):
                                            pending_copy_source = None
                                            ds_msg = "COPY SOURCE NO LONGER VALID"
                                        else:
                                            source_text = lines[pending_copy_source]
                                            insert_at = target_idx if target_cmd == "B" else target_idx + 1
                                            insert_at = max(0, min(insert_at, len(lines)))
                                            lines.insert(insert_at, source_text)
                                            pending_copy_source = None
                                            ds_msg = "1 COPIED"
                                    else:
                                        if pending_copy_block_range is not None:
                                            ds_msg = "CC BLOCK MARKED - ENTER A OR B"
                                        elif pending_copy_block_start is not None:
                                            ds_msg = "CC MARKED - ENTER SECOND CC"
                                        else:
                                            ds_msg = "C MARKED - ENTER A OR B"
                            elif has_rr_cmd:
                                rr_rows = sorted({idx for idx, cmd in line_cmds if cmd == "RR"})
                                if len(rr_rows) > 2:
                                    ds_msg = "ENTER NO MORE THAN TWO RR COMMANDS"
                                elif len(rr_rows) == 2:
                                    rr_start, rr_end = rr_rows[0], rr_rows[1]
                                    if 0 <= rr_start <= rr_end < len(lines):
                                        block = lines[rr_start:rr_end + 1]
                                        lines[rr_end + 1:rr_end + 1] = block
                                        ds_msg = f"{len(block)} REPLICATED"
                                        pending_rr_start = None
                                    else:
                                        ds_msg = "RR BLOCK NO LONGER VALID"
                                        pending_rr_start = None
                                else:
                                    rr_row = rr_rows[0]
                                    if pending_rr_start is None:
                                        pending_rr_start = rr_row
                                        ds_msg = "RR MARKED - ENTER SECOND RR"
                                    else:
                                        rr_start = min(pending_rr_start, rr_row)
                                        rr_end = max(pending_rr_start, rr_row)
                                        if 0 <= rr_start <= rr_end < len(lines):
                                            block = lines[rr_start:rr_end + 1]
                                            lines[rr_end + 1:rr_end + 1] = block
                                            ds_msg = f"{len(block)} REPLICATED"
                                        else:
                                            ds_msg = "RR BLOCK NO LONGER VALID"
                                        pending_rr_start = None
                            elif has_dd_cmd:
                                dd_rows = sorted({idx for idx, cmd in line_cmds if cmd == "DD"})
                                if len(dd_rows) > 2:
                                    ds_msg = "ENTER NO MORE THAN TWO DD COMMANDS"
                                elif len(dd_rows) == 2:
                                    dd_start, dd_end = dd_rows[0], dd_rows[1]
                                    if 0 <= dd_start <= dd_end < len(lines):
                                        deleted_count = dd_end - dd_start + 1
                                        del lines[dd_start:dd_end + 1]
                                        ds_msg = f"{deleted_count} DELETED"
                                        pending_dd_start = None
                                    else:
                                        ds_msg = "DD BLOCK NO LONGER VALID"
                                        pending_dd_start = None
                                else:
                                    dd_row = dd_rows[0]
                                    if pending_dd_start is None:
                                        pending_dd_start = dd_row
                                        ds_msg = "DD MARKED - ENTER SECOND DD"
                                    else:
                                        dd_start = min(pending_dd_start, dd_row)
                                        dd_end = max(pending_dd_start, dd_row)
                                        if 0 <= dd_start <= dd_end < len(lines):
                                            deleted_count = dd_end - dd_start + 1
                                            del lines[dd_start:dd_end + 1]
                                            ds_msg = f"{deleted_count} DELETED"
                                        else:
                                            ds_msg = "DD BLOCK NO LONGER VALID"
                                        pending_dd_start = None
                            else:
                                if len(cmd_types) > 1:
                                    ds_msg = "USE ONE LINE CMD TYPE AT A TIME"
                                else:
                                    cmd_type = next(iter(cmd_types))
                                    if cmd_type == "D":
                                        deletes = sorted({idx for idx, _ in line_cmds}, reverse=True)
                                        deleted_count = 0
                                        for idx in deletes:
                                            if 0 <= idx < len(lines):
                                                del lines[idx]
                                                deleted_count += 1
                                        ds_msg = f"{deleted_count} DELETED"
                                    elif cmd_type == "I":
                                        inserts = sorted(idx for idx, _ in line_cmds)
                                        inserted_count = 0
                                        for idx in inserts:
                                            insert_at = max(0, min(idx, len(lines)))
                                            lines.insert(insert_at, "")
                                            inserted_count += 1
                                        ds_msg = f"{inserted_count} INSERTED"
                                    else:  # R
                                        replicates = sorted({idx for idx, _ in line_cmds}, reverse=True)
                                        replicated_count = 0
                                        for idx in replicates:
                                            if 0 <= idx < len(lines):
                                                lines.insert(idx + 1, lines[idx])
                                                replicated_count += 1
                                        ds_msg = f"{replicated_count} REPLICATED"
                                    pending_copy_source = None
                                    pending_copy_block_start = None
                                    pending_copy_block_range = None
                                    pending_rr_start = None
                                    pending_dd_start = None

                            max_start = max(0, len(lines) - records_per_page)
                            page = min(page, max_start)
                    else:
                        for idx, text_value, touched in edited_rows:
                            if idx < len(lines):
                                lines[idx] = text_value
                            elif touched:
                                lines.extend([""] * (idx - len(lines) + 1))
                                lines[idx] = text_value
                        ds_msg = "CHANGES STAGED - PF3 TO SAVE"
                    ds_cmd = ""
                elif ds_aid_str == "Enter" and ds_cmd:
                    ds_msg = f"UNKNOWN COMMAND: {ds_cmd}"
                    ds_cmd = ""
                else:
                    ds_msg = "USE PF7/PF8 TO SCROLL, PF3 TO EXIT"
                    ds_cmd = ""
            continue

        if not dl_entered:
            dslist_msg = "ENTER DSNAME LEVEL PATTERN (EX: IBMUSER.*)"
            continue

        dslist_level = dl_entered
        dslist_rows = actions.search_catalog(catalog, dslist_level)
        if dslist_rows:
            dslist_msg = f"{len(dslist_rows)} DATA SET(S) LISTED"
        else:
            dslist_msg = f"NO DATA SETS FOUND FOR {dslist_level}"
