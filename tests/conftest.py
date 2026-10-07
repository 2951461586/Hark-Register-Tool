"""pytest 夹具。

🔴 每个用例都必须在**隔离**的 state / ledger 目录里跑 —— 这是本仓库最容易
   造成"测试污染真实数据"的地方，而后果是不可逆的：
   `ledger/` 里是账号的**唯一副本**（临时邮箱收不到找回信，密码只存在那里）。
   一个不小心写真的台账的用例，能在 `--overwrite` 路径上把整本账号覆盖掉。

   所以 `isolate_state` 是 **autouse** 的：不靠每个用例自己记得加。
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """把配额状态文件与台账目录都指到 `tmp_path`。

    ⚠ 台账目录的隔离是**换掉 `ledger.LEDGER_DIR` 这个模块级常量**，而不是
      改环境变量 —— `ledger` 的设计里没有 `HR_LEDGER_DIR`（台账位置是**算出来的**，
      不是配置出来的：`Path(__file__).parents[1]`）。如果哪天有人给它加了个
      环境变量开关，这个夹具要跟着改，否则隔离会**静默失效**。
    """
    from src import ledger

    monkeypatch.setenv("HR_QUOTA_STATE", str(tmp_path / "quota.jsonl"))
    monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
    return tmp_path


@pytest.fixture
def sample_ledger():
    """一本形状真实、**值全编造**的台账（email 用 RFC 2606 保留域）。

    为什么需要它：合并类用例需要"一本有历史的台账"当基线，而真实台账
    （含凭据）不在仓库里 ⇒ CI 上读到的是 `[]` ⇒ 每个用例以各自的形态给出
    **无意义的结论**（Intern-Register-Tool 实测：空基线炸出四种形态，
    其中两种是"碰巧通过"的假绿）。所以基线必须是**样本**，不是真实数据。
    """
    return [
        {
            "email": "user-a@example.com",
            "password": "Pw#aaaaaaaaaaaa",
            "user_id": "uid-a",
            "session_token": "tok-a",
            "status": "success",
            "error": "",
            "error_kind": "",
            "stages": {"mailbox": "ok", "register": "ok", "activate": "ok"},
            "timings": {"mailbox_ms": 100},
            "created_at": "2026-01-01 00:00:00",
        },
        {
            "email": "user-b@example.com",
            "password": "",
            "status": "failed",
            "error": "等 120s 没等到 magic link 邮件",
            "error_kind": "rejected",
            "stages": {},
            "timings": {},
            "created_at": "2026-01-01 00:00:01",
        },
        {"email": "", "status": "skipped", "error": "本地配额", "error_kind": "quota_guard"},
    ]


@pytest.fixture
def write_ledger(tmp_path):
    """把一份记录写成台账快照，返回路径。"""

    def _write(records, name="accounts-20260101-000000.json"):
        p = tmp_path / "ledger" / "runs" / "2026-01-01" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        return p

    return _write
