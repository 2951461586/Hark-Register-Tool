#!/usr/bin/env python3
"""补做「已激活但链没走完」的账号：重登 + 只读校验，并把结果写回台账。

什么样的账号会被选中
====================
`stages.activate` 以 `ok` 开头（= 邮箱已验证、账号已在 hark 上建好）
**且** `status != success`。典型成因都不是账号坏了，而是**链在收尾处被打断**：

  · 登录撞上 Cloudflare 的 429（出口维度，过一会儿就好）
  · 一次 TLS 抖动（`SSLEOFError`）
  · 当时密码没设上（更早的一个 bug）

所以它们的修法也不同，工具分两条路：

  **有密码** → `sign_in` → `verify_app`
  **无密码** → magic-link 补设密码 → `sign_in` → `verify_app`

🔴 两条路都必须先过 `account-discovery`
--------------------------------------
`exists:false` 的账号**再登也登不上**，而直接去登会得到
`INVALID_EMAIL_OR_PASSWORD` —— 那看起来像"密码错了"，实际是"这个号不存在"。
一个便宜的一次性查询就能把两者分开，所以它排在第一位。

🔴 校验用的是**注册链同一个函数**（`pipeline.verify_app`）
--------------------------------------------------------
否则"补完了"和"注册成功了"会变成两个不同的标准，而台账里那两行看不出区别。

🔴 登录要过**按出口**的限速闸门
------------------------------
实测 `sign-in/email` 是 3 次/10s 且**按 IP**。补登一批账号时若不加闸门，
会亲手把 429 造出来 —— 而那正是这批账号当初失败的原因（假阴性套假阴性）。

用法
====
    python tools/data/recover_accounts.py                    # dry run（只看要做什么）
    python tools/data/recover_accounts.py --apply            # 真跑 + 写台账
    python tools/data/recover_accounts.py --apply --limit 2  # 只做前 2 个

退出码：0 = 全部补完；1 = 有账号没补成；2 = 没有待补的账号
"""

from __future__ import annotations

import argparse
import copy
import sys
import time
from datetime import datetime

from _path import ROOT  # noqa: F401  （副作用：sys.path + 加载 .env）

from common import config  # noqa: E402
from src import ledger, pipeline, redact  # noqa: E402
from src.hark import CODE_PASSWORD_ALREADY_SET, HarkClient, HarkError  # noqa: E402
from src.proxypool import SlotPool, SlotPoolEmpty  # noqa: E402
from src.tempmail import TempMailClient, now_ms  # noqa: E402

# 选中口径：**邮箱已验证**（账号真的存在）但整链没走完。
STAGE_ACTIVATE_OK = "ok"


def needs_recovery(rec: dict) -> bool:
    """这条记录该不该补。**判据写在函数里**，方便被测试直接钉住。"""
    if not isinstance(rec, dict) or not rec.get("email"):
        return False
    if (rec.get("status") or "") == "success":
        return False
    stages = rec.get("stages") or {}
    return str(stages.get("activate") or "").startswith(STAGE_ACTIVATE_OK)


def recover_one(rec: dict, pool: SlotPool, *, dry: bool = False) -> tuple[dict, str]:
    """补一个账号。返回 `(新的记录, 结论文本)`；**不改台账**。

    ⚠ 返回的是**二元组**而不是单个 dict —— 结论要单独回给人看，
      而“补成了没有”不能靠猜（看 `status` 会把“本来就没戏”与“刚补完”
      混成同一个值）。
    """
    email = rec["email"]
    out = copy.deepcopy(rec)
    note = ""

    if dry:
        has_pw = bool(rec.get("password"))
        return out, "有密码 → 直接重登" if has_pw else "无密码 → 走 magic link 补设密码"

    slot = None
    try:
        slot = pool.acquire()
    except SlotPoolEmpty as ex:
        return out, f"✗ 借不到出口：{ex}"
    try:
        client = HarkClient(proxy=slot)
        # ① 账号还在不在 —— 一次便宜的只读查询，把"号不存在"与"密码不对"分开。
        try:
            disc = client.discover(email)
        except HarkError as ex:
            return out, f"✗ discovery 失败：{ex}"
        if not disc.get("exists"):
            return out, "✗ hark 上已无此账号（exists:false）—— 补不了"

        # ② 拿到一条可用会话。无密码的先补设一个。
        if not rec.get("password"):
            mail = TempMailClient()
            sent_at = now_ms()
            try:
                client.request_magic_link(email)
            except HarkError as ex:
                return out, f"✗ 请求 magic link 失败：{ex}"
            link = mail.wait_for_link(email, "magic-link/verify", since_ts=sent_at)
            if not link:
                why = mail.last_error or "邮件没到"
                return out, f"✗ 没拿到 magic link（{why}）"
            try:
                client.verify_magic_link(link)
            except HarkError as ex:
                return out, f"✗ 校验 magic link 失败：{ex}"
            new_pw = pipeline.make_password()
            try:
                client.set_password(new_pw)
            except HarkError as ex:
                code = getattr(ex, "code", "")
                if code == CODE_PASSWORD_ALREADY_SET:
                    return out, "✗ 账号其实已有密码（不是我们能设的）—— 只能靠已知密码登"
                return out, f"✗ 设密码失败：{ex}"
            out["password"] = new_pw
            out["stages"] = dict(out.get("stages") or {})
            out["stages"]["password"] = "ok(补设)"
            note = "（补设了密码）"
            # 设密码用的那条会话不是登录会话，下面照常重新登录 —— 那才是"能用"的证明。
            client = HarkClient(proxy=slot)

        # ③ 密码登录（过按出口的限速闸门）。
        waited = pipeline.login_limiter(slot).acquire()
        try:
            body = client.sign_in(email, rec["password"] or out["password"])
        except HarkError as ex:
            return out, f"✗ 密码登录失败：{ex}"
        out["session_token"] = client.session_token()
        out["region"] = client.region()
        out["user_id"] = str(((body or {}).get("user") or {}).get("id") or out.get("user_id") or "")
        out["stages"] = dict(out.get("stages") or {})
        out["stages"]["login"] = "ok"
        # 🔴 登录成功就是“密码可用”的**证明**，所以那一格不该继续写着 failed。
        #    实测踩到：补完的账号里 `password: failed` 而 `login: ok` —— 一个
        #    自相矛盾的阶段表，而它看起来像“密码那步坏了”（正是本次要消灭的那种
        #    误导性阶段值）。只在它还不是 ok 时才改写，保住原有的注记。
        if not str(out["stages"].get("password") or "").startswith("ok"):
            out["stages"]["password"] = "ok(登录验证)"
        out["timings"] = dict(out.get("timings") or {})
        out["timings"]["login_wait_ms"] = int(waited * 1000)

        # ④ 只读校验 —— 与注册链**同一个函数**。
        rec_obj = pipeline.AccountRecord(
            email=email,
            password=out["password"],
            stages=dict(out["stages"]),
            timings=dict(out["timings"]),
        )
        if not pipeline.verify_app(rec_obj, client):
            out["stages"] = dict(rec_obj.stages)
            out["timings"] = dict(rec_obj.timings)
            out["error_kind"] = rec_obj.error_kind
            out["error"] = rec_obj.error
            return out, f"✗ 校验没通过：{rec_obj.error}"

        # ⑤ 合并进原记录（保留 email/password/created_at/proxy 等原有字段）。
        out.update(
            {
                "status": "success",
                "error": "",
                "error_kind": "",
                "stages": dict(rec_obj.stages),
                "timings": dict(rec_obj.timings),
                "user_id": rec_obj.user_id or out.get("user_id", ""),
                "session_token": rec_obj.session_token or out.get("session_token", ""),
                "session_expires_at": rec_obj.session_expires_at,
                "region": rec_obj.region,
                "has_app_access": rec_obj.has_app_access,
                "plan": rec_obj.plan,
                "daily_limit": rec_obj.daily_limit,
                "pool_limit": rec_obj.pool_limit,
            }
        )
        out["recovered"] = (
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 由 recover_accounts.py 补做"
            f"{note}；出口 {redact.redact_url(slot)}"
        )
        return out, f"✓ 补完{note} plan={out['plan']} {out['daily_limit'] // 1000}K tokens/天"
    finally:
        if slot:
            pool.release(slot, ok=(out.get("status") == "success"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="补做已激活但链没走完的账号")
    ap.add_argument("--apply", action="store_true", help="真跑并写台账（默认 dry run）")
    ap.add_argument("--limit", type=int, default=0, help="最多补几个（0 = 全部）")
    args = ap.parse_args(argv)

    src = ledger.ledger_path()
    records = ledger.load_existing(src)
    todo = [r for r in records if needs_recovery(r)]
    if args.limit:
        todo = todo[: args.limit]
    print(f"读源：{ledger.repo_rel(src)}（{len(records)} 条）")
    print(f"待补（activate=ok 且 status≠success）：{len(todo)} 个\n")
    if not todo:
        print("没有待补的账号。")
        return 2

    pool = SlotPool(config.proxy_slots(), timeout=config.PROXY_SLOT_TIMEOUT)
    if not pool:
        print("⚠ 未配置槽位池，将直连 / 走 HR_PROXY")

    updated = []
    ok_n = 0
    for rec in todo:
        mark = "有密码" if rec.get("password") else "**无密码**"
        print(f"── {rec['email']}  [{mark}] 原失败原因={rec.get('error_kind') or '-'}")
        if rec.get("error"):
            print(f"     {str(rec['error'])[:100]}")
        new, verdict = recover_one(rec, pool, dry=not args.apply)
        print(f"   {verdict}")
        if new.get("status") == "success":
            ok_n += 1
            updated.append(new)
        time.sleep(0.2)

    print(f"\n补完 {ok_n} / {len(todo)}")
    if not args.apply:
        print("(dry run —— 加 --apply 才真跑并写台账)")
        return 0

    if not updated:
        print("没有需要写回的记录。")
        return 1

    merged, kept, added, upgraded = ledger.merge_records(records, updated)
    print(f"台账合并：原有 {kept} 条 + 新增 {added} 条（{upgraded} 条被升级）= {len(merged)} 条")
    if len(merged) != len(records):
        raise RuntimeError(f"合并后条数变了（{len(records)} -> {len(merged)}）—— 拒绝写盘")
    snap, last = ledger.save_snapshot(merged, updated, existing=records)
    print(f"新快照（读源）-> {ledger.repo_rel(snap)}")
    print(f"本批结果     -> {ledger.repo_rel(last)}（{len(updated)} 条）")
    print(f"旧快照保留为回滚点 -> {ledger.repo_rel(src)}")
    return 0 if ok_n == len(todo) else 1


if __name__ == "__main__":
    sys.exit(main())
