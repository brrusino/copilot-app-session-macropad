# SPDX-License-Identifier: MIT
"""Make the daemon package importable when running pytest from ``daemon/``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
