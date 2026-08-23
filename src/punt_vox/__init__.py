"""Vox's public Python API: the voxd clients and the types to call them."""

from __future__ import annotations

from punt_vox.client import SynthesizeResult, VoxClient
from punt_vox.client_errors import (
    VoxdConnectionError,
    VoxdProtocolError,
    VoxdRejectionError,
    VoxError,
)
from punt_vox.client_sync import VoxClientSync
from punt_vox.paths import installed_version
from punt_vox.types_programs import (
    CommandOutcome,
    HealthStatus,
    ProgramStatus,
    ProgramSummary,
    PromptSet,
)
from punt_vox.types_synthesis import SynthesisSpec

__all__ = [
    "CommandOutcome",
    "HealthStatus",
    "ProgramStatus",
    "ProgramSummary",
    "PromptSet",
    "SynthesisSpec",
    "SynthesizeResult",
    "VoxClient",
    "VoxClientSync",
    "VoxError",
    "VoxdConnectionError",
    "VoxdProtocolError",
    "VoxdRejectionError",
    "__version__",
]

# Read from installed metadata; a literal here is what went stale in v5.0.1.
__version__ = installed_version()
