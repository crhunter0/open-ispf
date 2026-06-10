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
            while True:
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
                    ds_msg = None
                    ds_cmd = ""
                    continue

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
                    max_page = max(0, (len(lines) - 1) // records_per_page)
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
                    max_page = max(0, (len(lines) - 1) // records_per_page)
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
                    data_row_start, rows_per_record, records_per_page = _dataset_display_geometry(
                        layout,
                        dslist_show_cols,
                        dslist_hex_mode,
                    )
                    start_idx = page * records_per_page
                    for i in range(records_per_page):
                        row = data_row_start + (i * rows_per_record)
                        if dslist_hex_mode:
                            row += 3
                        addr = row * 80 + layout.dataset_line_sf_col
                        if addr in ds_fields:
                            idx = start_idx + i
                            if idx >= len(lines):
                                lines.extend([""] * (idx - len(lines) + 1))
                            lines[idx] = ds_fields.get(addr, "")[: layout.dataset_line_width]
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
