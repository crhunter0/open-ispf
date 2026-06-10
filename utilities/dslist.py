from typing import Optional

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
                dslist_msg = "PDS SUPPORT NOT IMPLEMENTED YET"
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
            while True:
                line_cmd_overrides = {}
                if pending_copy_source is not None:
                    line_cmd_overrides[pending_copy_source] = "C"

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
                        for addr, value in ds_fields.items():
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
                        raw_prefix = _overlay_field(prefix_start, prefix_width, prefix_seed)
                        cmd_value = raw_prefix.upper()

                        # Prefix field doubles as editable sequence area. Accept
                        # I/D/R typed in any position of the 6-char field while
                        # still rejecting ambiguous alpha content.
                        normalized = cmd_value.strip()
                        alpha_chars = [ch for ch in normalized if ch.isalpha()]
                        parsed_line_cmd = None

                        if normalized and normalized != prefix_seed.strip().upper():
                            if not alpha_chars:
                                # User edited only sequence digits.
                                parsed_line_cmd = None
                            elif len(alpha_chars) == 1 and alpha_chars[0] in {"I", "D", "R", "C", "A", "B"}:
                                parsed_line_cmd = alpha_chars[0]
                            else:
                                invalid_line_cmd = normalized
                                break

                        if parsed_line_cmd:
                            line_cmds.append((row_idx, parsed_line_cmd))

                        text_seed = current_line[: layout.dataset_edit_text_width]
                        merged_text = _overlay_field(text_start, layout.dataset_edit_text_width, text_seed)
                        edited_rows.append((row_idx, merged_text[: layout.dataset_edit_text_width]))

                    if invalid_line_cmd:
                        ds_msg = f"INVALID LINE CMD: {invalid_line_cmd}"
                    elif line_cmds:
                        cmd_types = {cmd for _, cmd in line_cmds}
                        has_copy_cmd = any(cmd in {"C", "A", "B"} for _, cmd in line_cmds)
                        has_direct_cmd = any(cmd in {"I", "D", "R"} for _, cmd in line_cmds)

                        if has_copy_cmd and has_direct_cmd:
                            ds_msg = "DO NOT MIX C/A/B WITH I/D/R"
                        else:
                            # Apply staged text edits first so R duplicates what
                            # the user currently sees on screen.
                            for idx, text_value in edited_rows:
                                if idx >= len(lines):
                                    lines.extend([""] * (idx - len(lines) + 1))
                                lines[idx] = text_value

                            if has_copy_cmd:
                                c_rows = sorted({idx for idx, cmd in line_cmds if cmd == "C"})
                                target_rows = [(idx, cmd) for idx, cmd in line_cmds if cmd in {"A", "B"}]

                                if len(c_rows) > 1:
                                    ds_msg = "ENTER ONLY ONE C LINE COMMAND"
                                elif len(target_rows) > 1:
                                    ds_msg = "ENTER ONLY ONE A OR B TARGET"
                                else:
                                    if c_rows:
                                        pending_copy_source = c_rows[0]

                                    if target_rows:
                                        target_idx, target_cmd = target_rows[0]
                                        if pending_copy_source is None:
                                            ds_msg = "ENTER C BEFORE A OR B"
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
                                        ds_msg = "C MARKED - ENTER A OR B"
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

                            max_start = max(0, len(lines) - records_per_page)
                            page = min(page, max_start)
                    else:
                        for idx, text_value in edited_rows:
                            if idx >= len(lines):
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
