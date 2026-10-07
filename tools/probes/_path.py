"""子目录脚本的 sys.path 补丁（见 `tools/ops/_path.py` 的说明）。"""

import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1]
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from _bootstrap import ROOT  # noqa: E402,F401

__all__ = ["ROOT"]
