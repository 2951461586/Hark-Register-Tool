"""`tools/` 下**纯函数**的用例 —— 零网络、零副作用。

为什么 `tools/` 也要有测试
--------------------------
这些函数看着琐碎，但它们各自**编码了一个决定**，而错了不会报错、只会静默改变
工具的行为：

| 函数 | 它决定的 |
|---|---|
| `recover_accounts.needs_recovery` | 哪些账号算「已激活但链没走完」——选错就会漏补或白补 |
| `repair_stages._parse_sets` | `--set 阶段=值` 怎么解析——错了会写坏台账 |
| `verify_accounts._verifiable` | 哪些记录**算进存活率的分母**——错了会得出"你的账号全挂了" |
| `verify_accounts._pick` | 覆盖率的分子/分母——错了会报"全绿"但只覆盖了一小部分 |

🔴 最后两条是同一个失败形态：**假阴性 / 假绿**。本项目对它们的容忍度是零，
   所以哪怕工具脚本也该被钉住。

⚠ `sys.path` 那一小段是**必须**的，不是偷懒：`tools/<子目录>/*.py` 里都有
  `from _path import ROOT`，而那个模块的设计就是"把 tools/ 加进 sys.path"
  （见 `tools/ops/_path.py` 的 docstring）。所以要先让子目录可导入。
  三个子目录各有一份 `_path.py`，名字相同 —— 先导入的那份会留在 `sys.modules`
  里给后一个复用。它们**行为完全一致**（都是同一个 `_bootstrap` 的薄壳），
  所以这个复用是安全的；换名字反而会破坏"每个子目录自带一份"的约定。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("tools/data", "tools/ops", "tools/probes"):
    _p = str(ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import recover_accounts  # noqa: E402
import repair_stages  # noqa: E402
import verify_accounts  # noqa: E402


def rec(
    status="failed",
    activate="",
    password="Pw#1",
    email="a@example.com",
    stages=None,
    **kw,
):
    """造一条台账记录。

    ⚠ `stages` 是一个**显式参数**，不是通过 `**{...}` 传的：后者在类型上
      会被当成"往 `activate` / `password` 里塞 dict"（实测被静态检查报 8 条）。
    """
    d = {
        "email": email,
        "password": password,
        "status": status,
        "stages": stages
        if stages is not None
        else {"mailbox": "ok", "register": "ok", "activate": activate},
    }
    d.update(kw)
    return d


# ── recover_accounts.needs_recovery ───────────────────────────────
def test_needs_recovery_only_for_activated_incomplete():
    assert recover_accounts.needs_recovery(rec(activate="ok(mail+verify)"))
    assert recover_accounts.needs_recovery(rec(activate="ok"))


def test_needs_recovery_skips_success_and_never_activated():
    assert not recover_accounts.needs_recovery(rec(status="success", activate="ok"))
    assert not recover_accounts.needs_recovery(rec(activate=""))
    assert not recover_accounts.needs_recovery(rec(activate="failed"))
    assert not recover_accounts.needs_recovery(rec(activate="skipped"))


def test_needs_recovery_tolerates_broken_records():
    """台账是人工可读的 JSON —— 一条坏记录不该让工具崩掉。"""
    assert not recover_accounts.needs_recovery({})
    assert not recover_accounts.needs_recovery({"email": ""})
    assert not recover_accounts.needs_recovery({"email": "a@b.c", "stages": None})
    assert not recover_accounts.needs_recovery(None)


# ── repair_stages._parse_sets ─────────────────────────────────────
def test_parse_sets_basic():
    assert repair_stages._parse_sets(["a=b", "c=d"]) == {"a": "b", "c": "d"}


def test_parse_sets_keeps_empty_value():
    """`password=` 是**合法**输入（表示"没跑到"），不能当成缺值丢掉。"""
    assert repair_stages._parse_sets(["password="]) == {"password": ""}


def test_parse_sets_does_not_split_on_extra_equals():
    """🔴 用 `partition` 而不是 `split`：阶段值里可能有 `=`（注记里常带）。"""
    assert repair_stages._parse_sets(["password=ok(a=b)"]) == {"password": "ok(a=b)"}


def test_parse_sets_rejects_garbage():
    with pytest.raises(SystemExit):
        repair_stages._parse_sets(["no-equals-sign"])


def test_parse_sets_empty_is_empty_dict():
    assert repair_stages._parse_sets([]) == {}
    assert repair_stages._parse_sets(None) == {}


# ── verify_accounts._verifiable / _pick ───────────────────────────
def test_verifiable_requires_the_password_step_to_have_succeeded():
    """🔴 判据是「密码真的设上去了」，不是「有 password 字段」。

    注册失败的那些记录里 `password` 是**生成但从未提交**的，
    把它们算进分母会得到一堆 `INVALID_EMAIL_OR_PASSWORD` ——
    那不是"账号坏了"，是"这个账号从来没成过"。
    """
    assert verify_accounts._verifiable(rec(status="success", activate="ok"))
    assert verify_accounts._verifiable(rec(status="failed", stages={"password": "ok"}))
    # 有 password 字段、但密码阶段没成功 ⇒ 不算可复核
    assert not verify_accounts._verifiable(
        rec(
            status="failed", password="generated-but-never-submitted", stages={"password": "failed"}
        )
    )
    assert not verify_accounts._verifiable({"email": "a@b.c", "password": "x", "stages": None})


def test_pick_reports_three_numbers_including_excluded():
    """覆盖率的分子/分母必须一起出来 —— 只给分子就是"假绿"的温床。"""
    records = [
        rec(status="success", activate="ok"),
        rec(status="failed", stages={"password": "ok"}),
        rec(status="failed", password="never-submitted", stages={"password": "failed"}),
        rec(status="skipped", password="", stages={"password": ""}),
    ]
    todo, total, excluded = verify_accounts._pick(records, 0, 0)
    assert total == 2, "可复核的只有两条（成功 + 密码已设上）"
    assert len(todo) == 2
    assert excluded == 1, "密码没设上的那条要被明确排除并计数"


def test_pick_honours_limit_and_offset():
    records = [rec(status="success", activate="ok", email=f"{i}@example.com") for i in range(5)]
    todo, total, _ = verify_accounts._pick(records, 2, 1)
    assert total == 5 and [r["email"] for r in todo] == ["1@example.com", "2@example.com"]
