#!/usr/bin/env python3
import sys
import os

# Ensure the package is found regardless of current working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from codex_manager.cli import main

if __name__ == '__main__':
    sys.exit(main())
