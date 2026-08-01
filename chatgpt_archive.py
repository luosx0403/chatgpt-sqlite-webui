#!/usr/bin/env python
"""CLI entry point for the local ChatGPT export archiver."""

import time

_PROCESS_ENTRY_STARTED = time.perf_counter()

from chatgpt_export_archiver.cli import main


if __name__ == "__main__":
    raise SystemExit(main(_process_started=_PROCESS_ENTRY_STARTED))
