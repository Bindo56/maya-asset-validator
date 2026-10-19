"""Drag-and-drop installer for the Maya Asset Validator.

Drag this file from the Validation_Tool_Maya folder into the Maya viewport.
It installs the tool for the Maya version it is dropped into: the files are
copied to that version's user modules folder, a ``.mod`` file is written next
to them, and a Validate button is added on a ValidationToolMaya shelf. The
tool works in the current session straight away and loads by itself from the
next start. Dropping it again offers Reinstall and Uninstall.
"""

# This file stays valid Python 2 syntax (no f-strings, no annotations), so an
# old Maya reaches the version check and shows its message, not a SyntaxError.

import os
import re
import shutil
import stat
import sys
import traceback

import maya.cmds as cmds
import maya.mel as mel


MODULE_NAME = "Validation_Tool_Maya"
SHELF_NAME = "ValidationToolMaya"
WINDOW_NAME = "AssetValidatorWindow"
MIN_MAYA = 2024
MIN_PYTHON = (3, 10)
TITLE = "Asset Validator"
# Only what the tool needs at runtime; tests and dev files stay behind.
COPIED = ("scripts", "README.md", "install_asset_validator.py")


def onMayaDroppedPythonFile(obj):
    """Maya calls this when the file is dropped onto the viewport."""
    try:
        _run()
    except Exception as exc:
        traceback.print_exc()
        _message("The installer stopped unexpectedly:\n\n{0}".format(exc))
    finally:
        # Maya imports the dropped file as a module and reuses a cached one,
        # so forget this module: the next drop then runs the file it names.
        sys.modules.pop(__name__, None)
    return True


def _run():
    api = cmds.about(apiVersion=True)
    major = api // 10000 if api >= 10000000 else api // 100
    if major < MIN_MAYA or sys.version_info < MIN_PYTHON:
        _message("The Asset Validator needs Maya {0} or later. This is Maya {1}.".format(MIN_MAYA, major))
        return

    source = os.path.dirname(os.path.abspath(__file__))
    if not _is_ours(source):
        _message("Keep install_asset_validator.py inside the {0} folder, then drag it into Maya again."
                 .format(MODULE_NAME))
        return

    modules_dir, shared = _modules_dir(major)
    target = os.path.join(modules_dir, MODULE_NAME)
    mod_file = os.path.join(modules_dir, MODULE_NAME + ".mod")
    version = _read_version(source)
    in_place = _same_path(source, target)

    if os.path.isdir(target) and not _is_ours(target):
        if _inside(source, target):
            _message("This copy is inside\n{0}\n\nMove the {1} folder out of the modules folder, then "
                     "drag the installer into Maya again. Nothing was changed.".format(target, MODULE_NAME))
        else:
            _message("{0}\nalready exists but is not an Asset Validator install.\n\nMove or rename it, then "
                     "drag the installer into Maya again. Nothing was changed.".format(target))
        return

    if os.path.isfile(mod_file) or (os.path.isdir(target) and not in_place):
        if _is_ours(target):
            status = "Asset Validator {0} is installed for Maya {1}.".format(_read_version(target), major)
        else:
            status = "An incomplete Asset Validator install was found for Maya {0}.".format(major)
        reinstall = "Reinstall {0}".format(version)
        choice = _ask("{0}\n\n{1}".format(status, target), [reinstall, "Uninstall", "Cancel"])
        if choice == "Uninstall":
            _uninstall(major, target, mod_file)
            return
        if choice != reinstall:
            return
    elif _ask("Install Asset Validator {0} for Maya {1}?\n\nFiles go to:\n{2}".format(version, major, target),
              ["Install", "Cancel"]) != "Install":
        return

    _install(major, source, target, mod_file, version, shared, in_place)


def _install(major, source, target, mod_file, version, shared, in_place):
    if not in_place:
        # Copy into a staging folder and swap it in, so a failed copy or a
        # locked file leaves the previous install untouched.
        staging, previous = target + ".installing", target + ".previous"
        try:
            for leftover in (staging, previous):
                _remove_dir(leftover)
            _remove_dir(target + ".removing", ignore_errors=True)
            os.makedirs(staging)
            for name in COPIED:
                path = os.path.join(source, name)
                # copyfile, not copy2: read-only source files (a Perforce sync)
                # must not make the install impossible to replace or remove.
                if os.path.isdir(path):
                    shutil.copytree(path, os.path.join(staging, name), copy_function=shutil.copyfile,
                                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                elif os.path.isfile(path):
                    shutil.copyfile(path, os.path.join(staging, name))
            if _exists(target):
                os.rename(target, previous)
            try:
                os.rename(staging, target)
            except OSError:
                if _exists(previous):
                    os.rename(previous, target)
                raise
        except OSError as exc:
            _remove_dir(staging, ignore_errors=True)
            _message("The tool could not be installed, so nothing was changed. Close any program using\n"
                     "{0}\nand try again.\n\n{1}".format(target, exc))
            return
        _remove_dir(previous, ignore_errors=True)

    # A module in the shared folder is read by every Maya version, so pin it
    # to this one; the per-version folder needs no condition. The root path is
    # absolute: Maya resolved a relative one against its own modules folder.
    condition = "MAYAVERSION:{0} ".format(major) if shared else ""
    root = target.replace("\\", "/")
    try:
        with open(mod_file, "w", encoding="utf-8") as handle:
            handle.write("+ {0}{1} {2} {3}\nscripts: scripts\n".format(condition, MODULE_NAME, version, root))
    except OSError as exc:
        _message("The module file could not be written, so Maya will not load the tool on start.\n\n{0}"
                 .format(exc))
        return

    # Append, never insert at 0: Maya removes sys.path[0] after the drop.
    scripts = os.path.join(target, "scripts")
    if not any(_same_path(scripts, entry) for entry in sys.path if entry):
        sys.path.append(scripts)

    reloaded = "asset_validator" in sys.modules
    lines = ["Asset Validator {0} is installed for Maya {1}.".format(version, major)]
    try:
        from asset_validator.shelf import install_shelf
        install_shelf()
        _save_shelves()
    except Exception as exc:
        lines.append("\nThe shelf button could not be added ({0}). Run this instead:\n"
                     "from asset_validator.launcher import launch; launch()".format(exc))
    else:
        lines.append("Click Validate on the {0} shelf to open it.".format(SHELF_NAME))
    if reloaded:
        lines.append("\nRestart Maya to use the newly installed files.")
    other = _other_module_file(mod_file)
    if other:
        lines.append("\nAnother copy is also registered by:\n{0}\n"
                     "Remove that module file so only one copy loads.".format(other))
    _message("\n".join(lines))


def _uninstall(major, target, mod_file):
    # Move the folder aside first: it is the only step that can fail on a
    # file in use, and failing there leaves the install complete.
    removing = target + ".removing"
    try:
        _remove_dir(removing, ignore_errors=True)
        if _exists(target):
            os.rename(target, removing)
        if os.path.isfile(mod_file):
            os.remove(mod_file)
    except OSError as exc:
        _message("Nothing was removed. Close any program using\n{0}\nand try again.\n\n{1}".format(target, exc))
        return

    try:
        cmds.deleteUI(WINDOW_NAME)
    except RuntimeError:
        pass
    lines = ["The Asset Validator was removed from Maya {0}.".format(major), "Restart Maya to finish."]
    try:
        _remove_shelf()
    except Exception as exc:
        lines.append("\nThe {0} shelf could not be fully removed ({1}). Delete it in the Shelf Editor."
                     .format(SHELF_NAME, exc))
    _remove_dir(removing, ignore_errors=True)
    scripts = os.path.join(target, "scripts")
    sys.path[:] = [entry for entry in sys.path if not entry or not _same_path(scripts, entry)]
    _message("\n".join(lines))


def _modules_dir(major):
    """The user modules folder Maya scans for this version, and whether it is shared."""
    user_app = cmds.internalVar(userAppDir=True)
    own = os.path.join(user_app, str(major), "modules")
    shared = os.path.join(user_app, "modules")
    scanned = [entry for entry in os.environ.get("MAYA_MODULE_PATH", "").split(os.pathsep) if entry]
    for candidate, is_shared in ((own, False), (shared, True)):
        if any(_same_path(candidate, entry) for entry in scanned):
            return os.path.normpath(candidate), is_shared
    return os.path.normpath(own), False


def _remove_shelf():
    """Delete the shelf tab and its file without Maya's confirmation prompt."""
    shelf_top = mel.eval("$tmp = $gShelfTopLevel")
    if shelf_top and cmds.shelfLayout(SHELF_NAME, exists=True):
        # Shift the numbered shelf preferences down over the removed entry, as
        # Maya's deleteShelfTab does. Tabs created this session without
        # preferences are skipped, so only registered entries move.
        count = cmds.shelfTabLayout(shelf_top, query=True, numberOfChildren=True)
        index = next((i for i in range(1, count + 1)
                      if cmds.optionVar(query="shelfName{0}".format(i)) == SHELF_NAME), None)
        if index is not None:
            last = index
            while cmds.optionVar(exists="shelfName{0}".format(last + 1)):
                last += 1
            for i in range(index, last):
                nxt = i + 1
                align = "left"
                if cmds.optionVar(exists="shelfAlign{0}".format(nxt)):
                    align = str(cmds.optionVar(query="shelfAlign{0}".format(nxt)))
                cmds.optionVar(intValue=("shelfLoad{0}".format(i), int(cmds.optionVar(query="shelfLoad{0}".format(nxt)))))
                cmds.optionVar(stringValue=("shelfName{0}".format(i), str(cmds.optionVar(query="shelfName{0}".format(nxt)))))
                cmds.optionVar(stringValue=("shelfAlign{0}".format(i), align))
                cmds.optionVar(stringValue=("shelfFile{0}".format(i), str(cmds.optionVar(query="shelfFile{0}".format(nxt)))))
            for key in ("shelfLoad", "shelfName", "shelfAlign", "shelfFile"):
                cmds.optionVar(remove="{0}{1}".format(key, last))
        cmds.deleteUI("{0}|{1}".format(shelf_top, SHELF_NAME), layout=True)
        try:
            mel.eval("shelfTabChange")
        except RuntimeError:
            pass
    # Only the user's own shelf file; shelves on a studio shelf path stay.
    shelf_file = os.path.join(cmds.internalVar(userPrefDir=True), "shelves", "shelf_{0}.mel".format(SHELF_NAME))
    if os.path.isfile(shelf_file):
        os.remove(shelf_file)


def _save_shelves():
    try:
        mel.eval("saveAllShelves $gShelfTopLevel")
    except RuntimeError:
        pass  # Maya also saves its shelves when it closes.


def _other_module_file(mod_file):
    """The .mod file of another copy of this module that Maya knows, if any.

    Compares module files, not root folders: Maya caches a module's resolved
    folder when it first finds it in a session and does not re-read it.
    """
    try:
        if MODULE_NAME not in (cmds.moduleInfo(listModules=True) or []):
            return None
        definition = cmds.moduleInfo(moduleName=MODULE_NAME, definition=True)
    except RuntimeError:
        return None
    return None if not definition or _same_path(definition, mod_file) else os.path.normpath(definition)


def _remove_dir(path, ignore_errors=False):
    """Remove a folder, read-only files included; a link is removed, never its target."""
    def make_writable(func, failed, _info):
        os.chmod(failed, stat.S_IWRITE)
        func(failed)

    try:
        if _is_link(path):
            (os.rmdir if os.name == "nt" else os.unlink)(path)
        elif os.path.isdir(path):
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=make_writable)
            else:
                shutil.rmtree(path, onerror=make_writable)
    except OSError:
        if not ignore_errors:
            raise


def _is_link(path):
    if os.path.islink(path):
        return True
    try:
        return getattr(os.lstat(path), "st_reparse_tag", 0) == stat.IO_REPARSE_TAG_MOUNT_POINT
    except (OSError, AttributeError):
        return False


def _exists(path):
    return _is_link(path) or os.path.exists(path)


def _package_init(root):
    return os.path.join(root, "scripts", "asset_validator", "__init__.py")


def _is_ours(folder):
    return os.path.isfile(_package_init(folder))


def _read_version(root):
    with open(_package_init(root), encoding="utf-8") as handle:
        match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', handle.read(), re.MULTILINE)
    return match.group(1) if match else "0.0.0"


def _norm(path):
    return os.path.normcase(os.path.realpath(path))


def _same_path(a, b):
    return _norm(a) == _norm(b)


def _inside(path, folder):
    return _norm(path).startswith(_norm(folder) + os.sep)


def _ask(message, buttons):
    return cmds.confirmDialog(title=TITLE, message=message, button=buttons,
                              defaultButton=buttons[0], cancelButton=buttons[-1],
                              dismissString=buttons[-1])


def _message(text):
    cmds.confirmDialog(title=TITLE, message=text, button=["OK"], defaultButton="OK")
