"""
Enrollment year service
=======================

Glue between :mod:`utils.school_year` (which knows how to calculate) and the
user interface (which has to ask before overwriting).

One entry point, :func:`update_source_enrollment_years`, is used by both
callers: the automatic run after a source is loaded on the *Data Sources* tab,
and the button on the *Comparison and Sync* tab.

The resolved year is cached for the session so that loading five sources does
not make five network requests.
"""

import logging
from typing import Optional, Tuple

from utils.school_year import (
    YearResolution, apply_enrollment_changes, compute_enrollment_changes,
    resolve_current_year,
)

logger = logging.getLogger(__name__)

#: Cached year resolution, so each session asks the network once.
_cached_resolution: Optional[YearResolution] = None


def is_year_cached() -> bool:
    """True when the current year has already been resolved this session."""
    return _cached_resolution is not None


def get_year_resolution(refresh: bool = False,
                        use_internet: bool = True) -> YearResolution:
    """
    Return the current year, asking the network at most once per session.

    Args:
        refresh: Force a new lookup.
        use_internet: Set to False to skip the network.

    Returns:
        The :class:`~utils.school_year.YearResolution`.
    """
    global _cached_resolution

    if _cached_resolution is None or refresh:
        _cached_resolution = resolve_current_year(use_internet=use_internet)
        logger.info("Current year resolved: %s", _cached_resolution.describe())

    return _cached_resolution


def reset_year_cache() -> None:
    """Forget the cached year (used by the tests and by an explicit refresh)."""
    global _cached_resolution
    _cached_resolution = None


def update_source_enrollment_years(source, parent=None, silent: bool = False,
                                   refresh_year: bool = False) -> Tuple[int, YearResolution]:
    """
    Calculate and apply the enrollment year for every class of *source*.

    Classes that have no value yet are filled in. Classes whose stored value
    disagrees with the calculation are never overwritten silently: the user is
    shown both values and decides.

    Args:
        source: The source to update.
        parent: Parent widget for the dialog.
        silent: When True, apply the classes that have no value yet and leave
            every conflict untouched, without showing anything. Used for the
            automatic run right after a source is loaded so that a plain load
            does not interrupt the user - unless there is a real conflict.
        refresh_year: Ask the network again instead of using the cached year.

    Returns:
        ``(number_of_classes_updated, year_resolution)``
    """
    # The automatic run must never block the window on a network request. If
    # the year has not been resolved yet (the start-up warm-up has not finished,
    # or it failed) fall back to this computer's clock; the explicit button on
    # the Comparison tab does the full lookup behind a progress dialog.
    if silent and not is_year_cached():
        resolution = resolve_current_year(use_internet=False)
        logger.info("Enrollment years calculated from the system clock "
                    "(the year has not been resolved yet)")
        changes = compute_enrollment_changes(source, resolution.year)
        if not changes:
            return 0, resolution
        conflicts = [change for change in changes if change.conflicts]
        if not conflicts:
            updated = apply_enrollment_changes(source, changes)
            return updated, resolution
        # A conflict still needs the user, so fall through to the dialog.
    else:
        resolution = get_year_resolution(refresh=refresh_year)
    changes = compute_enrollment_changes(source, resolution.year)

    if not changes:
        logger.info("Enrollment years of '%s' are already up to date",
                    getattr(source, "name", "?"))
        return 0, resolution

    conflicts = [change for change in changes if change.conflicts]

    # A plain load only fills in what is missing. Anything that would overwrite
    # an existing value - or a clock disagreement - needs the user.
    if silent and not conflicts and not resolution.sources_disagree:
        updated = apply_enrollment_changes(source, changes)
        logger.info("Set the enrollment year of %d class(es) in '%s'",
                    updated, getattr(source, "name", "?"))
        return updated, resolution

    from PyQt6.QtWidgets import QDialog
    from ui.enrollment_year_dialog import EnrollmentYearDialog

    dialog = EnrollmentYearDialog(changes, resolution,
                                  getattr(source, "name", ""), parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        logger.info("Enrollment year update cancelled for '%s'",
                    getattr(source, "name", "?"))
        return 0, resolution

    selected = dialog.selected_changes()
    updated = apply_enrollment_changes(source, selected)
    logger.info("Set the enrollment year of %d class(es) in '%s'",
                updated, getattr(source, "name", "?"))
    return updated, resolution
