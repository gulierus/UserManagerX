"""
Equal-width tab bar
===================

Qt sizes each tab to its own label, so "1. Data Sources" and "5. Logs" end up
with very different widths. This bar gives every tab the width of the widest
one, which keeps the application's tab strip even.
"""

from PyQt6.QtCore import QSize
from PyQt6.QtWidgets import QTabBar


class EqualWidthTabBar(QTabBar):
    """
    Tab bar in which every tab is exactly as wide as the widest one.

    Qt sizes each tab to its own label, so "1. Data Sources" and "5. Logs"
    end up with very different widths.  Returning the maximum size hint for
    every tab makes the whole bar uniform, which is what the application
    asks for.
    """

    #: Extra horizontal padding added to the widest label.
    PADDING = 16

    def tabSizeHint(self, index: int) -> QSize:
        """Return the same size for every tab: the largest natural one."""
        hint = super().tabSizeHint(index)

        widest = hint.width()
        tallest = hint.height()
        for other in range(self.count()):
            other_hint = super().tabSizeHint(other)
            widest = max(widest, other_hint.width())
            tallest = max(tallest, other_hint.height())

        return QSize(widest + self.PADDING, tallest)
