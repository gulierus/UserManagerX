"""
AD Sync Task
Progress task for executing Active Directory synchronization
"""

import logging
from typing import Dict, Any, Optional

from utils.progress_tasks import AbstractProgressTask, LogLevel

logger = logging.getLogger(__name__)


class ADSyncTask(AbstractProgressTask):
    """
    Task for executing AD synchronization asynchronously.

    Executes a pre-built sync plan (conflicts already resolved by user).
    """

    def __init__(self, sync_service, sync_plan, conflict_strategy, user_conflict_choices: Dict):
        super().__init__(
            task_name="AD Synchronization",
            is_deterministic=False,
            can_pause=False
        )
        self.sync_service = sync_service
        self.sync_plan = sync_plan
        self.conflict_strategy = conflict_strategy
        self.user_conflict_choices = user_conflict_choices
        self.result = None

    def execute(self) -> str:
        """Execute synchronization"""
        stats_plan = self.sync_plan.statistics
        total_ops = stats_plan.get('total', 0)

        self.emit_log(
            f"Starting synchronization: {total_ops} operations planned "
            f"(create={stats_plan.get('create', 0)}, "
            f"update={stats_plan.get('update', 0)}, "
            f"skip={stats_plan.get('skip', 0)})",
            LogLevel.INFO
        )
        self.emit_progress(-1, "Synchronizing with Active Directory...")

        self.check_cancelled()

        self.result = self.sync_service.execute_sync(
            self.sync_plan,
            self.conflict_strategy,
            self.user_conflict_choices
        )

        stats = self.result.statistics
        successful = stats['successful']
        failed = stats['failed']
        skipped = stats['skipped']

        self.emit_log(
            f"Sync complete: {successful} successful, {failed} failed, {skipped} skipped",
            LogLevel.SUCCESS if failed == 0 else LogLevel.WARNING
        )
        self.emit_progress(100, "Synchronization complete")

        return f"Synchronized {successful} persons ({failed} failed, {skipped} skipped)"

    def cleanup(self):
        """Cleanup resources"""
        pass


class ADPlanTask(AbstractProgressTask):
    """
    Task for creating an AD sync plan (connecting to AD and analyzing).
    """

    def __init__(self, source, server: str, base_dn: str, username: str, password: str):
        super().__init__(
            task_name="AD Connection & Analysis",
            is_deterministic=False,
            can_pause=False
        )
        self.source = source
        self.server = server
        self.base_dn = base_dn
        self.username = username
        self.password = password
        self.sync_plan = None
        self.client = None
        self._ad_client_ctx = None

    def execute(self) -> str:
        """Connect to AD and create sync plan"""
        from services.ad_client import ADClient
        from services.ad_services import SyncPlanner, ConflictDetector

        self.emit_progress(-1, "Connecting to Active Directory...")
        self.emit_log(f"Connecting to {self.server}", LogLevel.INFO)

        self._ad_client_ctx = ADClient(self.server, self.username, self.password)
        self.client = self._ad_client_ctx.__enter__()

        if not self.client.connection:
            raise RuntimeError("Failed to connect to Active Directory")

        self.emit_log("Connected to AD", LogLevel.INFO)
        self.emit_progress(-1, "Analyzing source and building sync plan...")

        self.check_cancelled()

        conflict_detector = ConflictDetector(self.client)
        planner = SyncPlanner(self.client, conflict_detector)
        self.sync_plan = planner.create_sync_plan(self.source, self.base_dn)

        stats = self.sync_plan.statistics
        self.emit_log(
            f"Plan ready: {stats.get('total', 0)} operations "
            f"(create={stats.get('create', 0)}, update={stats.get('update', 0)}, "
            f"skip={stats.get('skip', 0)}, conflicts={stats.get('conflicts', 0)})",
            LogLevel.INFO
        )
        self.emit_progress(100, "Plan ready")
        return "Sync plan created"

    def cleanup(self):
        """Keep client open - will be used by sync task; caller must close."""
        pass

    def close_client(self):
        """Close the AD client when done."""
        if self._ad_client_ctx is not None:
            try:
                self._ad_client_ctx.__exit__(None, None, None)
            except Exception as exc:
                logger.warning("Error while closing the AD connection: %s", exc)
            self._ad_client_ctx = None
            self.client = None
