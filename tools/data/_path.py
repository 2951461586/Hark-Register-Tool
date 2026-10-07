"""子目录脚本的 sys.path 补丁（见 `tools/ops/_path.py` 的说明）。

⚠ 每个子目录都要有自己的一份：`python tools/data/xxx.py` 时 `sys.path[0]` 是
  `tools/data/`，所以 `from _path import ROOT` 找的是**同目录**的 `_path.py`。
  漏了这一份，脚本会以 `ModuleNotFoundError: No module named '_path'` 收场 ——
  一个看起来像"环境没配好"、实际是"少了一个文件"的报错（实测踩到）。
"""

import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1]
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from _bootstrap import ROOT  # noqa: E402,F401

__all__ = ["ROOT"]
