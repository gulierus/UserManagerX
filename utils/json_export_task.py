"""
JSON Export Task
Progress task for exporting source data to encrypted JSON
"""

import logging
import json
from typing import Dict, Any
from datetime import datetime
import os

from utils.progress_tasks import AbstractProgressTask, LogLevel
from utils.encryption import encrypt_file, get_available_methods
from utils.source_serialization import source_to_dict

logger = logging.getLogger(__name__)


class JSONExportTask(AbstractProgressTask):
    """
    Task for exporting source data to encrypted JSON file.

    Steps:
    1. Validate settings
    2. Convert source to JSON
    3. Encrypt and write to file
    4. Verify output
    """

    def __init__(self, source, output_path: str, password: str, method: str):
        super().__init__(
            task_name="JSON Export",
            is_deterministic=True,
            can_pause=False
        )
        self.source = source
        self.output_path = output_path
        self.password = password
        self.method = method
        self.success = False

    def execute(self) -> str:
        """Execute JSON export"""
        total_steps = 4
        current_step = 0

        # Step 1: Validate
        current_step += 1
        self.emit_progress(int(current_step / total_steps * 100), "Validating settings...")
        self.emit_log("Validating export settings", LogLevel.INFO)

        available = get_available_methods()
        if not available.get(self.method, False):
            raise RuntimeError(
                f"Encryption method '{self.method}' is not available. "
                "Install the required library."
            )

        self.check_cancelled()

        # Step 2: Convert source to JSON
        current_step += 1
        self.emit_progress(int(current_step / total_steps * 100), "Converting data to JSON...")
        self.emit_log(
            f"Converting source '{self.source.name}' to JSON",
            LogLevel.INFO
        )

        json_data = self._source_to_json()
        json_str = json.dumps(json_data, indent=2, ensure_ascii=False)
        self.emit_log(f"JSON prepared: {len(json_str)} characters", LogLevel.INFO)

        self.check_cancelled()

        # Step 3: Encrypt and write
        current_step += 1
        self.emit_progress(int(current_step / total_steps * 100), f"Encrypting with {self.method.upper()}...")
        self.emit_log(f"Encrypting output to {self.output_path}", LogLevel.INFO)

        encrypt_file(json_str, self.output_path, self.password, method=self.method)

        self.check_cancelled()

        # Step 4: Verify
        current_step += 1
        self.emit_progress(int(current_step / total_steps * 100), "Verifying output...")

 
        if not os.path.exists(self.output_path):
            raise RuntimeError("Output file was not created")

        file_size = os.path.getsize(self.output_path)
        self.emit_log(f"Output file: {file_size} bytes", LogLevel.INFO)

        self.emit_progress(100, "Export complete")
        self.emit_log("JSON export completed successfully", LogLevel.SUCCESS)
        self.success = True

        persons_count = len(self.source.get_all_persons())
        return f"Exported {persons_count} persons to {self.output_path}"

    def _source_to_json(self) -> Dict[str, Any]:
        """
        Convert the source to a JSON-serialisable dictionary.

        Delegates to :func:`utils.source_serialization.source_to_dict` so the
        export, the export widget and the file reader always agree on the
        structure - and so nothing (home directory, groups, password policy,
        account status) is lost on the way out.
        """
        return source_to_dict(self.source)

    def cleanup(self):
        """Cleanup resources"""
        pass
