#!/usr/bin/env python3
"""Entry point: python3 sync_menu.py --dry-run"""
import sys

from wmsync.cli import main

if __name__ == "__main__":
    sys.exit(main())
