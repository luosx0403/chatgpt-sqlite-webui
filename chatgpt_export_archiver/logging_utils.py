from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import TextIO

LOG_LEVELS = ("debug", "info", "warning", "error", "none")

_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_log_level(value: str) -> str:
    level = value.lower()
    if level not in LOG_LEVELS:
        raise ValueError(f"log level must be one of: {', '.join(LOG_LEVELS)}")
    return level


def configure_logging(
    level: str = "warning",
    *,
    stream: TextIO | None = None,
    file_path: Path | None = None,
    json_logs: bool = False,
) -> None:
    """Configure project logging without changing structured CLI stdout."""
    parsed = parse_log_level(level)
    root = logging.getLogger("chatgpt_export_archiver")
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.propagate = False

    if parsed == "none":
        logging.disable(logging.CRITICAL)
        root.setLevel(logging.CRITICAL + 1)
        return

    logging.disable(logging.NOTSET)
    root.setLevel(_LEVEL_MAP[parsed])
    formatter: logging.Formatter
    if json_logs:
        formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")

    targets: list[logging.Handler] = []
    if file_path is not None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        targets.append(logging.FileHandler(file_path, encoding="utf-8"))
    else:
        targets.append(logging.StreamHandler(stream or sys.stderr))

    for handler in targets:
        handler.setLevel(_LEVEL_MAP[parsed])
        handler.setFormatter(formatter)
        root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    if name.startswith("chatgpt_export_archiver"):
        return logging.getLogger(name)
    return logging.getLogger(f"chatgpt_export_archiver.{name}")
