"""State shared by one download attempt; never shared between tasks."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DownloadSession:
    config: dict
    base: Path
    payload: dict
    store: Any
    worker_slot: int
    progress: Any
    scheduler: Any
    meter: Any
    youtube: bool
    test_mode: bool
    oss_mode: bool
    automatic: bool
    catalog: Any
    output_files: dict = field(default_factory=dict)
    existing_files: set = field(default_factory=set)
    current_video: dict = field(default_factory=dict)
    deferred_files: dict = field(default_factory=dict)
    deferred_archives: dict = field(default_factory=dict)
    subtitle_files: dict = field(default_factory=dict)
    failure: Any = None
    failure_phase: str = 'resolve'
    transfer_seconds: float = 0
    billable_bytes: int = 0
