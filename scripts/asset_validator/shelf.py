"""Install one reusable button on a Maya shelf."""

import maya.cmds as cmds
import maya.mel as mel


_SHELF = "ValidationToolMaya"
_BUTTON_NAME = "validationToolMayaButton"
_COMMAND = "from asset_validator.launcher import launch; launch()"


def install_shelf():
    """Create or update the shelf button in Maya's graphical session."""
    if cmds.about(batch=True):
        raise RuntimeError("Install the shelf button in the Maya GUI, not mayapy.")
    shelf_top = mel.eval("$tmp = $gShelfTopLevel")
    if not shelf_top or not cmds.shelfTabLayout(shelf_top, exists=True):
        raise RuntimeError("Install the shelf button in the Maya GUI, not mayapy.")

    if not cmds.shelfLayout(_SHELF, exists=True):
        cmds.shelfLayout(_SHELF, parent=shelf_top)
    else:
        # Load the saved buttons first: Maya skips loading a shelf that
        # already has children, so adding to an unloaded shelf would hide them.
        mel.eval(f'loadNamedShelf "{_SHELF}"')

    # Saved shelves keep no button names, so find earlier Validate buttons by
    # their command; this also clears duplicates.
    shelf_path = cmds.shelfLayout(_SHELF, query=True, fullPathName=True)
    for child in cmds.shelfLayout(_SHELF, query=True, childArray=True) or []:
        path = f"{shelf_path}|{child}"
        if cmds.objectTypeUI(path) == "shelfButton" and \
                cmds.shelfButton(path, query=True, command=True) == _COMMAND:
            cmds.deleteUI(path)
    if cmds.shelfButton(_BUTTON_NAME, exists=True):
        cmds.deleteUI(_BUTTON_NAME)

    return cmds.shelfButton(
        _BUTTON_NAME,
        parent=_SHELF,
        label="Validate",
        annotation="Open the Maya Asset Validator",
        image1="commandButton.png",
        sourceType="python",
        command=_COMMAND,
    )
