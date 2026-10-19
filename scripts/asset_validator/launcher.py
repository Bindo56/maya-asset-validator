"""Stable shelf entry point for the validator window."""

from importlib import import_module


def launch():
    """Show the validator window, or warn when its UI module is missing."""
    try:
        window_module = import_module("asset_validator.ui.window")
    except ModuleNotFoundError as exc:
        if exc.name != "asset_validator.ui.window":
            raise
        import maya.cmds as cmds

        cmds.warning("The Asset Validator window module is missing.")
        return None

    return window_module.show_window()
