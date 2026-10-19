"""The scene-lifetime owner of the overlap decisions.

One ``IntentSession`` per Maya process keeps an ``IntentStore`` bound to the
open scene through file callbacks: it binds on open/new, drops decisions that
an import could hand to another mesh, and writes the sidecar after a real save.
It lives until ``shutdown()`` or Maya exits, so closing the window loses
nothing, and it holds no window reference.

The callback ids live on a holder module in ``sys.modules``, so a reloaded
copy of this module removes the previous copy's callbacks before adding its
own. The registry is separate from ``ui.scene``'s, whose counts do not include
these callbacks.

A session created while a scene is already open (by the window, say) did not
see what changed in memory since the open, so that first bind checks every
decision's witness path, for every file format. Only a bind at kAfterOpen of
an unmodified scene skips it.

Opt-in at Maya start (``userSetup.py``)::

    from asset_validator.intent import session; session.install()
"""

from __future__ import annotations

from collections.abc import Callable
import sys
import types

import maya.api.OpenMaya as om
import maya.cmds as cmds

from asset_validator.intent.store import IntentStore, file_stat


_HOLDER_NAME = "_asset_validator_intent_callbacks"
_SESSION: "IntentSession | None" = None


def _holder() -> types.ModuleType:
    holder = sys.modules.setdefault(_HOLDER_NAME, types.ModuleType(_HOLDER_NAME))
    if not isinstance(getattr(holder, "ids", None), list):
        holder.ids = []
    return holder


def _remove(ids: list[int]) -> None:
    while ids:
        callback_id = ids.pop()
        try:
            om.MMessage.removeCallback(callback_id)
        except RuntimeError:
            pass


def _scene_name() -> str:
    return cmds.file(query=True, sceneName=True) or ""


class IntentSession:
    """Scene callbacks that keep ``store`` bound to the open scene."""

    def __init__(self) -> None:
        self.store = IntentStore()
        self._ids: list[int] = []
        self._pending: str | None = None          # scene path named at kBeforeSave
        self._before_stat: tuple[int, int] | None = None
        holder = _holder()
        _remove(holder.ids)                       # an earlier module load's callbacks
        message = om.MSceneMessage
        for event, handler in (
            (message.kBeforeNew, self._unbind),
            (message.kBeforeOpen, self._unbind),
            (message.kAfterNew, self._after_new),
            (message.kAfterOpen, self._after_open),
            (message.kBeforeImport, self._before_import),
            (message.kBeforeImportReference, self._before_import_reference),
            (message.kAfterImport, self._verify),
            (message.kAfterImportReference, self._verify),
            (message.kAfterCreateReference, self._verify),
            (message.kAfterLoadReference, self._verify),
            (message.kAfterUnloadReference, self._verify),
            (message.kAfterRemoveReference, self._verify),
            (message.kBeforeSave, self._before_save),
            (message.kAfterSave, self._after_save),
        ):
            callback_id = message.addCallback(event, handler)
            self._ids.append(callback_id)
            holder.ids.append(callback_id)
        self.store.bind(_scene_name())            # a late bind: never a fresh open

    def callback_count(self) -> int:
        return len(self._ids)

    def close(self) -> None:
        holder = _holder()
        for callback_id in self._ids:
            if callback_id in holder.ids:
                holder.ids.remove(callback_id)
        _remove(self._ids)

    # Maya prints and swallows a callback's exception; these handlers swallow
    # their own so a failure is a note, and a failed save step blocks writing.
    def _guard(self, action: Callable[[], None], *, saving: bool = False) -> None:
        try:
            action()
        except Exception as exc:
            if saving:
                self.store.save_blocked = True
            self.store.notes.append(
                f"Overlap decisions: {type(exc).__name__}: {exc}"
                + ("; they were not written." if saving else "")
            )

    def _unbind(self, *_args) -> None:
        self._guard(self.store.unbind)

    def _after_new(self, *_args) -> None:
        self._guard(lambda: self.store.bind(""))

    def _after_open(self, *_args) -> None:
        def step() -> None:
            # Something that edited the scene during the open (a script node,
            # for example) leaves it modified: then it is a late bind too.
            fresh = not cmds.file(query=True, modified=True)
            self.store.bind(_scene_name(), fresh_open=fresh)

        self._guard(step)

    def _before_import(self, *_args) -> None:
        self._guard(lambda: self.store.drop_absent("import"))

    def _before_import_reference(self, *_args) -> None:
        self._guard(lambda: self.store.drop_absent("importReference"))

    def _verify(self, *_args) -> None:
        self._guard(lambda: self.store.verify_identities("reference"))

    def _before_save(self, *_args) -> None:
        def step() -> None:
            # During a Save As, sceneName already names the new file.
            self._pending = _scene_name()
            self._before_stat = file_stat(self._pending) if self._pending else None
            self.store.before_save()

        self._pending = None
        self._guard(step, saving=True)

    def _after_save(self, *_args) -> None:
        pending, before_stat = self._pending, self._before_stat
        self._pending, self._before_stat = None, None
        if not pending:
            return                                # no observed kBeforeSave: write nothing
        self._guard(lambda: self.store.after_save(pending, before_stat), saving=True)


def get_session(*, create: bool = False) -> IntentSession | None:
    """The process-wide session; created on first Mark, a sidecar on a run, or install()."""

    global _SESSION
    if _SESSION is None and create:
        _SESSION = IntentSession()
    return _SESSION


def install() -> IntentSession:
    """Start the session now, for example from userSetup.py."""

    session = get_session(create=True)
    assert session is not None
    return session


def shutdown() -> None:
    """Remove every intent callback and forget the session and its decisions."""

    global _SESSION
    if _SESSION is not None:
        _SESSION.close()
    _remove(_holder().ids)
    _SESSION = None


def live_callback_count() -> int:
    """Intent callbacks currently registered (any module load)."""

    return len(_holder().ids)


__all__ = ["IntentSession", "get_session", "install", "live_callback_count", "shutdown"]
