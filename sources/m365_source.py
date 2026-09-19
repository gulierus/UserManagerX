"""
Microsoft 365 as a data source
==============================

Version 26, point 21 a: read the groups and teams of a Microsoft 365 tenant and
turn the chosen ones into school classes.

What happens, in order:

1. The user picks a sign-in method and fills in the fields
   (:class:`~ui.m365_connection_widget.M365ConnectionWidget`).
2. :class:`~utils.m365_tasks.M365LoadGroupsTask` signs in and reads every group
   with its owners and members - in a worker thread, behind the progress
   dialog, so the window stays alive on a tenant with hundreds of groups.
3. :class:`~ui.m365_import_wizard.M365ImportWizard` shows the two steps:
   choose the groups, then name the classes.
4. The classes are built and the source is added.  Its enrollment years are
   calculated automatically, because :meth:`SourceManager.add_source` does that
   for every source regardless of where it came from.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from models import Class, Person, Source
from models_m365 import M365Group, M365GroupSelection
from services.graph_compat import GRAPH_AVAILABLE, graph_unavailable_message
from services.m365_auth import M365Credentials
from ui.m365_connection_widget import M365ConnectionWidget
from ui.m365_import_wizard import M365ImportWizard
from utils.source_naming import unique_source_name

logger = logging.getLogger(__name__)


def build_person(member, class_name: str) -> Optional[Person]:
    """
    Turn one Microsoft 365 account into a :class:`~models.Person`.

    Args:
        member: The account, an :class:`~models_m365.M365Member`.
        class_name: The class the user named in step 2.

    Returns:
        The person, or ``None`` when the account has no usable name at all -
        a record the application cannot match against anything is worse than
        no record.
    """
    first_name, last_name = member.split_name()
    if not first_name and not last_name:
        return None

    person = Person(
        first_name=first_name,
        last_name=last_name,
        class_name=class_name,
        ad_email=member.mail or member.user_principal_name or None,
        account_enabled=member.account_enabled,
    )

    # Microsoft 365 facts live in metadata rather than in the typed Active
    # Directory fields: the two directories are separate, and a person can be
    # in both with different values.
    person.metadata['m365_object_id'] = member.object_id
    person.metadata['m365_user_principal_name'] = member.user_principal_name
    person.metadata['m365_display_name'] = member.display_name
    person.metadata['m365_mail'] = member.mail
    if member.is_owner:
        person.metadata['m365_was_group_owner'] = True

    return person


def build_source(selections: List[M365GroupSelection], name: str,
                 origin_info: Optional[dict] = None) -> Source:
    """
    Build a source from the wizard's result.

    Args:
        selections: One per group the user chose, carrying the class name.
        name: The source name.
        origin_info: What to record about where the data came from - the
            tenant, the sign-in method and the account.  Never a secret.

    Returns:
        The source, read-only because it mirrors a directory this application
        did not author.
    """
    source = Source(name=name, source_type="microsoft_365", readonly=True)

    for key, value in (origin_info or {}).items():
        source.set_source_info(key, value)

    skipped = 0
    for selection in selections:
        class_name = (selection.class_name or "").strip()
        if not class_name:
            continue

        school_class = Class(name=class_name)
        for member in selection.people():
            person = build_person(member, class_name)
            if person is None:
                skipped += 1
                continue
            school_class.add_person(person)

        school_class.metadata['m365_group_id'] = selection.group.object_id
        school_class.metadata['m365_group_name'] = selection.group.display_name
        source.add_class(school_class)

    source.set_source_info('group_count', len(source.classes))
    if skipped:
        source.set_source_info('accounts_without_a_name', skipped)
        logger.info("%d Microsoft 365 account(s) had no usable name and were "
                    "not imported", skipped)

    return source


class MicrosoftM365SourceWidget(QWidget):
    """The "Microsoft 365 From Web" page of the "1. Data Sources" tab."""

    def __init__(self, source_manager):
        super().__init__()
        self.source_manager = source_manager
        #: Remembered between loads so the user does not retype the tenant.
        self._last_credentials: Optional[M365Credentials] = None
        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        info = QLabel(
            "Read the groups and teams of a Microsoft 365 tenant and turn the "
            "ones that are school classes into a source. You choose which "
            "groups to use and what each class is called."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #aaa; font-style: italic;")
        layout.addWidget(info)

        self.connection = M365ConnectionWidget()
        layout.addWidget(self.connection)

        buttons = QHBoxLayout()
        self.load_button = QPushButton("Connect and Load Groups")
        self.load_button.clicked.connect(self.on_load)
        buttons.addWidget(self.load_button)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self.status_label)

        if not GRAPH_AVAILABLE:
            # Say it here rather than when the button is pressed: the user can
            # then install the packages before filling anything in.
            self.load_button.setEnabled(False)
            self.status_label.setStyleSheet("color: #e6a93c; font-size: 11px;")
            self.status_label.setText(graph_unavailable_message())

        layout.addStretch()

    # -- loading ----------------------------------------------------------

    def on_load(self) -> None:
        """Sign in, read the groups, run the wizard, add the source."""
        from utils.m365_tasks import M365LoadGroupsTask
        from utils.progress_dialog import ProgressDialog

        credentials = self.connection.credentials()
        problems = credentials.problems()
        if problems:
            QMessageBox.warning(self, "Missing Information", "\n".join(problems))
            return

        task = M365LoadGroupsTask(credentials)
        progress = ProgressDialog(task, self)
        progress.start_task()
        progress.exec()

        if progress.was_cancelled():
            logger.info("Microsoft 365 load cancelled by the user")
            self._close_client(task)
            return

        if not task.groups:
            self._close_client(task)
            QMessageBox.warning(
                self, "No Groups",
                "No groups or teams were returned. Either the tenant has "
                "none, or this account cannot see them - check the "
                "Group.Read permissions."
            )
            return

        self._last_credentials = credentials
        try:
            self._run_wizard(task.groups, task.origin_info, task.unreadable)
        finally:
            # The client belongs to the worker thread, which has finished; the
            # session is of no further use here.
            self._close_client(task)

    @staticmethod
    def _close_client(task) -> None:
        """Release the session the task opened, whatever happened."""
        client = getattr(task, "client", None)
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.debug("Closing the Microsoft 365 session failed",
                             exc_info=True)
            task.client = None

    def _run_wizard(self, groups: List[M365Group], origin_info: dict,
                    unreadable: List[str]) -> None:
        """Show the two steps and build the source from the answer."""
        if unreadable:
            QMessageBox.warning(
                self, "Some Groups Could Not Be Read",
                f"The members of {len(unreadable)} group(s) could not be read, "
                f"so those groups would produce empty classes:\n\n"
                + "\n".join(unreadable[:10])
                + ("\n..." if len(unreadable) > 10 else "")
            )

        wizard = M365ImportWizard(groups, self)
        if wizard.exec() != QDialog.DialogCode.Accepted:
            logger.info("Microsoft 365 import cancelled in the wizard")
            return

        selections = wizard.result_selections()
        default_name = self._default_source_name(origin_info)

        name = self._ask_for_source_name(default_name)
        if not name:
            return

        source = build_source(selections, name, origin_info)
        if not source.classes:
            QMessageBox.warning(
                self, "Nothing to Import",
                "None of the selected groups produced a class."
            )
            return

        self.source_manager.add_source(source)

        people = len(source.get_all_persons())
        self.status_label.setText(
            f"Loaded {people} person(s) in {len(source.classes)} class(es) "
            f"into '{source.name}'."
        )
        QMessageBox.information(
            self, "Success",
            f"Loaded {people} person(s) from {len(source.classes)} "
            f"Microsoft 365 group(s) into '{source.name}'."
        )
        logger.info("Successfully loaded Microsoft 365 source: %s", source.name)

    def _default_source_name(self, origin_info: dict) -> str:
        """A readable, unused default name built from the tenant."""
        tenant = (origin_info.get('tenant_domain') or
                  origin_info.get('tenant_id') or "")
        base = f"M365 {tenant}".strip() if tenant else "Microsoft 365"
        return unique_source_name(base, self.source_manager.get_source_names())

    def _ask_for_source_name(self, default_name: str) -> Optional[str]:
        """
        Let the user confirm or change the source name.

        The same behaviour as every other loader: a name that is taken asks
        whether to replace, and Yes actually replaces - two sources with one
        name are unreachable through every lookup by name.
        """
        replaced = None

        while True:
            name, ok = QInputDialog.getText(
                self, "Name Source",
                "Enter a name for this Microsoft 365 source:",
                QLineEdit.EchoMode.Normal, default_name,
            )
            if not ok:
                return None

            name = (name or "").strip()
            if not name:
                QMessageBox.warning(self, "Invalid Name",
                                    "Source name cannot be empty")
                continue

            existing = self.source_manager.get_source_by_name(name)
            if existing is not None:
                reply = QMessageBox.question(
                    self, "Duplicate Name",
                    f"Source '{name}' already exists. Replace it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.No:
                    default_name = name
                    continue
                replaced = existing

            if replaced is not None:
                self.source_manager.remove_source(replaced)
                logger.info("Replaced existing source: %s", name)
            return name
