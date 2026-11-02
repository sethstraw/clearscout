"""Read every enabled source once and report what each one answered.

The sweep asks the catalog which boards are due, reads them through the network
seam, stages bounded reviewable rows, holds verified foreign locations out of
Pending while still refreshing what they match, and records an outcome per
source.
"""

from tailorcv.application.discovery.scan_outcome import INCOMPLETE_READ_NOTE, settle_drained_scan
from tailorcv.application.discovery.scan_sweep.models import (
    BoardReadOutcome,
    DueBoards,
    SweepBoard,
)
from tailorcv.application.discovery.scan_sweep.recording import RecordedScan, ScanRecorder
from tailorcv.application.discovery.scan_sweep.rescreen import (
    PendingQueueRescreen,
    PendingQueueRescreenOutcome,
    pending_row_failure,
)
from tailorcv.application.discovery.scan_sweep.sweep import (
    SCAN_LANES,
    SOURCE_STAGE_LIMIT,
    BoardSweep,
)

__all__ = [
    "INCOMPLETE_READ_NOTE",
    "SCAN_LANES",
    "SOURCE_STAGE_LIMIT",
    "BoardReadOutcome",
    "BoardSweep",
    "DueBoards",
    "PendingQueueRescreen",
    "PendingQueueRescreenOutcome",
    "RecordedScan",
    "ScanRecorder",
    "SweepBoard",
    "pending_row_failure",
    "settle_drained_scan",
]
