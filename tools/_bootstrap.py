"""把仓库根与 `tools/` 加进 `sys.path`，并**加载 `.env`**。

为什么需要它
============
`python tools/ops/xxx.py` 时 `sys.path[0]` 是 `tools/ops/`，不是仓库根 ——
所以 `from src...` / `from common...` 会 `ModuleNotFoundError`。

🔴 它**同时**负责加载 `.env`（链：`_bootstrap` → `import common.config` →
   `_load_dotenv`）。少了这一步，脚本里 `os.getenv("HR_WORKER_BASE")` 拿到空串，
   于是报"缺少 HR_WORKER_BASE" —— 看起来像**没配**，实际是**没读**。
   这个坑很值得记：报错信息指向的方向完全是错的。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 副作用导入：`common.config` 在 import 时解析 `.env`。
from common import config  # noqa: E402,F401

__all__ = ["ROOT", "config"]
