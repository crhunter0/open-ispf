from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class UtilityLayout:
    ispf_option_addr: int
    dsutil_option_addr: int
    dsutil_dsn_addr: int
    dsutil_new_dsn_addr: int
    dslist_level_addr: int
    dslist_results_first_row: int
    dslist_results_max_rows: int
    dslist_cmd_sf_col: int
    dataset_scroll_addr: int
    dataset_cmd_addr: int
    dataset_lines_first_row: int
    dataset_lines_max_rows: int
    dataset_line_sf_col: int
    dataset_line_width: int
    dataset_edit_cmd_sf_col: int
    dataset_edit_text_sf_col: int
    dataset_edit_text_width: int


@dataclass(frozen=True)
class UtilityActions:
    send_ispf_dsutil: Callable[..., None]
    send_ispf_dslist: Callable[..., None]
    send_dataset_panel: Callable[..., None]
    read_client_input: Callable[[Any], Any]
    aid_to_string: Callable[[int], str]
    load_catalog: Callable[[], list]
    save_catalog: Callable[[list], Optional[str]]
    search_catalog: Callable[[list, str], list]
    is_pds_like: Callable[[dict], bool]
    load_dataset_lines: Callable[[dict], tuple[list, str]]
    save_dataset_lines: Callable[[dict, list], str]
    normalize_dsn: Callable[[str], str]


@dataclass(frozen=True)
class UtilityResult:
    message: Optional[str]
    exit_menu: bool = False
    disconnect: bool = False


UtilityHandler = Callable[[Any, UtilityActions, UtilityLayout], UtilityResult]
