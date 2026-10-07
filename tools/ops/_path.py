"""子目录脚本的 sys.path 补丁。

为什么不能直接 `from _bootstrap import ROOT`
==========================================
脚本移进子目录后 `sys.path[0]` 变成那个子目录，直接 import 会失效。
所以子目录里放一个 `_path.py`，它把 `tools/` 加进来，再由它去 import
`_bootstrap`。

⚠ 别把它改名成 `_bootstrap.py` —— 那样会 import 到自己，报
  `cannot import name 'ROOT' from partially initialized module`。
"""

import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1]
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from _bootstrap import ROOT  # noqa: E402,F401

__all__ = ["ROOT"]
