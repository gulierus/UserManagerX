"""
School year and enrollment year
===============================

A class designation carries its grade (``6.A`` -> 6th grade, ``IX.B`` -> 9th).
Combined with the current school year that gives the **enrollment year**: the
calendar year in which that class started the first grade.

    6.A during the school year 2025/2026  ->  enrolled in 2020

Determining "now"
-----------------
The current year is taken from two independent sources:

* the **internet** - the ``Date`` header of a plain HTTPS request, which needs
  no API key and no JSON parsing;
* the **operating system** clock.

If the two disagree the caller is told, so a machine whose clock is wrong (a
flat CMOS battery sets it back years, and school computers are old) cannot
silently produce a whole set of wrong enrollment years. Without a working
connection the operating system time is used on its own.

School year boundary
--------------------
A school year is assumed to start on **1 September**, so everything from
September to December belongs to the school year that starts in that calendar
year, and January to August belongs to the one that started the year before.
The boundary is a module constant, so a school with a different calendar only
has to change it in one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

#: First month of a new school year (9 = September).
SCHOOL_YEAR_START_MONTH = 9

#: Hosts asked for the current date, in order. Only the ``Date`` response
#: header is used, so no service-specific format has to be parsed.
TIME_SOURCES = (
    "https://www.google.com",
    "https://www.cloudflare.com",
    "https://www.seznam.cz",
)

#: Seconds to wait for the internet before giving up on it.
NETWORK_TIMEOUT = 2.0

#: Plausible range for a grade parsed out of a class name.
MIN_GRADE = 1
MAX_GRADE = 13


@dataclass
class YearResolution:
    """
    The current year, and where it came from.

    Attributes:
        system_year: The year according to the operating system.
        internet_year: The year according to the internet, or ``None``.
        year: The year that should be used.
        network_error: Why the internet could not be reached, if it could not.
    """

    system_year: int
    internet_year: Optional[int] = None
    year: int = 0
    network_error: Optional[str] = None

    @property
    def sources_disagree(self) -> bool:
        """True when the internet and the system clock report different years."""
        return (self.internet_year is not None
                and self.internet_year != self.system_year)

    @property
    def used_internet(self) -> bool:
        """True when the year came from the internet."""
        return self.internet_year is not None

    def describe(self) -> str:
        """One-line summary for a log entry or a status label."""
        if self.internet_year is None:
            return (f"{self.year} (operating system; internet unavailable: "
                    f"{self.network_error})")
        if self.sources_disagree:
            return (f"{self.year} (internet {self.internet_year} vs. operating "
                    f"system {self.system_year} - they disagree)")
        return f"{self.year} (internet and operating system agree)"


def get_system_year(now: Optional[datetime] = None) -> int:
    """Return the current year according to the operating system."""
    return (now or datetime.now()).year


def get_internet_year(timeout: float = NETWORK_TIMEOUT) -> Tuple[Optional[int], Optional[str]]:
    """
    Read the current year from the internet.

    Args:
        timeout: Seconds to wait per host.

    Returns:
        ``(year, None)`` on success, ``(None, reason)`` when no host answered.
    """
    import time
    import urllib.error
    import urllib.request

    last_error = "no time source answered"
    # Hard budget across ALL hosts. Trying three unreachable hosts at the full
    # per-host timeout would freeze the window for six seconds every time a
    # source is loaded on a machine without internet access.
    deadline = time.monotonic() + timeout * 1.5

    for url in TIME_SOURCES:
        if time.monotonic() >= deadline:
            last_error = f"{last_error} (gave up after {timeout * 1.5:.1f}s)"
            break
        try:
            remaining = max(0.2, deadline - time.monotonic())
            request = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(request, timeout=min(timeout, remaining)) as response:
                header = response.headers.get("Date")
            if not header:
                last_error = f"{url} sent no Date header"
                continue

            stamp = parsedate_to_datetime(header)
            if stamp.tzinfo is None:                 # pragma: no cover - defensive
                stamp = stamp.replace(tzinfo=timezone.utc)
            # Compare in local time: a request just before midnight UTC can
            # otherwise look like a different year than the system clock.
            return stamp.astimezone().year, None

        except urllib.error.URLError as exc:
            last_error = f"{url}: {exc.reason}"
        except Exception as exc:
            last_error = f"{url}: {exc}"

    logger.info("Could not read the year from the internet: %s", last_error)
    return None, last_error


def resolve_current_year(use_internet: bool = True,
                         timeout: float = NETWORK_TIMEOUT,
                         now: Optional[datetime] = None) -> YearResolution:
    """
    Determine the current year from both sources.

    Args:
        use_internet: Set to False to skip the network entirely.
        timeout: Network timeout in seconds.
        now: Override the system clock (for tests).

    Returns:
        A :class:`YearResolution`. ``year`` prefers the internet, because a
        wrong system clock is the failure this exists to catch.
    """
    system_year = get_system_year(now)

    if not use_internet:
        return YearResolution(system_year=system_year, year=system_year,
                              network_error="not requested")

    internet_year, error = get_internet_year(timeout)
    resolution = YearResolution(
        system_year=system_year,
        internet_year=internet_year,
        network_error=error,
    )
    resolution.year = internet_year if internet_year is not None else system_year

    if resolution.sources_disagree:
        logger.warning("Year mismatch: internet says %s, this computer says %s",
                       internet_year, system_year)
    return resolution


def school_year_start(year: int, now: Optional[datetime] = None) -> int:
    """
    Return the calendar year the *current* school year began in.

    Args:
        year: The current calendar year.
        now: Override the system clock (for the month).

    Returns:
        ``year`` from September onwards, ``year - 1`` before that.
    """
    month = (now or datetime.now()).month
    return year if month >= SCHOOL_YEAR_START_MONTH else year - 1


def enrollment_year_for_grade(grade: int, current_year: int,
                              now: Optional[datetime] = None) -> int:
    """
    Return the year a class of the given grade started the first grade.

    Args:
        grade: The grade the class is in now (1-13).
        current_year: The current calendar year.
        now: Override the system clock (for the school-year boundary).

    Returns:
        The enrollment year.

    Raises:
        ValueError: If *grade* is outside the plausible range.
    """
    if not MIN_GRADE <= grade <= MAX_GRADE:
        raise ValueError(
            f"Grade {grade} is outside the plausible range "
            f"{MIN_GRADE}-{MAX_GRADE}"
        )
    return school_year_start(current_year, now) - (grade - 1)


def enrollment_year_for_class(class_name: str, current_year: int,
                              now: Optional[datetime] = None) -> Optional[int]:
    """
    Work out the enrollment year from a class name.

    Understands every form the class-name parser does, so ``6.A``, ``IX.B``,
    ``9 C`` and ``Blue class 6.A`` all yield a grade.

    Args:
        class_name: The class designation.
        current_year: The current calendar year.
        now: Override the system clock.

    Returns:
        The enrollment year, or ``None`` when the name carries no usable grade.
    """
    from utils.class_name_utils import parse_class_name

    parts = parse_class_name(class_name)
    if not parts.has_numeral:
        return None

    try:
        return enrollment_year_for_grade(parts.numeral_value, current_year, now)
    except ValueError:
        logger.debug("Class %r has grade %s, which is out of range",
                     class_name, parts.numeral_value)
        return None


@dataclass
class EnrollmentChange:
    """
    One class whose enrollment year would change.

    Attributes:
        class_name: The class.
        old_year: What it holds now (``None`` when unset).
        new_year: What the calculation produced.
    """

    class_name: str
    old_year: Optional[int]
    new_year: Optional[int]

    @property
    def is_new(self) -> bool:
        """True when the class had no enrollment year yet."""
        return self.old_year is None

    @property
    def conflicts(self) -> bool:
        """True when a value was already set and the calculation disagrees."""
        return self.old_year is not None and self.old_year != self.new_year


def compute_enrollment_changes(source, current_year: int,
                               now: Optional[datetime] = None) -> List[EnrollmentChange]:
    """
    Work out what the calculation would do to every class of a source.

    Nothing is written - the caller decides, because a class that already has
    a different value needs the user's confirmation.

    Args:
        source: The source to inspect.
        current_year: The current calendar year.
        now: Override the system clock.

    Returns:
        One :class:`EnrollmentChange` per class whose value would change.
    """
    changes = []
    for cls in getattr(source, "classes", []):
        new_year = enrollment_year_for_class(cls.name, current_year, now)
        if new_year is None:
            continue
        old_year = getattr(cls, "enrollment_year", None)
        if old_year != new_year:
            changes.append(EnrollmentChange(cls.name, old_year, new_year))
    return changes


def apply_enrollment_changes(source, changes: List[EnrollmentChange]) -> int:
    """
    Write the given changes onto the source's classes.

    Args:
        source: The source to modify.
        changes: The changes to apply.

    Returns:
        How many classes were updated.
    """
    wanted = {change.class_name: change.new_year for change in changes}
    updated = 0
    for cls in getattr(source, "classes", []):
        if cls.name in wanted:
            cls.enrollment_year = wanted[cls.name]
            updated += 1
    return updated
