"""Logging to stdout in a single-line format that reads well in journald."""

import logging
import sys

# journald adds its own timestamp, so the line starts with the level.
LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"
LOG_FORMAT_WITH_TIME = "%(asctime)s " + LOG_FORMAT


def setup_logging(level: str = "INFO") -> None:
    """Send all logging to stdout; the timestamp is omitted when stdout is not a terminal."""
    log_format = LOG_FORMAT_WITH_TIME if sys.stdout.isatty() else LOG_FORMAT
    handler = logging.StreamHandler(sys.stdout)
    logging.basicConfig(level=level, format=log_format, handlers=[handler], force=True)
