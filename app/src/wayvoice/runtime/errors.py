"""Shared runtime failures without dependencies on engine implementations."""

from __future__ import annotations

class TranscriptionCancelled(RuntimeError):
    pass


class TranscriptionTimeout(RuntimeError):
    pass


class WorkerUnavailable(RuntimeError):
    """The warm worker could not be reached or started.

    Only this failure is worth retrying through the one-shot runner; an error the
    worker reported itself is a real result.
    """



class WorkerBusyError(RuntimeError):
    """The worker refused admission; retrying in another process is unsafe."""
