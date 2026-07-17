import logging
import sys


class _LiveStderrHandler(logging.StreamHandler):
    """A StreamHandler that resolves ``sys.stderr`` at emit time, not import time.

    A plain ``StreamHandler(sys.stderr)`` captures the stream *object* once. Two
    problems follow: under pytest that object is the capture stream, which is
    closed before ``atexit`` handlers run — so the session store's shutdown
    logging spews ``--- Logging error ---`` blocks after every test summary; and
    anything else that rebinds ``sys.stderr`` (capture tools, IDE consoles) is
    silently bypassed. Looking the stream up per record writes to whatever
    stderr is current, and drops the record outright when that stream is closed
    (interpreter teardown — there is nowhere left to write).
    """

    def __init__(self):
        super().__init__(stream=sys.stderr)

    def emit(self, record: logging.LogRecord) -> None:
        stream = sys.stderr
        if stream is None or getattr(stream, "closed", False):
            return  # teardown: stderr is gone, drop rather than error-spam
        self.stream = stream
        super().emit(record)


def configure_logging():
    """Configure logging to stderr for MCP compatibility."""
    # Create a logger for the package
    logger = logging.getLogger("browden")

    # Avoid duplicate handlers if called multiple times
    if not logger.handlers:
        logger.setLevel(logging.INFO)

        # Standard formatter: [LEVEL] name: message
        formatter = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")

        # Explicitly stream to stderr (resolved per record — see the handler)
        handler = _LiveStderrHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        # Ensure it doesn't propagate to root logger which might write to stdout
        logger.propagate = False

    return logger


# Global logger instance for the package
logger = configure_logging()
