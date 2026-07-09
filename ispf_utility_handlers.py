from typing import Dict

from utilities import UtilityActions, UtilityHandler, UtilityLayout, UtilityResult, handle_dslist, handle_dsutil, handle_jcl_submit, handle_movecopy


UTILITY_HANDLERS: Dict[str, UtilityHandler] = {
    "2": handle_dsutil,
    "3": handle_movecopy,
    "4": handle_dslist,
    "5": handle_jcl_submit,
}


def handle_utility_option(
    option: str,
    client_socket,
    actions: UtilityActions,
    layout: UtilityLayout,
) -> UtilityResult:
    handler = UTILITY_HANDLERS.get(option)
    if handler is not None:
        return handler(client_socket, actions, layout)

    valid_utils_opts = {str(i) for i in range(1, 15)}
    if option in valid_utils_opts:
        return UtilityResult(message=f"UTILITY {option} NOT YET IMPLEMENTED")
    if option:
        return UtilityResult(message=f"INVALID OPTION: {option}")
    return UtilityResult(message=None)
