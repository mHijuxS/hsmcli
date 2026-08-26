#!/usr/bin/env python3
"""Module entry point: python -m hsmcli"""

import sys

from .cli import main

if __name__ == "__main__":
    # sys.exit, not a bare call: without it `python -m hsmcli` always exited
    # 0, so every failure looked like a success to a script. The console
    # script gets this for free from setuptools' wrapper; this path didn't.
    sys.exit(main())
