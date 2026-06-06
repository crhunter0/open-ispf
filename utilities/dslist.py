from utilities.base import UtilityActions, UtilityLayout, UtilityResult


def handle_dslist(client_socket, actions: UtilityActions, layout: UtilityLayout) -> UtilityResult:
    dslist_level = ""
    dslist_rows = []
    dslist_msg = None
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

        dl_aid, dl_fields = dl_result
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
                    short_msg=ds_msg,
                )
                ds_result = actions.read_client_input(client_socket)
                if ds_result is None:
                    return UtilityResult(message=None, disconnect=True)

                ds_aid, ds_fields = ds_result
                ds_aid_str = actions.aid_to_string(ds_aid)
                ds_cmd = ds_fields.get(layout.dataset_cmd_addr, "").strip().upper()

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
                    if "DOWN" in ds_cmd or ds_cmd.endswith("D"):
                        max_page = max(0, (len(lines) - 1) // layout.dataset_lines_max_rows)
                        page = min(max_page, page + 1)
                    else:
                        page = max(0, page - 1)
                    ds_msg = None
                    ds_cmd = ""
                    continue

                if ds_aid_str in ("PF7",):
                    page = max(0, page - 1)
                    ds_msg = None
                    ds_cmd = ""
                    continue
                if ds_aid_str in ("PF8",):
                    max_page = max(0, (len(lines) - 1) // layout.dataset_lines_max_rows)
                    page = min(max_page, page + 1)
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
                    start_idx = page * layout.dataset_lines_max_rows
                    for i in range(layout.dataset_lines_max_rows):
                        addr = (layout.dataset_lines_first_row + i) * 80 + (layout.dataset_line_sf_col + 1)
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
