import socket
import binascii
import json
import fnmatch
import argparse
from pathlib import Path
from datetime import datetime
from enum import Enum
from ispf_utility_handlers import UtilityActions, UtilityLayout, handle_utility_option
from utilities.dslist import edit_dataset_by_name
from utilities.jcl_submit import cancel_job, get_job, get_job_sections, list_jobs, purge_job, refresh_job_registry, set_default_submit_owner


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
CONFIG_BASE_DIR = BASE_DIR
DEFAULT_GLOBAL_CONFIG = {
    "catalog_path": "catalog.json",
    "text_encoding": "cp037",
}


def load_global_config(config_file: Path) -> dict:
    if not config_file.exists():
        return dict(DEFAULT_GLOBAL_CONFIG)

    try:
        loaded = json.loads(config_file.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            print("Config load warning: config.json root must be an object; using defaults")
            return dict(DEFAULT_GLOBAL_CONFIG)
    except Exception as e:
        print(f"Config load error: {e}; using defaults")
        return dict(DEFAULT_GLOBAL_CONFIG)

    merged = dict(DEFAULT_GLOBAL_CONFIG)
    merged.update(loaded)
    return merged


def _resolve_config_path(path_value: str, default_name: str) -> Path:
    cleaned = (path_value or "").strip()
    if not cleaned:
        cleaned = default_name
    candidate = Path(cleaned)
    if not candidate.is_absolute():
        candidate = CONFIG_BASE_DIR / candidate
    return candidate


def _configure_runtime(config_override: str = "") -> None:
    global CONFIG_FILE
    global CONFIG_BASE_DIR
    global GLOBAL_CONFIG
    global STANDARD_TEXT_CCSID
    global CATALOG_FILE

    override = str(config_override or "").strip()
    if override:
        candidate = Path(override)
        if not candidate.is_absolute():
            candidate = BASE_DIR / candidate
        CONFIG_FILE = candidate
    else:
        CONFIG_FILE = BASE_DIR / "config.json"

    CONFIG_BASE_DIR = CONFIG_FILE.parent
    GLOBAL_CONFIG = load_global_config(CONFIG_FILE)
    STANDARD_TEXT_CCSID = str(GLOBAL_CONFIG.get("text_encoding", "cp037"))
    CATALOG_FILE = _resolve_config_path(str(GLOBAL_CONFIG.get("catalog_path", "catalog.json")), "catalog.json")


def to_ebcdic(s: str) -> bytes:
    return s.encode(STANDARD_TEXT_CCSID)


def encode_pack_addr(row: int, col: int, cols=80) -> bytes:
    """Encodes a 12-bit 3270 presentation space address from row/col"""
    addr = row * cols + col
    if addr < 0 or addr >= 0x1000:
        raise ValueError("Address out of range")
    hi_chunk = (addr >> 6) & 0b0011_1111
    lo_chunk = addr & 0b0011_1111
    hi = hi_chunk | 0b1100_0000
    lo = lo_chunk | 0b0100_0000
    return bytes([hi, lo])


def write_control_character(
    reset_mdts: bool = True,
    sound_alarm: bool = False,
    keyboard_restore: bool = False,
    start_printer: bool = False,
) -> bytes:
    # WCC bit layout per x3270/wc3270 source (3270ds.h):
    #   0x40 = WCC_RESET_BIT      (always set for normal SNA/LU2 writes)
    #   0x08 = WCC_START_PRINTER_BIT
    #   0x04 = WCC_SOUND_ALARM_BIT
    #   0x02 = WCC_KEYBOARD_RESTORE_BIT  ← unlocks keyboard after AID
    #   0x01 = WCC_RESET_MDT_BIT         ← clears all MDT flags
    wcc = 0x40  # WCC_RESET_BIT: always include for LU2 mode
    if reset_mdts:
        wcc |= 0x01
    if sound_alarm:
        wcc |= 0x04
    if start_printer:
        wcc |= 0x08
    if keyboard_restore:
        wcc |= 0x02
    return bytes([wcc])


class DisplayIntensity(Enum):
    NORMAL = 0
    HIGH = 1
    HIGHLIGHTED = 2
    NON_DISPLAY = 3


class FieldType(Enum):
    ALPHANUMERIC = 0
    NUMERIC = 1


def field_attribute(
    display: DisplayIntensity = DisplayIntensity.NORMAL,
    protected: bool = True,
    field_type: FieldType = FieldType.ALPHANUMERIC,
    mdt: bool = False,
) -> int:
    attr = 0x00
    if display == DisplayIntensity.HIGH:
        attr |= 0b0100_0000
    elif display == DisplayIntensity.HIGHLIGHTED:
        attr |= 0b1000_0000
    elif display == DisplayIntensity.NON_DISPLAY:
        attr |= 0b1100_0000
    if protected:
        attr |= 0b0010_0000
    if field_type == FieldType.NUMERIC:
        attr |= 0b0001_0000
    if mdt:
        attr |= 0b0000_0001
    return attr


IAC = 0xFF
EOR = 0xEF
SBA = 0x11
SF = 0x1D
IC = 0x13


def _sba(buf: bytearray, row: int, col: int):
    buf.append(SBA)
    buf.extend(encode_pack_addr(row, col))


def _sba_sf(
    buf: bytearray,
    row: int,
    col: int,
    protected: bool = True,
    display: DisplayIntensity = DisplayIntensity.NORMAL,
    field_type: FieldType = FieldType.ALPHANUMERIC,
    mdt: bool = False,
):
    buf.append(SBA)
    buf.extend(encode_pack_addr(row, col))
    buf.append(SF)
    buf.append(field_attribute(display=display, protected=protected, field_type=field_type, mdt=mdt))


def _text(buf: bytearray, s: str):
    buf.extend(to_ebcdic(s))


def _high(buf: bytearray, row: int, col: int, s: str):
    """Write high-intensity protected text at position."""
    _sba_sf(buf, row, col, protected=True, display=DisplayIntensity.HIGH)
    _text(buf, s)


def _normal(buf: bytearray, row: int, col: int, s: str):
    """Write normal-intensity protected text at position."""
    _sba_sf(buf, row, col, protected=True, display=DisplayIntensity.NORMAL)
    _text(buf, s)


# Credentials — keys are uppercase userids
_CREDENTIALS = {
    "GP5CRH": "TSYS",
    "TESTUSER": "RACF",
}
# Passwords are stored and compared uppercase (default RACF behavior without MIXEDCASE option)

# Field addresses (row * 80 + col_after_sf) for fields the server reads back
# TSO logon panel: input fields start at col 17, SF is at col 16
LOGON_USERID_SF_COL = 16
LOGON_USERID_ROW = 5
LOGON_PASSWORD_SF_COL = 16
LOGON_PASSWORD_ROW = 6
LOGON_PROC_SF_COL = 16
LOGON_PROC_ROW = 7

LOGON_USERID_ADDR = LOGON_USERID_ROW * 80 + (LOGON_USERID_SF_COL + 1)
LOGON_PASSWORD_ADDR = LOGON_PASSWORD_ROW * 80 + (LOGON_PASSWORD_SF_COL + 1)
LOGON_PROC_ADDR = LOGON_PROC_ROW * 80 + (LOGON_PROC_SF_COL + 1)

# ISPF menu: Option ===> input SF at col 13, data at col 14
ISPF_OPTION_SF_COL = 13
ISPF_OPTION_ROW = 2
ISPF_OPTION_ADDR = ISPF_OPTION_ROW * 80 + (ISPF_OPTION_SF_COL + 1)

# Utility 3.2 Data Set panel fields
DSUTIL_OPTION_ROW = 2
DSUTIL_OPTION_SF_COL = 13
DSUTIL_OPTION_WIDTH = 64
DSUTIL_OPTION_ADDR = DSUTIL_OPTION_ROW * 80 + (DSUTIL_OPTION_SF_COL + 1)
DSUTIL_DSN_ROW = 4
DSUTIL_DSN_SF_COL = 19
DSUTIL_DSN_ADDR = DSUTIL_DSN_ROW * 80 + (DSUTIL_DSN_SF_COL + 1)
DSUTIL_NEW_DSN_ROW = 5
DSUTIL_NEW_DSN_SF_COL = 19
DSUTIL_NEW_DSN_ADDR = DSUTIL_NEW_DSN_ROW * 80 + (DSUTIL_NEW_DSN_SF_COL + 1)
DSUTIL_TYPE_ROW = 6
DSUTIL_TYPE_SF_COL = 19
DSUTIL_TYPE_ADDR = DSUTIL_TYPE_ROW * 80 + (DSUTIL_TYPE_SF_COL + 1)

# Utility 3.3 Move/Copy panel fields
MOVECOPY_OPTION_ROW = 2
MOVECOPY_OPTION_SF_COL = 13
MOVECOPY_OPTION_ADDR = MOVECOPY_OPTION_ROW * 80 + (MOVECOPY_OPTION_SF_COL + 1)
MOVECOPY_FROM_DSN_ROW = 9
MOVECOPY_FROM_DSN_SF_COL = 19
MOVECOPY_FROM_DSN_ADDR = MOVECOPY_FROM_DSN_ROW * 80 + (MOVECOPY_FROM_DSN_SF_COL + 1)
MOVECOPY_FROM_MEMBER_ROW = 10
MOVECOPY_FROM_MEMBER_SF_COL = 19
MOVECOPY_FROM_MEMBER_ADDR = MOVECOPY_FROM_MEMBER_ROW * 80 + (MOVECOPY_FROM_MEMBER_SF_COL + 1)
MOVECOPY_TO_DSN_ROW = 12
MOVECOPY_TO_DSN_SF_COL = 19
MOVECOPY_TO_DSN_ADDR = MOVECOPY_TO_DSN_ROW * 80 + (MOVECOPY_TO_DSN_SF_COL + 1)
MOVECOPY_TO_MEMBER_ROW = 13
MOVECOPY_TO_MEMBER_SF_COL = 19
MOVECOPY_TO_MEMBER_ADDR = MOVECOPY_TO_MEMBER_ROW * 80 + (MOVECOPY_TO_MEMBER_SF_COL + 1)

# Utility 3.5 JCL Submit panel fields
JCLSUB_OPTION_ROW = 2
JCLSUB_OPTION_SF_COL = 13
JCLSUB_OPTION_WIDTH = 64
JCLSUB_OPTION_ADDR = JCLSUB_OPTION_ROW * 80 + (JCLSUB_OPTION_SF_COL + 1)
JCLSUB_DSN_ROW = 5
JCLSUB_DSN_SF_COL = 19
JCLSUB_DSN_ADDR = JCLSUB_DSN_ROW * 80 + (JCLSUB_DSN_SF_COL + 1)
JCLSUB_MEMBER_ROW = 6
JCLSUB_MEMBER_SF_COL = 19
JCLSUB_MEMBER_ADDR = JCLSUB_MEMBER_ROW * 80 + (JCLSUB_MEMBER_SF_COL + 1)

# SDSF panel fields
SDSF_CMD_ROW = 1
SDSF_CMD_SF_COL = 13
SDSF_CMD_WIDTH = 64
SDSF_CMD_ADDR = SDSF_CMD_ROW * 80 + (SDSF_CMD_SF_COL + 1)
SDSF_PRE_ROW = 2
SDSF_PRE_SF_COL = 13
SDSF_PRE_WIDTH = 16
SDSF_PRE_ADDR = SDSF_PRE_ROW * 80 + (SDSF_PRE_SF_COL + 1)
SDSF_OWNER_ROW = 2
SDSF_OWNER_SF_COL = 43
SDSF_OWNER_WIDTH = 12
SDSF_OWNER_ADDR = SDSF_OWNER_ROW * 80 + (SDSF_OWNER_SF_COL + 1)
SDSF_RESULTS_FIRST_ROW = 6
SDSF_RESULTS_MAX_ROWS = 14
SDSF_LINE_CMD_SF_COL = 1

# ISPF option 2 (Edit) entry panel fields
EDIT_DSN_ROW = 5
EDIT_DSN_SF_COL = 19
EDIT_DSN_ADDR = EDIT_DSN_ROW * 80 + (EDIT_DSN_SF_COL + 1)

# DSLIST panel: "Dsname Level ===>" input field at row 2, SF at col 19
DSLIST_CMD_ROW = 1
DSLIST_CMD_SF_COL = 13
DSLIST_CMD_WIDTH = 64
DSLIST_CMD_ADDR = DSLIST_CMD_ROW * 80 + (DSLIST_CMD_SF_COL + 1)
DSLIST_LEVEL_ROW = 2
DSLIST_LEVEL_SF_COL = 19
DSLIST_LEVEL_ADDR = DSLIST_LEVEL_ROW * 80 + (DSLIST_LEVEL_SF_COL + 1)  # = 180
DSLIST_RESULTS_FIRST_ROW = 7
DSLIST_RESULTS_MAX_ROWS = 14
DSLIST_LINE_CMD_SF_COL = 1

# Dataset Browse/View/Edit panel layout
DATASET_SCROLL_ROW = 2
DATASET_SCROLL_SF_COL = 60
DATASET_SCROLL_ADDR = DATASET_SCROLL_ROW * 80 + (DATASET_SCROLL_SF_COL + 1)
DATASET_CMD_ROW = 2
DATASET_CMD_SF_COL = 13
DATASET_CMD_ADDR = DATASET_CMD_ROW * 80 + (DATASET_CMD_SF_COL + 1)
DATASET_LINES_FIRST_ROW = 3
DATASET_LINES_MAX_ROWS = 18
DATASET_LINE_SF_COL = 0
DATASET_LINE_WIDTH = 78
DATASET_EDIT_CMD_SF_COL = 0
DATASET_EDIT_CMD_WIDTH = 6
DATASET_EDIT_LINE_NO_COL = 0
DATASET_EDIT_LINE_NO_WIDTH = 6
DATASET_EDIT_TEXT_SF_COL = 6
DATASET_EDIT_TEXT_WIDTH = 72

# Catalog — maps mainframe DSN to local file metadata.
# Dataset bytes are always stored raw; no whole-dataset transcoding is performed.
# CP037 is used only at UI/terminal text field boundaries.
GLOBAL_CONFIG = {}
STANDARD_TEXT_CCSID = "cp037"
CATALOG_FILE = BASE_DIR / "catalog.json"
_configure_runtime("")


def _normalize_dsn(dsn: str) -> str:
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


def save_catalog(entries: list) -> str:
    try:
        payload = {"datasets": entries}
        CATALOG_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return None
    except Exception as e:
        return f"CATALOG SAVE FAILED: {e}"


def search_catalog(entries: list, pattern: str) -> list:
    patt = _normalize_dsn(pattern)
    if not patt:
        return []
    out = [ds for ds in entries
           if fnmatch.fnmatchcase(_normalize_dsn(ds.get("dsn", "")), patt)]
    out.sort(key=lambda d: _normalize_dsn(d.get("dsn", "")))
    return out


def _catalog_entry_path(entry: dict) -> Path:
    return _resolve_config_path(str(entry.get("path", "")).strip(), "")


def _is_pds_like(entry: dict) -> bool:
    return str(entry.get("org", "")).strip().upper() in {"PO", "POE"}


def load_dataset_lines(entry: dict) -> tuple[list, str]:
    file_path = _catalog_entry_path(entry)
    if not file_path.exists():
        dsn = _normalize_dsn(entry.get("dsn", ""))
        return [], f"DATA SET NOT FOUND: {dsn}"
    if not file_path.is_file():
        dsn = _normalize_dsn(entry.get("dsn", ""))
        return [], f"CATALOG PATH IS NOT A FILE: {dsn}"

    try:
        raw = file_path.read_bytes()
    except Exception as e:
        return [], f"UNABLE TO READ DATA SET: {e}"

    if str(entry.get("content_mode", "text")).strip().lower() == "binary":
        return [], "BINARY DATA SET NOT SUPPORTED YET"

    text_ccsid = str(entry.get("text_ccsid", STANDARD_TEXT_CCSID)).strip() or STANDARD_TEXT_CCSID
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

    file_path = _catalog_entry_path(entry)
    if not file_path.exists():
        return "DATA SET FILE NOT FOUND"

    text_ccsid = str(entry.get("text_ccsid", STANDARD_TEXT_CCSID)).strip() or STANDARD_TEXT_CCSID
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


def _dataset_cols_ruler(width: int = DATASET_LINE_WIDTH) -> str:
    ruler = []
    for offset in range(1, width + 1):
        if offset % 10 == 0:
            ruler.append(str((offset // 10) % 10))
        elif offset % 5 == 0:
            ruler.append("+")
        else:
            ruler.append("-")
    return "".join(ruler)


def _dataset_hex_rows(text: str, width: int = DATASET_LINE_WIDTH) -> tuple[str, str]:
    encoded = text[:width].ljust(width).encode(
        STANDARD_TEXT_CCSID, errors="replace"
    )
    high_row = []
    low_row = []
    for byte in encoded[:width]:
        hex_byte = f"{byte:02X}"
        high_row.append(hex_byte[0])
        low_row.append(hex_byte[1])
    return "".join(high_row), "".join(low_row)


def _dataset_banner(label: str, width: int = DATASET_LINE_WIDTH) -> str:
    inner = f" {label} "
    star_count = max(2, width - len(inner))
    left = star_count // 2
    right = star_count - left
    return f"{'*' * left}{inner}{'*' * right}"[:width]


def send_dataset_panel(
    client_socket,
    dsn: str,
    mode: str,
    lines: list,
    page: int,
    command: str = "",
    scroll: str = "PAGE",
    show_cols: bool = False,
    hex_mode: bool = False,
    lrecl: int = DATASET_LINE_WIDTH,
    short_msg: str = None,
    line_cmd_overrides: dict = None,
):
    """Send a simple ISPF-like dataset Browse/View/Edit panel for sequential datasets."""
    buf = bytearray()
    buf.append(0xF5)  # ERASE_WRITE
    buf.extend(write_control_character(reset_mdts=True, keyboard_restore=True))

    mode = mode.upper()
    line_cmd_overrides = line_cmd_overrides or {}
    mode_label = {"B": "BROWSE", "V": "VIEW", "E": "EDIT"}.get(mode, mode)
    inner = f" Data Set {mode_label} "
    pad = (79 - len(inner)) // 2
    border = "-" * pad + inner + "-" * (79 - pad - len(inner))
    _high(buf, 0, 0, border)

    panel_first_row = DATASET_LINES_FIRST_ROW + (1 if show_cols else 0)
    content_first_row = panel_first_row + 1
    content_rows = max(1, DATASET_LINES_MAX_ROWS - 2)
    rows_per_record = 4 if hex_mode else 1
    records_per_page = max(1, content_rows // rows_per_record)
    max_start = max(0, len(lines) - records_per_page)
    page = max(0, min(page, max_start))
    start = page
    edit_text_width = DATASET_EDIT_TEXT_WIDTH if mode == "E" else DATASET_LINE_WIDTH
    display_lrecl = max(
        1,
        min(edit_text_width, int(lrecl) if str(lrecl).isdigit() else edit_text_width),
    )

    _normal(
        buf,
        1,
        1,
        f"{mode_label:<6} {dsn[:28]:<28} Line {start + 1:>6} of {max(1, len(lines)):<6} Col 001 of {display_lrecl:03}",
    )
    _normal(buf, DATASET_SCROLL_ROW, 50, "Scroll ===>")
    _sba(buf, DATASET_SCROLL_ROW, DATASET_SCROLL_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{scroll[:4].upper():<4}")
    _sba_sf(buf, DATASET_SCROLL_ROW, DATASET_SCROLL_SF_COL + 5, protected=True)

    _normal(buf, DATASET_CMD_ROW, 1, "Command ===>")
    _sba(buf, DATASET_CMD_ROW, DATASET_CMD_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{command[:8]:<8}")
    _sba_sf(buf, DATASET_CMD_ROW, DATASET_CMD_SF_COL + 9, protected=True)

    if show_cols:
        if mode == "E":
            _normal(
                buf,
                DATASET_LINES_FIRST_ROW,
                DATASET_LINE_SF_COL,
                f"{'':<7}{_dataset_cols_ruler(DATASET_EDIT_TEXT_WIDTH)}",
            )
        else:
            _normal(buf, DATASET_LINES_FIRST_ROW, DATASET_LINE_SF_COL, _dataset_cols_ruler(DATASET_LINE_WIDTH))

    _normal(buf, panel_first_row, DATASET_LINE_SF_COL, _dataset_banner("TOP OF DATA"))
    _normal(
        buf,
        panel_first_row + DATASET_LINES_MAX_ROWS - 1,
        DATASET_LINE_SF_COL,
        _dataset_banner("BOTTOM OF DATA"),
    )

    for i in range(records_per_page):
        row = content_first_row + (i * rows_per_record)
        text = ""
        if start + i < len(lines):
            text = lines[start + i][:edit_text_width]

        if hex_mode:
            if mode == "E":
                _normal(
                    buf,
                    row,
                    DATASET_LINE_SF_COL,
                    f"{(start + i + 1):06d} {'-' * display_lrecl:<{DATASET_EDIT_TEXT_WIDTH}}",
                )
                hex_high, hex_low = _dataset_hex_rows(text, DATASET_EDIT_TEXT_WIDTH)
                _normal(buf, row + 1, DATASET_LINE_SF_COL, f"{'':<7}{hex_high:<{DATASET_EDIT_TEXT_WIDTH}}")
                _normal(buf, row + 2, DATASET_LINE_SF_COL, f"{'':<7}{hex_low:<{DATASET_EDIT_TEXT_WIDTH}}")
            else:
                _normal(buf, row, DATASET_LINE_SF_COL, f"{'-' * display_lrecl:<{DATASET_LINE_WIDTH}}")
                hex_high, hex_low = _dataset_hex_rows(text, DATASET_LINE_WIDTH)
                _normal(buf, row + 1, DATASET_LINE_SF_COL, f"{hex_high:<{DATASET_LINE_WIDTH}}")
                _normal(buf, row + 2, DATASET_LINE_SF_COL, f"{hex_low:<{DATASET_LINE_WIDTH}}")
            data_row = row + 3
        else:
            data_row = row

        if mode == "E":
            line_no = start + i + 1
            prefix = line_cmd_overrides.get(start + i, f"{line_no:06d}")
            prefix = str(prefix).upper().ljust(DATASET_EDIT_CMD_WIDTH)[:DATASET_EDIT_CMD_WIDTH]

            # Editable sequence number field that begins in display column 1.
            # In 3270, SF occupies one cell and data starts at the next column,
            # so anchor SF at previous row col 79 to make text begin at row col 0.
            _sba(buf, data_row - 1, 79)
            buf.append(SF)
            buf.append(field_attribute(protected=False, mdt=True))
            _text(buf, prefix)

            # Editable text field.
            _sba(buf, data_row, DATASET_EDIT_TEXT_SF_COL)
            buf.append(SF)
            buf.append(field_attribute(protected=False, mdt=True))
            _text(buf, f"{text:<{DATASET_EDIT_TEXT_WIDTH}}")
            _sba_sf(
                buf,
                data_row,
                DATASET_EDIT_TEXT_SF_COL + DATASET_EDIT_TEXT_WIDTH + 1,
                protected=True,
            )
        else:
            _normal(buf, data_row, DATASET_LINE_SF_COL, f"{text:<{DATASET_LINE_WIDTH}}")

    if mode == "E":
        buf.append(SBA)
        is_member_edit = "(" in dsn and ")" in dsn
        if is_member_edit:
            # For member edits, default to command line for consistency with ISPF workflows.
            buf.extend(encode_pack_addr(DATASET_CMD_ROW, DATASET_CMD_SF_COL + 1))
        else:
            cursor_row = content_first_row + (3 if hex_mode else 0)
            # Start cursor in the 6-char prefix command area (col 1) so line
            # commands are entered reliably without extra cursor movement.
            buf.extend(encode_pack_addr(cursor_row, DATASET_EDIT_LINE_NO_COL))
        buf.append(IC)
    else:
        buf.append(SBA)
        buf.extend(encode_pack_addr(DATASET_CMD_ROW, DATASET_CMD_SF_COL + 1))
        buf.append(IC)

    footer_text = (
        "Commands: X=Exit COLS HEX SCROLL PAGE/CSR  Line cmds: I/D/DD/R/RR/C/CC/A/B PF3=Save PF7/PF8"
        if mode == "E"
        else "Commands: X=Exit COLS HEX SCROLL PAGE/CSR  PF3=End  PF7=Up  PF8=Down"
    )

    if short_msg:
        _high(buf, 21, 1, short_msg[:78])

    _normal(buf, 22, 1, footer_text[:79])
    _high(buf, 23, 0, "-" * 79)
    buf.extend([IAC, EOR])
    print("TX:", binascii.hexlify(buf))
    client_socket.sendall(buf)


def send_tso_logon(client_socket, error_msg: str = None):
    """Send authentic z/OS TSO/E LOGON panel."""
    buf = bytearray()
    buf.append(0xF5)  # ERASE_WRITE
    buf.extend(write_control_character(reset_mdts=True, keyboard_restore=True))

    # Row 0: title bar (centered in 80 cols)
    title = "-" * 8 + "  z/OS V2R5.0 TSO/E LOGON  " + "-" * 8
    title_col = (80 - len(title)) // 2
    _high(buf, 0, title_col, title)

    # Row 2: column headers
    _normal(buf, 2, 1, "Enter LOGON parameters below:")
    _normal(buf, 2, 42, "RACF LOGON parameters:")

    # Row 3: separator line
    _normal(buf, 3, 1, "-" * 37)
    _normal(buf, 3, 42, "-" * 37)

    # Row 5: Userid
    _normal(buf, 5, 1, "Userid   ===>")
    _sba_sf(buf, 5, LOGON_USERID_SF_COL, protected=False, mdt=True)
    _text(buf, " " * 8)
    _sba_sf(buf, 5, LOGON_USERID_SF_COL + 9, protected=True)  # field terminator

    # Insert cursor in userid field
    buf.append(SBA)
    buf.extend(encode_pack_addr(5, LOGON_USERID_SF_COL + 1))
    buf.append(IC)

    # Row 6: Password
    _normal(buf, 6, 1, "Password ===>")
    _sba_sf(buf, 6, LOGON_PASSWORD_SF_COL, protected=False, display=DisplayIntensity.NON_DISPLAY, mdt=True)
    _text(buf, " " * 8)
    _sba_sf(buf, 6, LOGON_PASSWORD_SF_COL + 9, protected=True)

    # Row 7: Procedure
    _normal(buf, 7, 1, "Procedure===>")
    _sba_sf(buf, 7, LOGON_PROC_SF_COL, protected=False, mdt=True)
    _text(buf, "IKJACCNT")
    _sba_sf(buf, 7, LOGON_PROC_SF_COL + 9, protected=True)

    _normal(buf, 7, 42, "Acct Nmbr    ===>")
    _sba_sf(buf, 7, 60, protected=False, mdt=True)
    _text(buf, " " * 8)
    _sba_sf(buf, 7, 69, protected=True)

    # Row 8: Size
    _normal(buf, 8, 1, "Size     ===>")
    _sba_sf(buf, 8, 16, protected=False, field_type=FieldType.NUMERIC, mdt=True)
    _text(buf, "00150")
    _sba_sf(buf, 8, 22, protected=True)

    _normal(buf, 8, 42, "Perform      ===>")
    _sba_sf(buf, 8, 60, protected=False, field_type=FieldType.NUMERIC, mdt=True)
    _text(buf, " " * 8)
    _sba_sf(buf, 8, 69, protected=True)

    # Row 9: Command
    _normal(buf, 9, 1, "Command  ===>")
    _sba_sf(buf, 9, 16, protected=False, mdt=True)
    _text(buf, " " * 62)
    _sba_sf(buf, 9, 79, protected=True)

    # Row 11: Reconnect (right column)
    _normal(buf, 11, 1, "PDS/E Dsname ===>")
    _sba_sf(buf, 11, 19, protected=False, mdt=True)
    _text(buf, " " * 59)
    _sba_sf(buf, 11, 79, protected=True)

    # Row 12: Mail notify
    _normal(buf, 12, 42, "Mail      ===> Yes")
    _normal(buf, 13, 42, "Reconnect ===> Auto")

    # Row 14: Sysout class
    _normal(buf, 14, 42, "OIDcard   ===> None")

    # Row 16: Enter/PF key hints
    _normal(buf, 16, 1, "Press ENTER to logon to TSO/E")
    _normal(buf, 17, 1, "PF1=HELP   PF3=LOGOFF")

    # Row 19: error message (high intensity, centered)
    if error_msg:
        err_col = max(0, (80 - len(error_msg)) // 2)
        _high(buf, 19, err_col, error_msg)

    # Row 21: bottom message
    _normal(buf, 21, 1, "ENTER AN END COMMAND TO LOGOFF")

    buf.extend([IAC, EOR])
    print("TX:", binascii.hexlify(buf))
    client_socket.sendall(buf)


_ISPF_OPTIONS = [
    ("0", "Settings      ", "Terminal and user parameters"),
    ("1", "View          ", "Display source data or listings"),
    ("2", "Edit          ", "Create or change source data"),
    ("3", "Utilities     ", "Perform utility functions"),
    ("4", "Foreground    ", "Interactive language processing"),
    ("5", "Batch         ", "Submit job for language processing"),
    ("6", "Command       ", "Enter TSO or Workstation commands"),
    ("7", "Dialog Test   ", "Perform dialog testing"),
    ("S", "SDSF          ", "System Display and Search Facility"),
    ("9", "IBM Products  ", "IBM program development products"),
    ("10", "SCLM          ", "SW Configuration Library Manager"),
    ("11", "Workplace     ", "ISPF Object/Action Workplace"),
    ("12", "z/OS System   ", "z/OS system programmer applications"),
    ("13", "z/OS User     ", "z/OS user applications"),
]

_UTILS_OPTIONS = [
    ("1", "Library       ", "Library utility"),
    ("2", "Data Set      ", "Data set utility"),
    ("3", "Move/Copy     ", "Move or copy utility"),
    ("4", "DSLIST        ", "Data set list utility"),
    ("5", "JCL Submit    ", "Submit and run JCL jobs"),
    ("6", "Hardcopy      ", "Hardcopy utility"),
    ("7", "Outlist       ", "Output list utility"),
    ("8", "Catalog       ", "Catalog utility"),
    ("9", "Search-For    ", "Search-for utility"),
    ("10", "Convert       ", "Conversion utility"),
    ("11", "Format        ", "Data set format utility"),
    ("12", "SuperC        ", "Compare utility"),
    ("13", "SuperCE       ", "Extended compare utility"),
    ("14", "Search-For    ", "Batch search utility"),
]


def send_ispf_menu(client_socket, userid: str, short_msg: str = None):
    """Send authentic ISPF Primary Option Menu."""
    buf = bytearray()
    buf.append(0xF5)  # ERASE_WRITE
    buf.extend(write_control_character(reset_mdts=True, keyboard_restore=True))

    now = datetime.now()
    time_str = now.strftime("%H:%M")

    # Row 0: title border (SF at col 0, text fills cols 1-79)
    inner = " ISPF Primary Option Menu "   # 26 chars
    pad = (79 - len(inner)) // 2           # 26 dashes each side
    border = "-" * pad + inner + "-" * (79 - pad - len(inner))
    _high(buf, 0, 0, border)

    # Row 2: Option ===> label + unprotected input field
    _normal(buf, 2, 1, "Option ===>")
    _sba(buf, 2, ISPF_OPTION_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, " " * 6)
    _sba_sf(buf, 2, ISPF_OPTION_SF_COL + 7, protected=True)

    # Position cursor in the option field using a separate SBA+IC after the
    # terminator — same pattern as the working logon panel (IC immediately
    # after an SF attr byte causes wc3270 to show X SYSTEM keyboard lock).
    buf.append(SBA)
    buf.extend(encode_pack_addr(ISPF_OPTION_ROW, ISPF_OPTION_SF_COL + 1))
    buf.append(IC)

    if short_msg:
        _high(buf, 2, 25, short_msg[:54])

    # Rows 4-16: single-column option list (avoids any cross-column text bleed)
    # Col layout: SF+num at col 1, SF+name at col 4, SF+desc at col 21
    for i, (num, name, desc) in enumerate(_ISPF_OPTIONS):
        row = 4 + i
        _sba_sf(buf, row, 1, protected=True, display=DisplayIntensity.HIGH)
        _text(buf, f"{num:<2}")
        _normal(buf, row, 4, f"  {name}")   # name is 14 chars + leading "  " = 16 total
        _normal(buf, row, 21, f"  {desc}")

    # Row 18: X / exit option
    _sba_sf(buf, 18, 1, protected=True, display=DisplayIntensity.HIGH)
    _text(buf, "X ")
    _normal(buf, 18, 4, "  Exit          ")
    _normal(buf, 18, 21, "  Terminate ISPF using log/list defaults")

    # Row 20: PF key hints
    _normal(buf, 20, 1, "Enter X or PF3 to terminate ISPF.")

    # Row 21-22: status block
    _normal(buf, 21, 1, f"User ID . . :  {userid:<8}")
    _normal(buf, 21, 41, f"Time. . . . :  {time_str}")
    _normal(buf, 22, 1, "System ID . :  SY1     ")
    _normal(buf, 22, 41, "ISPF Ver. . :  7.1.0   ")

    # Row 23: bottom border
    _high(buf, 23, 0, "-" * 79)

    buf.extend([IAC, EOR])
    print("TX:", binascii.hexlify(buf))
    client_socket.sendall(buf)


def send_ispf_utils(client_socket, short_msg: str = None):
    """Send ISPF Utilities Selection Panel (option 3)."""
    buf = bytearray()
    buf.append(0xF5)  # ERASE_WRITE
    buf.extend(write_control_character(reset_mdts=True, keyboard_restore=True))

    # Row 0: title border
    inner = " UTILITY SELECTION PANEL "
    pad = (79 - len(inner)) // 2
    border = "-" * pad + inner + "-" * (79 - pad - len(inner))
    _high(buf, 0, 0, border)

    # Row 2: Option ===> label + unprotected input field
    _normal(buf, 2, 1, "Option ===>")
    _sba(buf, 2, ISPF_OPTION_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, " " * 6)
    _sba_sf(buf, 2, ISPF_OPTION_SF_COL + 7, protected=True)

    # Position cursor in option field with SBA+IC after terminator.
    buf.append(SBA)
    buf.extend(encode_pack_addr(ISPF_OPTION_ROW, ISPF_OPTION_SF_COL + 1))
    buf.append(IC)

    if short_msg:
        _high(buf, 2, 25, short_msg[:54])

    # Rows 4-17: utilities list
    for i, (num, name, desc) in enumerate(_UTILS_OPTIONS):
        row = 4 + i
        _sba_sf(buf, row, 1, protected=True, display=DisplayIntensity.HIGH)
        _text(buf, f"{num:<2}")
        _normal(buf, row, 4, f"  {name}")
        _normal(buf, row, 21, f"  {desc}")

    # Row 18: X / exit option
    _sba_sf(buf, 18, 1, protected=True, display=DisplayIntensity.HIGH)
    _text(buf, "X ")
    _normal(buf, 18, 4, "  Exit          ")
    _normal(buf, 18, 21, "  Return to ISPF Primary Option Menu")

    # Row 20: PF key hints
    _normal(buf, 20, 1, "Enter X or PF3 to return to ISPF Primary Option Menu.")

    # Row 23: bottom border
    _high(buf, 23, 0, "-" * 79)

    buf.extend([IAC, EOR])
    print("TX:", binascii.hexlify(buf))
    client_socket.sendall(buf)


def send_ispf_dsutil(
    client_socket,
    option: str = "",
    dsn: str = "",
    new_dsn: str = "",
    dsorg: str = "PS",
    short_msg: str = None,
):
    """Send ISPF 3.2 Data Set Utility panel."""
    buf = bytearray()
    buf.append(0xF5)  # ERASE_WRITE
    buf.extend(write_control_character(reset_mdts=True, keyboard_restore=True))

    inner = " ISPF UTILITIES - DATA SET "
    pad = (79 - len(inner)) // 2
    border = "-" * pad + inner + "-" * (79 - pad - len(inner))
    _high(buf, 0, 0, border)

    _normal(buf, 2, 1, "Option ===>")
    _sba(buf, DSUTIL_OPTION_ROW, DSUTIL_OPTION_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{option[:DSUTIL_OPTION_WIDTH]:<{DSUTIL_OPTION_WIDTH}}")
    _sba_sf(buf, DSUTIL_OPTION_ROW, DSUTIL_OPTION_SF_COL + DSUTIL_OPTION_WIDTH + 1, protected=True)

    _normal(buf, 4, 1, "Data Set Name ===>")
    _sba(buf, DSUTIL_DSN_ROW, DSUTIL_DSN_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{dsn[:44]:<44}")
    _sba_sf(buf, DSUTIL_DSN_ROW, DSUTIL_DSN_SF_COL + 45, protected=True)

    _normal(buf, 5, 1, "New Name     ===>")
    _sba(buf, DSUTIL_NEW_DSN_ROW, DSUTIL_NEW_DSN_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{new_dsn[:44]:<44}")
    _sba_sf(buf, DSUTIL_NEW_DSN_ROW, DSUTIL_NEW_DSN_SF_COL + 45, protected=True)

    _normal(buf, 6, 1, "Data Set Type ===>")
    _sba(buf, DSUTIL_TYPE_ROW, DSUTIL_TYPE_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{dsorg[:2].upper():<2}")
    _sba_sf(buf, DSUTIL_TYPE_ROW, DSUTIL_TYPE_SF_COL + 3, protected=True)

    _normal(buf, 7, 1, "Specify one of the following options:")
    _normal(buf, 9, 3, "A  - Allocate new data set (PS or PO)")
    _normal(buf, 10, 3, "R  - Rename data set")
    _normal(buf, 11, 3, "D  - Delete data set")
    _normal(buf, 12, 3, "C  - Catalog data set (not implemented)")
    _normal(buf, 13, 3, "U  - Uncatalog data set (not implemented)")
    _normal(buf, 14, 3, "I  - Data set information (not implemented)")
    _normal(buf, 15, 3, "M  - Member list (not implemented)")
    _normal(buf, 17, 1, "Enter X or press PF3 to return to Utility Selection Panel")

    if short_msg:
        _high(buf, 19, 1, short_msg[:78])

    buf.append(SBA)
    buf.extend(encode_pack_addr(DSUTIL_OPTION_ROW, DSUTIL_OPTION_SF_COL + 1))
    buf.append(IC)

    _high(buf, 23, 0, "-" * 79)
    buf.extend([IAC, EOR])
    print("TX:", binascii.hexlify(buf))
    client_socket.sendall(buf)


def send_ispf_movecopy(
    client_socket,
    option: str = "C",
    from_dsn: str = "",
    from_member: str = "",
    to_dsn: str = "",
    to_member: str = "",
    short_msg: str = None,
):
    """Send ISPF 3.3 Move/Copy Utility panel."""
    buf = bytearray()
    buf.append(0xF5)  # ERASE_WRITE
    buf.extend(write_control_character(reset_mdts=True, keyboard_restore=True))

    inner = " Move/Copy Utility "
    pad = (79 - len(inner)) // 2
    border = "-" * pad + inner + "-" * (79 - pad - len(inner))
    _high(buf, 0, 0, border)

    _normal(buf, 2, 1, "Option ===>")
    _sba(buf, MOVECOPY_OPTION_ROW, MOVECOPY_OPTION_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{option[:2].upper():<2}")
    _sba_sf(buf, MOVECOPY_OPTION_ROW, MOVECOPY_OPTION_SF_COL + 3, protected=True)

    _normal(buf, 4, 3, "C  Copy data set or member(s)    CP Copy and print")
    _normal(buf, 5, 3, "M  Move data set or member(s)    MP Move and print")

    _normal(buf, 7, 1, "Specify source and target names, then press Enter")

    _normal(buf, 9, 1, "From Data Set Name ===>")
    _sba(buf, MOVECOPY_FROM_DSN_ROW, MOVECOPY_FROM_DSN_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{from_dsn[:44]:<44}")
    _sba_sf(buf, MOVECOPY_FROM_DSN_ROW, MOVECOPY_FROM_DSN_SF_COL + 45, protected=True)

    _normal(buf, 10, 1, "From Member    ===>")
    _sba(buf, MOVECOPY_FROM_MEMBER_ROW, MOVECOPY_FROM_MEMBER_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{from_member[:8]:<8}")
    _sba_sf(buf, MOVECOPY_FROM_MEMBER_ROW, MOVECOPY_FROM_MEMBER_SF_COL + 9, protected=True)

    _normal(buf, 12, 1, "To   Data Set Name ===>")
    _sba(buf, MOVECOPY_TO_DSN_ROW, MOVECOPY_TO_DSN_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{to_dsn[:44]:<44}")
    _sba_sf(buf, MOVECOPY_TO_DSN_ROW, MOVECOPY_TO_DSN_SF_COL + 45, protected=True)

    _normal(buf, 13, 1, "To   Member    ===>")
    _sba(buf, MOVECOPY_TO_MEMBER_ROW, MOVECOPY_TO_MEMBER_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{to_member[:8]:<8}")
    _sba_sf(buf, MOVECOPY_TO_MEMBER_ROW, MOVECOPY_TO_MEMBER_SF_COL + 9, protected=True)

    _normal(buf, 16, 1, "Notes: Use DSN(MEMBER) or Member fields for PDS members")
    _normal(buf, 17, 1, "Target data set must not exist for full data set move/copy")
    _normal(buf, 18, 1, "Enter X or press PF3 to return to Utility Selection Panel")

    if short_msg:
        _high(buf, 3, 1, short_msg[:78])

    buf.append(SBA)
    buf.extend(encode_pack_addr(MOVECOPY_OPTION_ROW, MOVECOPY_OPTION_SF_COL + 1))
    buf.append(IC)

    _high(buf, 23, 0, "-" * 79)
    buf.extend([IAC, EOR])
    print("TX:", binascii.hexlify(buf))
    client_socket.sendall(buf)


def send_ispf_jcl_submit(
    client_socket,
    option: str = "SUBMIT",
    jcl_dsn: str = "",
    jcl_member: str = "",
    short_msg: str = None,
):
    """Send ISPF Utilities JCL Submit panel."""
    buf = bytearray()
    buf.append(0xF5)  # ERASE_WRITE
    buf.extend(write_control_character(reset_mdts=True, keyboard_restore=True))

    inner = " JCL Submit Utility "
    pad = (79 - len(inner)) // 2
    border = "-" * pad + inner + "-" * (79 - pad - len(inner))
    _high(buf, 0, 0, border)

    _normal(buf, 2, 1, "Option ===>")
    _sba(buf, JCLSUB_OPTION_ROW, JCLSUB_OPTION_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{option[:JCLSUB_OPTION_WIDTH]:<{JCLSUB_OPTION_WIDTH}}")
    _sba_sf(buf, JCLSUB_OPTION_ROW, JCLSUB_OPTION_SF_COL + JCLSUB_OPTION_WIDTH + 1, protected=True)

    _normal(buf, 4, 1, "Enter SUBMIT and specify a JCL data set below")

    _normal(buf, 5, 1, "JCL Data Set Name ===>")
    _sba(buf, JCLSUB_DSN_ROW, JCLSUB_DSN_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{jcl_dsn[:44]:<44}")
    _sba_sf(buf, JCLSUB_DSN_ROW, JCLSUB_DSN_SF_COL + 45, protected=True)

    _normal(buf, 6, 1, "Member           ===>")
    _sba(buf, JCLSUB_MEMBER_ROW, JCLSUB_MEMBER_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{jcl_member[:8]:<8}")
    _sba_sf(buf, JCLSUB_MEMBER_ROW, JCLSUB_MEMBER_SF_COL + 9, protected=True)

    _normal(buf, 9, 1, "Notes:")
    _normal(buf, 10, 3, "- For PDS JCL libraries, member is required")
    _normal(buf, 11, 3, "- EXEC PGM resolution searches SYS1.LOADLIB first")
    _normal(buf, 12, 3, "- Job run artifacts are written under job_runs/")
    _normal(buf, 14, 1, "Use = commands to jump from this panel (for example =3.4)")
    _normal(buf, 18, 1, "Enter X or press PF3 to return to Utility Selection Panel")

    if short_msg:
        _high(buf, 3, 1, short_msg[:78])

    buf.append(SBA)
    buf.extend(encode_pack_addr(JCLSUB_OPTION_ROW, JCLSUB_OPTION_SF_COL + 1))
    buf.append(IC)

    _high(buf, 23, 0, "-" * 79)
    buf.extend([IAC, EOR])
    print("TX:", binascii.hexlify(buf))
    client_socket.sendall(buf)


def send_ispf_edit_entry(client_socket, dsn: str = "", short_msg: str = None):
    """Send ISPF Edit Entry Panel (option 2)."""
    buf = bytearray()
    buf.append(0xF5)  # ERASE_WRITE
    buf.extend(write_control_character(reset_mdts=True, keyboard_restore=True))

    inner = " EDIT - ENTRY PANEL "
    pad = (79 - len(inner)) // 2
    border = "-" * pad + inner + "-" * (79 - pad - len(inner))
    _high(buf, 0, 0, border)

    _normal(buf, 2, 1, "Specify a data set name to edit")
    _normal(buf, 3, 1, "(Option 2 routes to the editor with existing line/block commands)")

    _normal(buf, EDIT_DSN_ROW, 1, "Data Set Name ===>")
    _sba(buf, EDIT_DSN_ROW, EDIT_DSN_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{dsn[:44]:<44}")
    _sba_sf(buf, EDIT_DSN_ROW, EDIT_DSN_SF_COL + 45, protected=True)

    buf.append(SBA)
    buf.extend(encode_pack_addr(EDIT_DSN_ROW, EDIT_DSN_SF_COL + 1))
    buf.append(IC)

    if short_msg:
        _high(buf, 7, 1, short_msg[:78])

    _normal(buf, 20, 1, "ENTER to continue, PF3 to return to ISPF Primary Option Menu")
    _high(buf, 23, 0, "-" * 79)

    buf.extend([IAC, EOR])
    print("TX:", binascii.hexlify(buf))
    client_socket.sendall(buf)


def send_ispf_dslist(
    client_socket,
    command: str = "",
    level: str = "",
    rows=None,
    short_msg: str = None,
    footer_hint: str = None,
):
    """Send ISPF 3.4 Data Set List Utility panel."""
    rows = rows or []
    buf = bytearray()
    buf.append(0xF5)  # ERASE_WRITE
    buf.extend(write_control_character(reset_mdts=True, keyboard_restore=True))

    # Row 0: title border
    inner = " Data Set List Utility "
    pad = (79 - len(inner)) // 2
    border = "-" * pad + inner + "-" * (79 - pad - len(inner))
    _high(buf, 0, 0, border)

    # Row 1: command line
    _normal(buf, DSLIST_CMD_ROW, 1, "Command ===>")
    _sba(buf, DSLIST_CMD_ROW, DSLIST_CMD_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{command[:DSLIST_CMD_WIDTH]:<{DSLIST_CMD_WIDTH}}")
    _sba_sf(buf, DSLIST_CMD_ROW, DSLIST_CMD_SF_COL + DSLIST_CMD_WIDTH + 1, protected=True)

    # Row 2: "Dsname Level ===>" label + unprotected input field
    _normal(buf, 2, 1, "Dsname Level ===>")
    _sba(buf, DSLIST_LEVEL_ROW, DSLIST_LEVEL_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{level[:44]:<44}")
    _sba_sf(buf, DSLIST_LEVEL_ROW, DSLIST_LEVEL_SF_COL + 45, protected=True)

    # Position cursor in the command line field
    buf.append(SBA)
    buf.extend(encode_pack_addr(DSLIST_CMD_ROW, DSLIST_CMD_SF_COL + 1))
    buf.append(IC)

    if short_msg:
        _high(buf, 3, 1, short_msg[:78])

    # Row 5-6: column headers
    _normal(buf, 5, 1, "Cmd  Data Set Name                       Org  Recfm Lrecl  Mode  ")
    _normal(buf, 6, 1, "---  -----------------------------------  ---  ----- -----  ------")

    # Rows 7-20: up to 14 result rows with line-command input field
    for i, ds in enumerate(rows[:DSLIST_RESULTS_MAX_ROWS]):
        row = DSLIST_RESULTS_FIRST_ROW + i

        _sba(buf, row, DSLIST_LINE_CMD_SF_COL)
        buf.append(SF)
        buf.append(field_attribute(protected=False, mdt=True))
        _text(buf, " ")
        _sba_sf(buf, row, DSLIST_LINE_CMD_SF_COL + 2, protected=True)

        dsn   = _normalize_dsn(ds.get("dsn", ""))[:35]
        org   = str(ds.get("org",   ""))[:3]
        recfm = str(ds.get("recfm", ""))[:5]
        lrecl = str(ds.get("lrecl", ""))[:5]
        mode  = ds.get("content_mode", "text").upper()[:6]
        # Keep cols 1-3 reserved for line command input and separator.
        _normal(buf, row, 4, f"  {dsn:<35}  {org:<3}  {recfm:<5} {lrecl:>5}  {mode:<6}")

    # Row 22: usage hint; Row 23: bottom border
    hint = footer_hint or "Use Command for jumps/X. Dsname Level is remembered. Line cmd B/V/E/D. PF3=return"
    _normal(buf, 22, 1, hint[:78])
    _high(buf, 23, 0, "-" * 79)

    buf.extend([IAC, EOR])
    print("TX:", binascii.hexlify(buf))
    client_socket.sendall(buf)


def _sdsf_metric_strings(job: dict) -> tuple[str, str, str]:
    metrics = job.get("runtime_metrics", {}) if isinstance(job, dict) else {}
    cpu_val = metrics.get("cpu_percent") if isinstance(metrics, dict) else None
    mem_val = metrics.get("mem_bytes") if isinstance(metrics, dict) else None
    io_r = metrics.get("io_read_bytes") if isinstance(metrics, dict) else None
    io_w = metrics.get("io_write_bytes") if isinstance(metrics, dict) else None

    if cpu_val is None:
        cpu_text = "N/A"
    else:
        cpu_text = f"{float(cpu_val):.1f}"

    if mem_val is None:
        mem_text = "N/A"
    else:
        mem_text = f"{int(mem_val) / (1024 * 1024):.1f}"

    if io_r is None and io_w is None:
        io_text = "N/A"
    else:
        total_io = int(io_r or 0) + int(io_w or 0)
        io_text = f"{total_io // 1024}"

    return cpu_text, mem_text, io_text


def send_ispf_sdsf(
    client_socket,
    userid: str,
    command: str = "ST",
    pre_filter: str = "*",
    owner_filter: str = "",
    rows=None,
    short_msg: str = None,
):
    rows = rows or []
    owner_display = owner_filter or userid

    buf = bytearray()
    buf.append(0xF5)
    buf.extend(write_control_character(reset_mdts=True, keyboard_restore=True))

    inner = " SDSF STATUS DISPLAY "
    pad = (79 - len(inner)) // 2
    border = "-" * pad + inner + "-" * (79 - pad - len(inner))
    _high(buf, 0, 0, border)

    _normal(buf, SDSF_CMD_ROW, 1, "Command ===>")
    _sba(buf, SDSF_CMD_ROW, SDSF_CMD_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{command[:SDSF_CMD_WIDTH]:<{SDSF_CMD_WIDTH}}")
    _sba_sf(buf, SDSF_CMD_ROW, SDSF_CMD_SF_COL + SDSF_CMD_WIDTH + 1, protected=True)

    _normal(buf, SDSF_PRE_ROW, 1, "PRE   ===>")
    _sba(buf, SDSF_PRE_ROW, SDSF_PRE_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{pre_filter[:SDSF_PRE_WIDTH]:<{SDSF_PRE_WIDTH}}")
    _sba_sf(buf, SDSF_PRE_ROW, SDSF_PRE_SF_COL + SDSF_PRE_WIDTH + 1, protected=True)

    _normal(buf, SDSF_OWNER_ROW, 31, "OWNER ===>")
    _sba(buf, SDSF_OWNER_ROW, SDSF_OWNER_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, f"{owner_display[:SDSF_OWNER_WIDTH]:<{SDSF_OWNER_WIDTH}}")
    _sba_sf(buf, SDSF_OWNER_ROW, SDSF_OWNER_SF_COL + SDSF_OWNER_WIDTH + 1, protected=True)

    if short_msg:
        _high(buf, 3, 1, short_msg[:78])

    _normal(buf, 4, 1, "Use ST (all jobs) or DA (active jobs). Line cmds: S detail  ? sections  C cancel  P purge")
    _normal(buf, 5, 1, "Cmd  JobID     JobName  Owner    St       RC   PID     CPU%  MEMMB   IOKB")

    for i, job in enumerate(rows[:SDSF_RESULTS_MAX_ROWS]):
        row = SDSF_RESULTS_FIRST_ROW + i
        _sba(buf, row, SDSF_LINE_CMD_SF_COL)
        buf.append(SF)
        buf.append(field_attribute(protected=False, mdt=True))
        _text(buf, "  ")
        _sba_sf(buf, row, SDSF_LINE_CMD_SF_COL + 3, protected=True)

        cpu_text, mem_text, io_text = _sdsf_metric_strings(job)
        job_id = str(job.get("job_id", ""))[:9]
        job_name = str(job.get("job_name", ""))[:8]
        owner = str(job.get("owner", ""))[:8]
        status = str(job.get("status", ""))[:8]
        rc_val = job.get("return_code")
        rc_text = "----" if rc_val is None else str(rc_val)[:4]
        pid_text = str(job.get("pid", ""))[:7]

        line = f"  {job_id:<9} {job_name:<8} {owner:<8} {status:<8} {rc_text:>4} {pid_text:<7} {cpu_text:>5} {mem_text:>6} {io_text:>6}"
        _normal(buf, row, 4, line[:75])

    _normal(buf, 22, 1, "X/PF3=Exit  PF7/PF8 reserved for viewer panels")
    _high(buf, 23, 0, "-" * 79)

    buf.append(SBA)
    buf.extend(encode_pack_addr(SDSF_CMD_ROW, SDSF_CMD_SF_COL + 1))
    buf.append(IC)
    buf.extend([IAC, EOR])
    print("TX:", binascii.hexlify(buf))
    client_socket.sendall(buf)


def send_ispf_sdsf_sections(client_socket, job_id: str, short_msg: str = None):
    buf = bytearray()
    buf.append(0xF5)
    buf.extend(write_control_character(reset_mdts=True, keyboard_restore=True))

    inner = f" SDSF SECTIONS {job_id[:9]} "
    pad = (79 - len(inner)) // 2
    border = "-" * pad + inner + "-" * (79 - pad - len(inner))
    _high(buf, 0, 0, border)

    _normal(buf, SDSF_CMD_ROW, 1, "Command ===>")
    _sba(buf, SDSF_CMD_ROW, SDSF_CMD_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, " " * SDSF_CMD_WIDTH)
    _sba_sf(buf, SDSF_CMD_ROW, SDSF_CMD_SF_COL + SDSF_CMD_WIDTH + 1, protected=True)

    if short_msg:
        _high(buf, 3, 1, short_msg[:78])

    _normal(buf, 4, 1, "Line cmd S to open a section")
    _normal(buf, 5, 1, "Cmd  Section     Description")
    _normal(buf, 6, 4, "     JESMSG      JES message log")
    _normal(buf, 7, 4, "     JCL         Submitted JCL text")
    _normal(buf, 8, 4, "     SYSOUT      Program standard output")
    _normal(buf, 9, 4, "     SYSERR      Program standard error")
    _normal(buf, 10, 4, "     JOBMETA     Job metadata JSON")

    for row in (6, 7, 8, 9, 10):
        _sba(buf, row, SDSF_LINE_CMD_SF_COL)
        buf.append(SF)
        buf.append(field_attribute(protected=False, mdt=True))
        _text(buf, " ")
        _sba_sf(buf, row, SDSF_LINE_CMD_SF_COL + 2, protected=True)

    _normal(buf, 22, 1, "X/PF3=Back  Enter section name in command line is also supported")
    _high(buf, 23, 0, "-" * 79)

    buf.append(SBA)
    buf.extend(encode_pack_addr(SDSF_CMD_ROW, SDSF_CMD_SF_COL + 1))
    buf.append(IC)
    buf.extend([IAC, EOR])
    print("TX:", binascii.hexlify(buf))
    client_socket.sendall(buf)


def send_ispf_text_viewer(client_socket, title: str, lines: list[str], page: int = 0, short_msg: str = None):
    view_rows = 18
    max_start = max(0, len(lines) - view_rows)
    page = max(0, min(page, max_start))

    buf = bytearray()
    buf.append(0xF5)
    buf.extend(write_control_character(reset_mdts=True, keyboard_restore=True))

    inner = f" {title[:45]} "
    pad = (79 - len(inner)) // 2
    border = "-" * pad + inner + "-" * (79 - pad - len(inner))
    _high(buf, 0, 0, border)

    _normal(buf, SDSF_CMD_ROW, 1, "Command ===>")
    _sba(buf, SDSF_CMD_ROW, SDSF_CMD_SF_COL)
    buf.append(SF)
    buf.append(field_attribute(protected=False, mdt=True))
    _text(buf, " " * SDSF_CMD_WIDTH)
    _sba_sf(buf, SDSF_CMD_ROW, SDSF_CMD_SF_COL + SDSF_CMD_WIDTH + 1, protected=True)

    _normal(buf, 2, 1, f"Line {page + 1} of {max(1, len(lines))}")
    if short_msg:
        _high(buf, 2, 30, short_msg[:48])

    for i in range(view_rows):
        row = 3 + i
        text = ""
        if page + i < len(lines):
            text = lines[page + i]
        _normal(buf, row, 1, f"{text[:78]:<78}")

    _normal(buf, 22, 1, "PF7=Up  PF8=Down  X/PF3=Back")
    _high(buf, 23, 0, "-" * 79)
    buf.append(SBA)
    buf.extend(encode_pack_addr(SDSF_CMD_ROW, SDSF_CMD_SF_COL + 1))
    buf.append(IC)
    buf.extend([IAC, EOR])
    print("TX:", binascii.hexlify(buf))
    client_socket.sendall(buf)


def _sdsf_detail_lines(job: dict) -> list[str]:
    lines = []
    lines.append(f"JOB ID       : {job.get('job_id', '')}")
    lines.append(f"JOB NAME     : {job.get('job_name', '')}")
    lines.append(f"OWNER        : {job.get('owner', '')}")
    lines.append(f"STATUS       : {job.get('status', '')}")
    lines.append(f"RETURN CODE  : {job.get('return_code', '')}")
    lines.append(f"PID / PGID   : {job.get('pid', '')} / {job.get('pgid', '')}")
    lines.append(f"SUBMITTED    : {job.get('submitted_at', '')}")
    lines.append(f"COMPLETED    : {job.get('completed_at', '')}")
    lines.append(f"SOURCE DSN   : {job.get('source_dsn', '')}")
    lines.append(f"SOURCE MEMBER: {job.get('source_member', '')}")
    lines.append(f"RUN DIR      : {job.get('run_dir', '')}")
    lines.append("")

    metrics = job.get("runtime_metrics", {}) if isinstance(job, dict) else {}
    lines.append("RUNTIME METRICS")
    lines.append(f"  CPU %      : {metrics.get('cpu_percent', 'N/A')}")
    lines.append(f"  MEM BYTES  : {metrics.get('mem_bytes', 'N/A')}")
    lines.append(f"  IO READ    : {metrics.get('io_read_bytes', 'N/A')}")
    lines.append(f"  IO WRITE   : {metrics.get('io_write_bytes', 'N/A')}")
    lines.append("")
    lines.append("STEP RESULTS")
    step_results = job.get("step_results", [])
    if step_results:
        for item in step_results:
            lines.append(f"  {item.get('step_name', ''):<8} RC={item.get('rc', '')}")
    else:
        lines.append("  NONE")
    return lines


def _view_text_panel_session(client_socket, title: str, lines: list[str]) -> tuple[bool, str]:
    page = 0
    message = None
    while True:
        send_ispf_text_viewer(client_socket, title=title, lines=lines, page=page, short_msg=message)
        message = None
        result = read_client_input(client_socket)
        if result is None:
            return True, ""

        aid, cursor_addr, fields = result
        aid_str = aid_to_string(aid)
        command = fields.get(SDSF_CMD_ADDR, "").strip().upper()

        if command in {"X", "END", "EXIT", "CANCEL"} or aid_str in ("PF3", "PF15"):
            return False, ""
        if aid_str == "PF7" or command in {"UP", "TOP"}:
            page = 0 if command == "TOP" else max(0, page - 18)
            continue
        if aid_str == "PF8" or command in {"DOWN", "BOT", "BOTTOM"}:
            if command in {"BOT", "BOTTOM"}:
                page = max(0, len(lines) - 18)
            else:
                page = min(max(0, len(lines) - 18), page + 18)
            continue

        if command:
            message = f"UNKNOWN COMMAND: {command}"


def _sdsf_section_session(client_socket, job_id: str) -> tuple[bool, str]:
    section_msg = None
    section_rows = {
        6: "JESMSG",
        7: "JCL",
        8: "SYSOUT",
        9: "SYSERR",
        10: "JOBMETA",
    }

    while True:
        send_ispf_sdsf_sections(client_socket, job_id=job_id, short_msg=section_msg)
        section_msg = None
        result = read_client_input(client_socket)
        if result is None:
            return True, ""

        aid, cursor_addr, fields = result
        aid_str = aid_to_string(aid)
        command = fields.get(SDSF_CMD_ADDR, "").strip().upper()

        if command in {"X", "END", "EXIT", "CANCEL"} or aid_str in ("PF3", "PF15"):
            return False, ""

        target_section = ""
        if command in {"JESMSG", "JCL", "SYSOUT", "SYSERR", "JOBMETA"}:
            target_section = command

        if not target_section:
            for row, name in section_rows.items():
                cmd_addr = row * 80 + (SDSF_LINE_CMD_SF_COL + 1)
                line_cmd = fields.get(cmd_addr, "").strip().upper()
                if line_cmd == "S":
                    target_section = name
                    break
                if line_cmd:
                    section_msg = f"INVALID LINE CMD: {line_cmd}"
                    break

        if not target_section:
            if command:
                section_msg = f"UNKNOWN SECTION: {command}"
            continue

        sections, sec_err = get_job_sections(job_id)
        if sec_err:
            section_msg = sec_err
            continue

        content = sections.get(target_section, "")
        if not content:
            content = f"NO CONTENT FOR {target_section}"

        disconnected, _ = _view_text_panel_session(
            client_socket,
            title=f"{job_id} {target_section}",
            lines=content.splitlines() or [""],
        )
        if disconnected:
            return True, ""


def handle_sdsf_session(client_socket, userid: str) -> tuple[bool, str, str]:
    mode = "ST"
    pre_filter = "*"
    owner_filter = userid
    short_msg = None

    while True:
        refresh_job_registry()
        rows = list_jobs(
            pre_filter=pre_filter,
            owner_filter=owner_filter,
            active_only=(mode == "DA"),
        )
        send_ispf_sdsf(
            client_socket,
            userid=userid,
            command=mode,
            pre_filter=pre_filter,
            owner_filter=owner_filter,
            rows=rows,
            short_msg=short_msg,
        )
        short_msg = None

        result = read_client_input(client_socket)
        if result is None:
            return True, "", ""

        aid, cursor_addr, fields = result
        aid_str = aid_to_string(aid)
        cmd_text = fields.get(SDSF_CMD_ADDR, "").strip().upper()
        entered_pre = fields.get(SDSF_PRE_ADDR, "").strip().upper()
        entered_owner = fields.get(SDSF_OWNER_ADDR, "").strip().upper()

        for text in (cmd_text, entered_pre, entered_owner):
            if text.startswith("=") and len(text) > 1:
                return False, text[1:], ""

        if entered_pre:
            pre_filter = entered_pre
        if entered_owner:
            owner_filter = entered_owner

        if cmd_text in {"X", "END", "EXIT", "CANCEL"} or aid_str in ("PF3", "PF15"):
            return False, "", ""

        if cmd_text:
            parts = cmd_text.split()
            primary = parts[0]
            operand = " ".join(parts[1:]).strip().upper() if len(parts) > 1 else ""
            if primary in {"ST", "DA"}:
                mode = primary
            elif primary == "PRE" and operand:
                pre_filter = operand
            elif primary == "OWNER" and operand:
                owner_filter = operand
            elif primary not in {"PRE", "OWNER"}:
                short_msg = f"UNKNOWN COMMAND: {cmd_text}"
                continue

        selected = None
        selected_cmd = ""
        selected_count = 0
        for i, job in enumerate(rows[:SDSF_RESULTS_MAX_ROWS]):
            cmd_addr = (SDSF_RESULTS_FIRST_ROW + i) * 80 + (SDSF_LINE_CMD_SF_COL + 1)
            line_cmd = fields.get(cmd_addr, "").strip().upper()
            if not line_cmd:
                continue
            selected_count += 1
            if line_cmd in {"S", "?", "C", "P"} and selected is None:
                selected = job
                selected_cmd = line_cmd
            elif line_cmd not in {"S", "?", "C", "P"}:
                short_msg = f"INVALID LINE CMD: {line_cmd}"
                selected = None
                selected_cmd = ""
                break

        if selected_count > 1:
            short_msg = "ENTER ONLY ONE LINE COMMAND"
            continue

        if selected is None:
            continue

        job_id = str(selected.get("job_id", "")).strip().upper()
        if not job_id:
            short_msg = "MISSING JOB ID"
            continue

        if selected_cmd == "C":
            short_msg = cancel_job(job_id)
            continue
        if selected_cmd == "P":
            short_msg = purge_job(job_id)
            continue
        if selected_cmd == "S":
            job, job_err = get_job(job_id)
            if job_err:
                short_msg = job_err
                continue
            disconnected, _ = _view_text_panel_session(
                client_socket,
                title=f"SDSF DETAIL {job_id}",
                lines=_sdsf_detail_lines(job),
            )
            if disconnected:
                return True, "", ""
            continue
        if selected_cmd == "?":
            disconnected, _ = _sdsf_section_session(client_socket, job_id)
            if disconnected:
                return True, "", ""
            continue

    return False, "", ""


def aid_to_string(aid: int):
    aid_codes = {
        0x60: "No AID",
        0x7D: "Enter",
        0x6D: "Clear",
        0x6C: "PA1",
        0x6E: "PA2",
        0x6B: "PA3",
        0xF1: "PF1",
        0xF2: "PF2",
        0xF3: "PF3",
        0xF4: "PF4",
        0xF5: "PF5",
        0xF6: "PF6",
        0xF7: "PF7",
        0xF8: "PF8",
        0xF9: "PF9",
        0x7A: "PF10",
        0x7B: "PF11",
        0x7C: "PF12",
        0xC1: "PF13",
        0xC2: "PF14",
        0xC3: "PF15",
        0xC4: "PF16",
        0xC5: "PF17",
        0xC6: "PF18",
        0xC7: "PF19",
        0xC8: "PF20",
        0xC9: "PF21",
        0x4A: "PF22",
        0x4B: "PF23",
        0x4C: "PF24",
    }
    return aid_codes.get(aid, f"Unknown AID {hex(aid)}")


def read_client_input(client_socket):
    buffer = bytearray()
    while True:
        data = client_socket.recv(1024)
        if not data:
            return None
        buffer.extend(data)
        if len(buffer) >= 2 and buffer[-2:] == bytes([IAC, EOR]):
            break

    print("RX:", binascii.hexlify(buffer))

    # Strip IAC EOR
    buffer = buffer[:-2]

    if not buffer:
        return None
    aid = buffer[0]
    print(f"AID: {aid_to_string(aid)}")

    if len(buffer) < 3:
        return None

    cursor_addr = ((buffer[1] & 0x3F) << 6) | (buffer[2] & 0x3F)
    print(f"Cursor address: {cursor_addr}")

    SBA_ORD = 0x11
    SF_ORD = 0x1D
    results = {}
    i = 3
    while i < len(buffer):
        if buffer[i] == SBA_ORD and i + 2 < len(buffer):
            addr_hi, addr_lo = buffer[i + 1], buffer[i + 2]
            addr = ((addr_hi & 0x3F) << 6) | (addr_lo & 0x3F)
            i += 3
            field_bytes = bytearray()
            while i < len(buffer) and buffer[i] not in (SBA_ORD, SF_ORD):
                field_bytes.append(buffer[i])
                i += 1
            # Preserve leading spaces so partial-field updates keep their
            # original in-field column offsets. This is required for reliable
            # line-command parsing in editable prefixes.
            field_text = field_bytes.decode(STANDARD_TEXT_CCSID).rstrip()
            if field_text:
                results[addr] = field_text
        else:
            i += 1

    return aid, cursor_addr, results


def tn3270_negotiate(client_socket):
    DONT = 254
    DO = 253
    WONT = 252
    WILL = 251
    SB = 250
    SE = 240

    BINARY = 0
    TERMINAL_TYPE = 24
    EOR_OPT = 25

    got_binary = False
    got_eor = False
    got_term = False

    negot = bytearray()
    negot.extend([IAC, WILL, BINARY])
    negot.extend([IAC, DO, BINARY])
    negot.extend([IAC, WILL, EOR_OPT])
    negot.extend([IAC, DO, EOR_OPT])
    negot.extend([IAC, WILL, TERMINAL_TYPE])
    negot.extend([IAC, DO, TERMINAL_TYPE])
    negot.extend([IAC, SB, TERMINAL_TYPE, 1, IAC, SE])

    print("TX:", binascii.hexlify(negot))
    client_socket.sendall(negot)

    buffer = bytearray()
    client_socket.settimeout(60.0)

    while not (got_binary and got_eor and got_term):
        data = client_socket.recv(1024)
        if not data:
            break
        buffer.extend(data)
        print("RX:", binascii.hexlify(data))

        i = 0
        while i < len(buffer):
            if buffer[i] != IAC:
                i += 1
                continue

            if i + 1 >= len(buffer):
                break  # IAC at recv boundary; wait for next recv
            cmd = buffer[i + 1]

            if cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= len(buffer):
                    break  # incomplete 3-byte command; wait for more data
                opt = buffer[i + 2]
                if cmd == DO:
                    client_socket.sendall(bytes([IAC, WILL, opt]))
                elif cmd == DONT:
                    client_socket.sendall(bytes([IAC, WONT, opt]))
                elif cmd == WILL:
                    client_socket.sendall(bytes([IAC, DO, opt]))
                elif cmd == WONT:
                    client_socket.sendall(bytes([IAC, DONT, opt]))

                if opt == BINARY and cmd in (DO, WILL):
                    got_binary = True
                if opt == EOR_OPT and cmd in (DO, WILL):
                    got_eor = True

                i += 3
                continue

            if cmd == SB:
                if i + 3 >= len(buffer):
                    break  # incomplete SB sequence; wait for more data
                opt = buffer[i + 2]
                if opt == TERMINAL_TYPE:
                    subopt = buffer[i + 3]
                    if subopt == 1:  # SEND
                        term = b"IBM-3278-2"
                        reply = bytes([IAC, SB, TERMINAL_TYPE, 0]) + term + bytes([IAC, SE])
                        print("TX:", binascii.hexlify(reply))
                        client_socket.sendall(reply)
                    elif subopt == 0:  # IS
                        term_type = buffer[i + 4 : buffer.index(IAC, i + 4)].decode(errors="ignore")
                        print("Client terminal type:", term_type)
                        got_term = True

                se_pos = buffer.find(bytes([IAC, SE]), i + 3)
                if se_pos != -1:
                    i = se_pos + 2
                else:
                    break
                continue
            else:
                print("Unknown IAC command:", cmd)

            i += 2

    print("Negotiation complete: binary={}, eor={}, term={}".format(got_binary, got_eor, got_term))


UTILITY_LAYOUT = UtilityLayout(
    ispf_option_addr=ISPF_OPTION_ADDR,
    dsutil_option_addr=DSUTIL_OPTION_ADDR,
    dsutil_dsn_addr=DSUTIL_DSN_ADDR,
    dsutil_new_dsn_addr=DSUTIL_NEW_DSN_ADDR,
    dsutil_type_addr=DSUTIL_TYPE_ADDR,
    movecopy_option_addr=MOVECOPY_OPTION_ADDR,
    movecopy_from_dsn_addr=MOVECOPY_FROM_DSN_ADDR,
    movecopy_from_member_addr=MOVECOPY_FROM_MEMBER_ADDR,
    movecopy_to_dsn_addr=MOVECOPY_TO_DSN_ADDR,
    movecopy_to_member_addr=MOVECOPY_TO_MEMBER_ADDR,
    jcl_option_addr=JCLSUB_OPTION_ADDR,
    jcl_dsn_addr=JCLSUB_DSN_ADDR,
    jcl_member_addr=JCLSUB_MEMBER_ADDR,
    dslist_cmd_addr=DSLIST_CMD_ADDR,
    dslist_level_addr=DSLIST_LEVEL_ADDR,
    dslist_results_first_row=DSLIST_RESULTS_FIRST_ROW,
    dslist_results_max_rows=DSLIST_RESULTS_MAX_ROWS,
    dslist_cmd_sf_col=DSLIST_LINE_CMD_SF_COL,
    dataset_scroll_addr=DATASET_SCROLL_ADDR,
    dataset_cmd_addr=DATASET_CMD_ADDR,
    dataset_lines_first_row=DATASET_LINES_FIRST_ROW,
    dataset_lines_max_rows=DATASET_LINES_MAX_ROWS,
    dataset_line_sf_col=DATASET_LINE_SF_COL,
    dataset_line_width=DATASET_LINE_WIDTH,
    dataset_edit_cmd_sf_col=DATASET_EDIT_CMD_SF_COL,
    dataset_edit_text_sf_col=DATASET_EDIT_TEXT_SF_COL,
    dataset_edit_text_width=DATASET_EDIT_TEXT_WIDTH,
)


UTILITY_ACTIONS = UtilityActions(
    send_ispf_dsutil=send_ispf_dsutil,
    send_ispf_movecopy=send_ispf_movecopy,
    send_ispf_jcl_submit=send_ispf_jcl_submit,
    send_ispf_dslist=send_ispf_dslist,
    send_dataset_panel=send_dataset_panel,
    read_client_input=read_client_input,
    aid_to_string=aid_to_string,
    load_catalog=load_catalog,
    save_catalog=save_catalog,
    search_catalog=search_catalog,
    is_pds_like=_is_pds_like,
    load_dataset_lines=load_dataset_lines,
    save_dataset_lines=save_dataset_lines,
    normalize_dsn=_normalize_dsn,
)


def handle_client(client_socket, addr):
    print(f"Connection from {addr}")
    tn3270_negotiate(client_socket)
    client_socket.settimeout(600)

    while True:
        # Logon loop
        error_msg = None
        userid = None
        while True:
            send_tso_logon(client_socket, error_msg)
            result = read_client_input(client_socket)
            if result is None:
                return
            aid, cursor_addr, fields = result
            print(f"AID={hex(aid)}, fields={fields}")

            aid_str = aid_to_string(aid)
            if aid_str in ("PF3", "PF15"):
                # Logoff
                return

            userid_raw = fields.get(LOGON_USERID_ADDR, "").strip().upper()
            password_raw = fields.get(LOGON_PASSWORD_ADDR, "").strip().upper()

            if not userid_raw:
                error_msg = "IKJ56700I USERID MUST BE SPECIFIED"
                continue

            if _CREDENTIALS.get(userid_raw) != password_raw:
                error_msg = f"IKJ56425I PASSWORD NOT CORRECT FOR {userid_raw}"
                continue

            userid = userid_raw
            set_default_submit_owner(userid)
            break

        # ISPF menu loop
        short_msg = None
        pending_main_option = None
        while True:
            if pending_main_option:
                aid_str = "Enter"
                option = pending_main_option.strip().upper()
                pending_main_option = None
            else:
                send_ispf_menu(client_socket, userid, short_msg)
                result = read_client_input(client_socket)
                if result is None:
                    return
                aid, cursor_addr, fields = result
                print(f"AID={hex(aid)}, fields={fields}")

                aid_str = aid_to_string(aid)
                option = fields.get(ISPF_OPTION_ADDR, "").strip().upper()
            stacked_utils_option = None

            if option.startswith("="):
                option = option[1:].strip().upper()

            if "." in option:
                parts = [p for p in option.split(".") if p]
                if len(parts) >= 2 and parts[0] == "3":
                    option = "3"
                    stacked_utils_option = parts[1]
                else:
                    short_msg = f"INVALID OPTION: {option}"
                    continue

            if option == "X" or aid_str in ("PF3", "PF15"):
                # Logoff — back to logon panel
                break

            valid_opts = {"0", "1", "2", "4", "5", "6", "7", "9", "10", "11", "12", "13"}
            if option == "2":
                edit_dsn = ""
                edit_msg = None
                while True:
                    send_ispf_edit_entry(client_socket, dsn=edit_dsn, short_msg=edit_msg)
                    edit_result = read_client_input(client_socket)
                    if edit_result is None:
                        return

                    edit_aid, edit_cursor_addr, edit_fields = edit_result
                    edit_aid_str = aid_to_string(edit_aid)
                    entered_dsn = edit_fields.get(EDIT_DSN_ADDR, "").strip().upper()
                    if entered_dsn:
                        edit_dsn = entered_dsn

                    if edit_dsn.startswith("="):
                        pending_main_option = edit_dsn[1:]
                        short_msg = None
                        break

                    if edit_dsn == "X" or edit_aid_str in ("PF3", "PF15"):
                        short_msg = None
                        break

                    if edit_aid_str != "Enter":
                        edit_msg = "PRESS ENTER TO EDIT OR PF3 TO RETURN"
                        continue

                    if not edit_dsn:
                        edit_msg = "ENTER A DATA SET NAME"
                        continue

                    launched = edit_dataset_by_name(
                        client_socket=client_socket,
                        actions=UTILITY_ACTIONS,
                        layout=UTILITY_LAYOUT,
                        dsn_input=edit_dsn,
                    )
                    if launched.disconnect:
                        return
                    if launched.jump_option:
                        pending_main_option = launched.jump_option
                        break
                    edit_msg = launched.message

                if not pending_main_option:
                    short_msg = None
            elif option == "3":
                utils_msg = None
                pending_utils_option = stacked_utils_option
                while True:
                    if pending_utils_option:
                        utility_result = handle_utility_option(
                            option=pending_utils_option,
                            client_socket=client_socket,
                            actions=UTILITY_ACTIONS,
                            layout=UTILITY_LAYOUT,
                        )
                        pending_utils_option = None
                        if utility_result.disconnect:
                            return
                        utils_msg = utility_result.message
                        continue

                    send_ispf_utils(client_socket, utils_msg)
                    utils_result = read_client_input(client_socket)
                    if utils_result is None:
                        return
                    utils_aid, utils_cursor_addr, utils_fields = utils_result
                    print(f"AID={hex(utils_aid)}, fields={utils_fields}")

                    utils_aid_str = aid_to_string(utils_aid)
                    utils_option = utils_fields.get(ISPF_OPTION_ADDR, "").strip().upper()

                    if utils_option.startswith("="):
                        pending_main_option = utils_option[1:]
                        break

                    if utils_option == "X" or utils_aid_str in ("PF3", "PF15"):
                        break

                    utility_result = handle_utility_option(
                        option=utils_option,
                        client_socket=client_socket,
                        actions=UTILITY_ACTIONS,
                        layout=UTILITY_LAYOUT,
                    )
                    if utility_result.disconnect:
                        return
                    if utility_result.jump_option:
                        pending_main_option = utility_result.jump_option
                        break
                    utils_msg = utility_result.message

                if not pending_main_option:
                    short_msg = None
            elif option == "S":
                disconnect, jump_option, sdsf_msg = handle_sdsf_session(client_socket, userid)
                if disconnect:
                    return
                if jump_option:
                    pending_main_option = jump_option
                short_msg = sdsf_msg
            elif option in valid_opts:
                short_msg = f"OPTION {option} NOT YET IMPLEMENTED"
            elif option:
                short_msg = f"INVALID OPTION: {option}"
            else:
                short_msg = None


def run_tn3270_server(host="0.0.0.0", port=2323):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((host, port))
        server_socket.listen(1)
        print(f"TN3270 server listening on {host}:{port}")
        while True:
            client_socket, addr = server_socket.accept()
            try:
                handle_client(client_socket, addr)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                print(f"Client {addr} disconnected unexpectedly")
            except Exception as e:
                print(f"Error handling client {addr}: {e}")
            finally:
                client_socket.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Open-ISPF TN3270 server")
    parser.add_argument(
        "--config",
        default="",
        help="Path to config JSON file (defaults to ./config.json)",
    )
    args = parser.parse_args()
    _configure_runtime(args.config)
    run_tn3270_server()
