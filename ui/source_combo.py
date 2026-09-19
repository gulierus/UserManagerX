"""
Shared labelling of data sources inside combo boxes.

A source is either read-only (Active Directory, an imported encrypted file)
or editable (anything the user may change in place).  Which one a source is
decides whether half of the application's buttons will work on it, but the
combo boxes used to show the bare name, so the user only found out after
picking the source and finding the controls disabled.

Every source combo therefore appends the access mode to the name:

    Students 2025            ->  Students 2025 (editable)
    AD school.local          ->  AD school.local (read-only)

The suffix can be turned off in Settings -> General for users who prefer the
shorter names; the setting is read through :func:`show_access_enabled` so the
whole application follows one switch.

The real source name is always stored in the item's *data*, never parsed back
out of the visible text.  Lookups therefore keep working no matter how the
label is formatted, and a source genuinely named ``Backup (editable)`` cannot
be confused with a decorated one.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: Settings key and category of the "show access mode" switch.
ACCESS_SETTING_KEY = "show_source_access_in_lists"
ACCESS_SETTING_CATEGORY = "general"

#: Words appended to the source name.
READONLY_LABEL = "read-only"
EDITABLE_LABEL = "editable"

#: Text of the "nothing selected" entry shared by every source combo.
PLACEHOLDER_TEXT = "(Select Source)"

#: Suffixes recognised when a label has to be turned back into a name without
#: the combo at hand (see :func:`strip_access_suffix`).
_KNOWN_SUFFIXES = (f" ({READONLY_LABEL})", f" ({EDITABLE_LABEL})")


def is_readonly(source: Any) -> bool:
    """
    Report whether a source may not be edited.

    A source object that does not carry the flag at all is treated as
    editable, which is what the :class:`~models.Source` default says.
    """
    return bool(getattr(source, "readonly", False))


def access_label(source: Any) -> str:
    """Return ``'read-only'`` or ``'editable'`` for *source*."""
    return READONLY_LABEL if is_readonly(source) else EDITABLE_LABEL


def show_access_enabled(settings: Any = None) -> bool:
    """
    Read the "show access mode" switch.

    Args:
        settings: Settings manager to read from.  The application-wide one is
            used when omitted.

    Returns:
        ``True`` when the suffix should be shown.  Defaults to ``True`` and
        never raises - a settings file that cannot be read must not stop a
        combo box from being filled.
    """
    try:
        if settings is None:
            from utils.settings_manager import get_settings
            settings = get_settings()
        return bool(settings.get_bool(ACCESS_SETTING_KEY, True,
                                      category=ACCESS_SETTING_CATEGORY))
    except Exception:
        logger.debug("Could not read %r - showing the access mode",
                     ACCESS_SETTING_KEY, exc_info=True)
        return True


def source_display_label(source: Any, show_access: Optional[bool] = None) -> str:
    """
    Build the text shown for one source.

    Args:
        source: The source object.
        show_access: Force the suffix on or off.  When ``None`` the user
            setting decides.

    Returns:
        ``'Students 2025 (editable)'`` or just ``'Students 2025'``.
    """
    name = str(getattr(source, "name", "") or "")
    if show_access is None:
        show_access = show_access_enabled()
    if not show_access:
        return name
    return f"{name} ({access_label(source)})"


def strip_access_suffix(text: str) -> str:
    """
    Remove a trailing access suffix from a label.

    Only used as a last resort, when a label cannot be matched against a combo
    box item - for instance a selection remembered before the list was rebuilt.
    """
    label = (text or "").strip()
    for suffix in _KNOWN_SUFFIXES:
        if label.endswith(suffix):
            return label[: -len(suffix)].rstrip()
    return label


def populate_source_combo(combo, sources: Iterable[Any],
                          placeholder: Optional[str] = PLACEHOLDER_TEXT,
                          show_access: Optional[bool] = None) -> None:
    """
    Fill a combo box with sources, labelled and with their names as data.

    The combo's signals are *not* blocked here - the caller decides whether a
    rebuild should be visible to its handlers, because some callers restore
    the previous selection afterwards and some do not.

    Args:
        combo: The ``QComboBox`` to fill.  It is cleared first.
        sources: The source objects, in the order they should appear.
        placeholder: Text of the leading "nothing selected" entry.  Pass
            ``None`` to build a combo without one.
        show_access: Force the suffix on or off; ``None`` follows the setting.
    """
    if show_access is None:
        show_access = show_access_enabled()

    combo.clear()
    if placeholder is not None:
        # The placeholder carries no source name, so every lookup by data
        # cleanly reports "nothing selected".
        combo.addItem(placeholder, None)

    for source in sources:
        combo.addItem(source_display_label(source, show_access),
                      str(getattr(source, "name", "") or ""))


def combo_source_name(combo, text: Optional[str] = None) -> str:
    """
    Translate what a combo box shows into the name of the source behind it.

    Args:
        combo: The combo box built by :func:`populate_source_combo`.
        text: A label to resolve.  When omitted the current selection is used.

    Returns:
        The source name, or the placeholder text when nothing is selected.
        Callers may pass the result straight to
        :meth:`SourceManager.get_source_by_name`.
    """
    if text is None:
        data = combo.currentData()
        if isinstance(data, str) and data:
            return data
        return combo.currentText()

    index = combo.findText(text)
    if index >= 0:
        data = combo.itemData(index)
        if isinstance(data, str) and data:
            return data
        # An item without data is the placeholder - report it unchanged so the
        # caller's "nothing selected" comparison still matches.
        return text

    # The label is not in the combo (a stale selection, or a caller that passed
    # a plain source name).  Fall back to removing a known suffix.
    return strip_access_suffix(text)


def find_source_index(combo, name: str) -> int:
    """
    Find the row holding a given source, whatever its label looks like.

    Args:
        combo: The combo box built by :func:`populate_source_combo`.
        name: The source name to look for.

    Returns:
        The row index, or ``-1`` when the source is not in the list.
    """
    if not name:
        return -1
    index = combo.findData(name)
    if index >= 0:
        return index
    # Tolerate a combo that was filled without data (or a label passed in
    # place of a name) so this helper is safe to call from anywhere.
    return combo.findText(name)


def source_labels(combo) -> List[str]:
    """Return the visible text of every row - convenience for tests and logs."""
    return [combo.itemText(row) for row in range(combo.count())]
