from enum import StrEnum


class DocStatus(StrEnum):
    """Document processing status"""

    READY = "ready"
    HANDLING = "handling"
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
