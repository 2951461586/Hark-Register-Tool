"""原子写盘 —— 先写同目录临时文件，再原子替换。

为什么要单独一个模块
--------------------
这个模式在本项目里有**三处**调用（`ledger` 的台账落盘、`quota` 的计数压缩、
`report` 的导出），而"崩溃安全"这件事的写法只有一种是对的。同一份逻辑复制
三遍，代价不是行数，是**修复只会改一处**：漏改的那一处，症状是"批量跑一半被
Ctrl-C 之后状态文件变成语法不完整的 JSON"，而下一轮读它会**静默降级成
「没有状态」** —— 台账丢了 ⇒ 那批账号**永久失去访问凭据**（密码只存在于
台账里，邮箱是临时邮箱，收不到找回信）。而且不报错。

🔴 临时文件名一律是 `p.name + ".tmp"`（**追加**），不用 `path.with_suffix()`。
   `with_suffix` 会**替换**最后一个后缀：`register_quota.jsonl` 会写成
   `register_quota.tmp` —— 抹掉了"它是什么文件"这个信息，而且同目录下同时
   存在 `a.json` 与 `a.jsonl` 时，两个临时名会**撞车**。

⚠ 本模块**只负责写**。要不要捕获 `OSError` 由调用方决定 —— 三处的策略本就
  不同，且这个不同是有意的：
    * `ledger` —— 让它抛。台账写不成必须让人知道，静默失败代价最高。
    * `quota`  —— 捕获并降级成一条告警。计数是**加速器**，丢了只影响
      下一轮的保守程度，不该让整批任务失败。

本模块不 import `config`，只依赖标准库 —— 它是叶子，不进任何依赖环。
"""

import os
from pathlib import Path

# 临时文件后缀。单独提出来是为了让"追加而不是替换"这条约定有个名字。
TMP_SUFFIX = ".tmp"


def tmp_path_for(path) -> Path:
    """`path` 对应的临时文件路径。

    单独一个函数（而不是内联一行）是为了让"**追加**后缀、不替换"这条约定
    可以被测试直接钉住 —— 它是本模块里唯一**不能**统一错的地方。
    """
    p = Path(path)
    return p.with_name(p.name + TMP_SUFFIX)


def atomic_write_text(path, text: str, *, encoding: str = "utf-8") -> Path:
    """把 `text` 原子地写进 `path`，返回实际落盘路径。

    保证 `path` 要么是**旧内容**、要么是**新内容**，永远不会是半截 ——
    读者（下次运行的合并、或并发进程）看不到中间态。

    ⚠ 单次写入不是原子的：`tmp.write_text()` 自己写一半被杀，`tmp` 是半截，
    但 `path` **没被动过**。这正是要的效果 —— 半截文件带着 `.tmp` 后缀，
    落在 `.gitignore` 里，且不会被当成真数据读。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(p)
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, p)
    return p


def atomic_write_json(path, obj) -> Path:
    """`json.dumps` + `atomic_write_text` 的组合。**必须 `ensure_ascii=False`**。

    ⚠ 不转义非 ASCII 有两个理由：① 报告 / 台账是要给人看的，`\\u4e2d` 那种
    形态没法读；② 转义后字节变长，写盘时的"半截文件"窗口更大。
    """
    import json

    return atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))
