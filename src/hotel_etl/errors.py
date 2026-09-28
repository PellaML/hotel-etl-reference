"""Errors exposed by the pipeline and CLI."""


class PipelineError(Exception):
    """A failure whose message is safe to show to the caller."""


class SourceError(PipelineError):
    """The upstream response could not be safely or completely read."""


class ValidationError(PipelineError):
    """Configuration or source records violate the documented contract."""


class StorageError(PipelineError):
    """A destination check, write or cleanup step failed."""
