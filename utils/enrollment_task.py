"""
Enrollment year background task
===============================

Resolving "what year is it" asks the internet, which can take a few seconds on
a machine with no connection. Doing that on the GUI thread froze the whole
window while the *Enrollment Years* button was working.

The lookup and the calculation now run in an :class:`AbstractProgressTask`, the
same machinery the EduPage import uses, so the user sees a progress dialog they
can watch and cancel. Only the *decision* - which changes to apply - happens on
the GUI thread afterwards, because that is a dialog.
"""

import logging
from typing import List, Optional

from utils.progress_tasks import AbstractProgressTask, LogLevel
from utils.school_year import (
    EnrollmentChange, YearResolution, compute_enrollment_changes,
    resolve_current_year,
)

logger = logging.getLogger(__name__)


class EnrollmentYearTask(AbstractProgressTask):
    """
    Work out the enrollment year of every class of a source.

    Nothing is written: the task only reports what *would* change, so the user
    can confirm it. Applying the result is the caller's job.
    """

    def __init__(self, source, use_internet: bool = True,
                 task_name: str = "Enrollment Years"):
        """
        Args:
            source: The source to inspect.
            use_internet: Whether to ask the internet for the current year.
            task_name: Shown in the progress dialog.
        """
        super().__init__(task_name, is_deterministic=True, can_pause=False)
        self.source = source
        self.use_internet = use_internet

        #: Filled in by :meth:`execute`.
        self.resolution: Optional[YearResolution] = None
        self.changes: List[EnrollmentChange] = []

    def execute(self) -> str:
        """Resolve the year, then compute the per-class changes."""
        self.emit_progress(0, "Determining the current year...")
        self.emit_log("Reading the year from the internet and from this computer",
                      LogLevel.INFO)

        self.resolution = resolve_current_year(use_internet=self.use_internet)
        self.check_cancelled()

        if self.resolution.internet_year is None:
            self.emit_log(
                f"The internet could not be reached ({self.resolution.network_error}); "
                f"using this computer's clock",
                LogLevel.WARNING,
            )
        elif self.resolution.sources_disagree:
            self.emit_log(
                f"The internet says {self.resolution.internet_year}, this computer "
                f"says {self.resolution.system_year} - using the internet value",
                LogLevel.WARNING,
            )
        else:
            self.emit_log("The internet and this computer agree", LogLevel.SUCCESS)

        self.emit_progress(60, f"Current year: {self.resolution.year}")
        self.check_cancelled()

        classes = getattr(self.source, "classes", [])
        self.emit_progress(70, f"Checking {len(classes)} class(es)...")

        self.changes = compute_enrollment_changes(self.source, self.resolution.year)
        self.check_cancelled()

        conflicts = sum(1 for change in self.changes if change.conflicts)
        for change in self.changes:
            self.emit_log(
                f"{change.class_name}: "
                + (f"{change.old_year} → {change.new_year}" if change.conflicts
                   else f"enrolled {change.new_year}"),
                LogLevel.WARNING if change.conflicts else LogLevel.INFO,
            )

        self.emit_progress(100, "Done")

        if not self.changes:
            return "Every class already has the right enrollment year"
        return (f"{len(self.changes)} class(es) to update"
                + (f", {conflicts} of them conflicting" if conflicts else ""))

    def cleanup(self):
        """Nothing to release."""


class YearWarmupTask(AbstractProgressTask):
    """
    Resolve the current year once, in the background, at start-up.

    Loading a source calculates enrollment years, and that needs the current
    year. Doing the lookup at that moment would block the window; doing it here
    means the value is already cached by the time the user loads anything.
    """

    def __init__(self):
        super().__init__("Determining the current year", is_deterministic=False,
                         can_pause=False)

    def execute(self) -> str:
        from utils.enrollment_service import get_year_resolution

        # Not refresh=True: if something already resolved the year there is
        # nothing to warm up, and forcing a second lookup would make a network
        # request the application does not need.
        resolution = get_year_resolution()
        return resolution.describe()

    def cleanup(self):
        """Nothing to release."""
