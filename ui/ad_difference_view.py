"""
Showing what Active Directory holds, next to what the application holds

Version 26, point 19 b.  Two places show the same information in the same way:

* the **editing window** (``PropertyEditorDialog``) outlines every field whose
  value differs from the directory;
* the **person table** on the "3. Operations" tab outlines every such cell.

Hovering an outline shows the directory's value.  The popup is Qt's tooltip -
it appears where the user expects it and disappears on its own - but its
content is built here as HTML in the application's own colours and spacing, so
it does not look like a bare system hint.

The values compared against are the ones "Discover in AD" read; see
:mod:`services.ad_comparison`.  Nothing here ever talks to the directory.

One switch, two windows
-----------------------
The show/hide state is a *setting*, not a property of one dialog.  That is what
makes it survive: turning the outlines on while editing one person leaves them
on for the next person, and for the table.
"""

from __future__ import annotations

import logging
from html import escape
from typing import Any, Dict, Optional

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QStyledItemDelegate

from services.ad_comparison import FieldDifference, differing_fields

logger = logging.getLogger(__name__)

#: Settings key and category of the show/hide switch.
SHOW_DIFFERENCES_KEY = "show_ad_differences"
SHOW_DIFFERENCES_CATEGORY = "general"

#: Colour of the outline.  Amber rather than red: a difference is information,
#: not an error - the person may well have been edited on purpose.
OUTLINE_COLOR = QColor(230, 170, 60)

#: Width of the outline, in device-independent pixels.
OUTLINE_WIDTH = 2

#: Item data role carrying the :class:`FieldDifference` of a table cell.
DIFFERENCE_ROLE = int(Qt.ItemDataRole.UserRole) + 50

#: Style sheet for the tooltip chrome, applied once to the application so the
#: popup matches the rest of the interface instead of the system default.
TOOLTIP_STYLE = (
    "QToolTip {"
    "  background-color: #2b2b2b;"
    "  color: #e6e6e6;"
    f"  border: 1px solid {OUTLINE_COLOR.name()};"
    "  border-radius: 4px;"
    "  padding: 6px 8px;"
    "  font-size: 12px;"
    "}"
)

#: Text shown where Active Directory holds no value at all.
EMPTY_VALUE_TEXT = "(not set)"


def _settings(settings: Any = None):
    """Return the settings manager to use, or ``None`` when unavailable."""
    if settings is not None:
        return settings
    try:
        from utils.settings_manager import get_settings
        return get_settings()
    except Exception:                                # pragma: no cover - defensive
        logger.debug("Settings are unavailable", exc_info=True)
        return None


def differences_shown(settings: Any = None) -> bool:
    """
    Read the show/hide switch.

    Defaults to ``True`` and never raises: a settings file that cannot be read
    must not stop a dialog from opening.
    """
    manager = _settings(settings)
    if manager is None:
        return True
    try:
        return bool(manager.get_bool(SHOW_DIFFERENCES_KEY, True,
                                     category=SHOW_DIFFERENCES_CATEGORY))
    except Exception:
        logger.debug("Could not read %r", SHOW_DIFFERENCES_KEY, exc_info=True)
        return True


def set_differences_shown(shown: bool, settings: Any = None) -> None:
    """
    Store the show/hide switch so the next window opens the same way.

    A failure is logged and swallowed - the user's click still takes effect in
    the window they clicked in, it simply will not be remembered.
    """
    manager = _settings(settings)
    if manager is None:
        return
    try:
        manager.set(SHOW_DIFFERENCES_KEY, bool(shown),
                    category=SHOW_DIFFERENCES_CATEGORY)
    except Exception:
        logger.debug("Could not store %r", SHOW_DIFFERENCES_KEY, exc_info=True)


def toggle_button_text(shown: bool) -> str:
    """Caption of the show/hide button for the given state."""
    return "👁 Hide AD differences" if shown else "👁 Show AD differences"


def difference_tooltip(difference: FieldDifference) -> str:
    """
    Build the popup shown when the user hovers an outlined field or cell.

    Returns:
        Rich text naming the field and putting the two values side by side.
        Everything coming from the data is escaped, because a display name may
        legitimately contain ``<`` or ``&``.
    """
    ad_value = escape(difference.ad_value) or f"<i>{EMPTY_VALUE_TEXT}</i>"
    app_value = escape(difference.app_value) or f"<i>{EMPTY_VALUE_TEXT}</i>"
    label = escape(difference.label)

    return (
        f'<div style="margin-bottom:4px;">'
        f'<b style="color:{OUTLINE_COLOR.name()};">{label}</b> '
        f'<span style="color:#9e9e9e;">differs from Active Directory</span>'
        f'</div>'
        f'<div><span style="color:#9e9e9e;">In Active Directory:</span> '
        f'{ad_value}</div>'
        f'<div><span style="color:#9e9e9e;">In this application:</span> '
        f'{app_value}</div>'
        f'<div style="margin-top:4px;color:#7a7a7a;font-size:11px;">'
        f'Read by &quot;Discover in AD&quot;. Press it again to refresh.</div>'
    )


def apply_tooltip_style(app) -> None:
    """
    Give the application's tooltips the look used by these popups.

    Appends to whatever style sheet is already set rather than replacing it,
    so a theme applied elsewhere survives.
    """
    try:
        existing = app.styleSheet() or ""
        if TOOLTIP_STYLE in existing:
            return
        app.setStyleSheet(f"{existing}\n{TOOLTIP_STYLE}".strip())
    except Exception:                                # pragma: no cover - defensive
        logger.debug("Could not style the tooltips", exc_info=True)


def outline_style_sheet(widget_class: str = "QLineEdit") -> str:
    """
    Style sheet that draws the outline around a plain input widget.

    Args:
        widget_class: The Qt class name the rule applies to, so the rule does
            not leak into child widgets of a different kind.
    """
    return (f"{widget_class} {{ border: {OUTLINE_WIDTH}px solid "
            f"{OUTLINE_COLOR.name()}; border-radius: 3px; }}")


def mark_widget(widget, difference: Optional[FieldDifference],
                widget_class: str = "QLineEdit") -> None:
    """
    Outline a widget and give it the explaining tooltip, or clear both.

    Args:
        widget: The widget to mark.  ``None`` is accepted and ignored, so a
            caller need not check whether an optional field exists.
        difference: The difference to show, or ``None`` to clear the mark.
        widget_class: Qt class name for the style rule.
    """
    if widget is None:
        return
    try:
        if difference is None:
            widget.setStyleSheet("")
            widget.setToolTip("")
        else:
            widget.setStyleSheet(outline_style_sheet(widget_class))
            widget.setToolTip(difference_tooltip(difference))
    except Exception:                                # pragma: no cover - defensive
        logger.debug("Could not mark %r", widget, exc_info=True)


def mark_item(item, difference: Optional[FieldDifference]) -> None:
    """
    Flag a table cell as differing, or clear the flag.

    The outline itself is painted by :class:`ADDifferenceDelegate`; a
    ``QTableWidgetItem`` cannot carry a border of its own.
    """
    if item is None:
        return
    if difference is None:
        item.setData(DIFFERENCE_ROLE, None)
        return
    item.setData(DIFFERENCE_ROLE, difference.as_dict())
    item.setToolTip(difference_tooltip(difference))


def combined_difference(label: str, parts) -> Optional[FieldDifference]:
    """
    Merge the differences of several fields shown in one cell.

    The person table puts the first and the last name in a single "Name" cell
    and the drive plus the path in a single "Home Directory" cell, so one cell
    can be explained by two differences.

    Args:
        label: Caption for the merged difference, normally the column name.
        parts: The differences to merge, in the order they should read.

    Returns:
        A single :class:`FieldDifference` carrying both sides joined by a
        space, or ``None`` when *parts* is empty.
    """
    parts = [part for part in parts if part is not None]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]

    merged = FieldDifference(
        parts[0].field,
        " ".join(part.app_value for part in parts if part.app_value),
        " ".join(part.ad_value for part in parts if part.ad_value),
    )
    merged.label = label
    return merged


def person_differences(person) -> Dict[str, FieldDifference]:
    """
    The differences recorded for a person, keyed by field name.

    A thin re-export of :func:`services.ad_comparison.differing_fields`, so the
    user interface has a single module to import from.
    """
    return differing_fields(person)


class ADDifferenceDelegate(QStyledItemDelegate):
    """
    Paints the outline around table cells that differ from Active Directory.

    The delegate asks a callable whether the outlines are switched on, so the
    table can hand it its own button state instead of the delegate reaching
    into the settings on every repaint.
    """

    def __init__(self, is_enabled=None, parent=None):
        """
        Args:
            is_enabled: Callable returning ``True`` while outlines should be
                drawn.  Defaults to the stored setting.
            parent: Qt parent.
        """
        super().__init__(parent)
        self._is_enabled = is_enabled or differences_shown

    def _should_paint(self, index) -> bool:
        """True when this cell carries a difference and outlines are on."""
        try:
            if not self._is_enabled():
                return False
        except Exception:                            # pragma: no cover - defensive
            logger.debug("Outline switch raised", exc_info=True)
            return False
        return bool(index.data(DIFFERENCE_ROLE))

    def paint(self, painter: QPainter, option, index) -> None:
        """Draw the cell normally, then the outline on top of it."""
        super().paint(painter, option, index)

        if not self._should_paint(index):
            return

        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pen = QPen(OUTLINE_COLOR)
            pen.setWidth(OUTLINE_WIDTH)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # Inset by half the pen width so the stroke stays inside the cell
            # and is not clipped in half by the neighbouring column.
            inset = OUTLINE_WIDTH / 2.0
            rect = QRectF(option.rect).adjusted(inset, inset, -inset, -inset)
            painter.drawRoundedRect(rect, 3.0, 3.0)
        finally:
            painter.restore()
