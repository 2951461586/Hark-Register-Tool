# Hark-Register-Tool

Hark（hark.com）的**端到端注册全链路**：CF Worker 域名邮箱 → magic link →
设密码 → 密码登录 → 会话/资料/额度校验。批量可跑，带台账、配额闸门与报告。

```bash
cp .env.example .env      # 填 IR/HR_WORKER_* 三项（见下）
python run.py             # 跑 1 个账号
python run.py --count 6   # 跑 6 个
```

**零浏览器**：整条链路是标准 JSON API，没有验证码、没有 WAF 解盾，运行依赖
只有 `requests` 一个（对比 [Intern-Register-Tool](https://example.invalid) 必须
用真浏览器解阿里云验证码）。协议与实测证据在 [`docs/protocol.md`](docs/protocol.md)。

---

## 怎么跑

一律**从项目根**执行：

```bash
python run.py                          # 1 个账号
python run.py --count 6                # 6 个（默认 4 路生产者 × 4 路消费者）
python run.py --count 6 --producers 1 --workers 1     # 强制顺序（最保守）
python run.py --out accounts.json      # 导出到指定文件（**不落台账快照**）
python run.py --ignore-quota           # 跳过本地配额闸门
python run.py --quiet                  # 只输出汇总
```

启动阶段会校验配置：缺 `HR_WORKER_*` 直接退出（码 4），
配额窗口已满直接退出（码 2，**一个请求都不发**）。

### 配置（`.env`，已在 `.gitignore` 中）

| 变量 | 必需 | 说明 |
|---|---|---|
| `HR_WORKER_BASE` | ✅ | CF Worker 地址，如 `https://<worker>.<sub>.workers.dev` |
| `HR_WORKER_ADMIN_TOKEN` | ✅ | Worker 的 Admin Token |
| `HR_WORKER_DOMAIN` | ✅ | 收信域名（实测**自有域名直接过**，hark 没有域名门） |
| `HR_BIRTHDAY` | — | 默认 `1995-01-01`。🔴 服务端要求**满 18**，否则要绑卡 |
| `HR_REG_QUOTA_MAX` / `HR_REG_QUOTA_WINDOW_H` | — | **爆炸半径上限**（默认 50 / 24h）：真正的限流是按出口的滑动窗口，这个只防“手滑跑 500 个” |
| `HR_REGISTER_MAX_PER_WINDOW` / `HR_REGISTER_WINDOW_S` | — | **按出口**的注册申请限速（默认 4 次 / 60s，**实测**：magic-link 约 5 次/分/IP） |
| `HR_LOGIN_MAX_PER_WINDOW` / `HR_LOGIN_WINDOW_S` | — | **按出口**的登录限速（默认 3 次 / 10s，实测） |
| `HR_PROXY` | — | 可选出口代理（`host:port:user:pass`） |
| `HR_CF_API_TOKEN` / `HR_CF_ACCOUNT_ID` / `HR_CF_WORKER_NAME` / `HR_CF_D1_DATABASE_ID` | — | 只给 `tools/ops/cf_service_doctor.py` 用 |

完整清单与每项的**取值理由**见 [`.env.example`](.env.example)（注释就是设计文档）。

### 运维体检（跑批量前建议先跑）

```bash
python tools/ops/cf_service_doctor.py          # 邮箱 Worker / D1 健康度
python tools/probes/probe_magic_link.py        # magic-link 端点对照实验
python tools/probes/probe_slots.py             # 出口池：IP 是否两两不同 / hark 是否可达
python tools/probes/probe_domains.py           # 哪些收信域名真能收到 hark 的信
python tools/ops/verify_accounts.py            # 台账账号存活复核
```

**`cf_service_doctor.py`** 从**服务端**回答"邮箱还能不能用来跑批量"：
Worker 版本与部署历史（标出回滚）、D1 的 `rows_read`/`rows_written`
（免费额度 5,000,000 / 100,000 每天，UTC 00:00 重置）、以及两条收信端点的
直连探针。

🔴 两条判据值得单独记住（都是实测出来的）：

* **判 token 有没有效要打 `/accounts`，不要打 `/user/tokens/verify`。**
  本项目用的是**账户级** token（`cfat_` 前缀），而 `/user/tokens/verify`
  是**用户级**端点 —— 账户级 token 在它那里必然返回 `Invalid API Token`。
  拿它当判据会把一个好 token 判死，然后人就去重新签发一个，白折腾。
* **D1 拦的是"扫描行数"，不是"返回行数"。** 所以"返回空"不等于"没被拦"：
  `/api/inbox?email=<不存在>` 走索引（rows_read=0，可用），
  而 `/admin/all?limit=1` 要扫表（配额用尽时会被 7500 顶成 500）。
  这正是那两条探针的用途。

**`probe_magic_link.py`** 回答"注册全失败时，是服务端挂了还是我们被封了"。
这两种原因的**修法完全相反**，而现象一模一样，所以它做的是一次对照实验：
控制组（`account-discovery` + `signup-age`）+ 对照组（`magic-link`）+
变量（`--proxy` 给多个出口）。三种结论分别对应三种处理。

> ⚠ 它会在输出里提醒一个很容易踩的前提：**"换了代理"不等于"换了出口 IP"** ——
> 本地 Clash 的混合端口对外往往仍是同一个 IP（实测过）。

**`probe_domains.py`** 回答"哪个收信域名真能收到 hark 的信"。
有一个自有域名在 Worker 的 `DOMAINS` 里、邮箱也建得出来，但 hark 的 magic link
**从来没到** —— 每个账号白等 **120s** 的收信超时。本工具把那次 120s 变成一次
35s 的对照实验，而且**不验证链接**（hark 的账号在 verify 那一步才建），所以零副作用。

**`probe_slots.py`** 回答"出口池是不是真的有 N 个不同出口"。
🔴 判据必须是**真的发一个请求拿到出口 IP**：本机开着 Clash TUN 时连一个不可达端口
只要 0.02s（那是连到本地虚拟网卡），TCP 能连 ≠ 代理可用。实测 5 个槽位：
出口 IP 两两不同且**稳定**（每个连打 4 次都是同一个）。

**`verify_accounts.py`** 用台账里的密码重新登录一次，证明账号**现在**还能用
（两级判据：登录 200 + 能读到额度）。它默认读**台账读源**而不是某个导出快照，
并在覆盖不足时返回退出码 3 —— 详见「已知限制」里的"文件级静默缩水"。

---

## 架构

```
┌─ 生产者池（producers 路）────────────────┐   ┌─ 消费者池（workers 路）────────┐
│ 建邮箱 → discovery → signup-age →        │   │ 设密码 → 密码登录 →            │
│ magic-link → **等信** → 校验链接          │──▶│ get-session/profile/billing   │
└──────────────────────────────────────────┘队列└──────────────────────────────┘
```

**为什么分两段**：两段的**等待形状**不同 —— 生产者那侧卡在"等外部 SMTP 投递"
（不可控、要轮询），消费者那侧是纯 HTTP 快活。把等信放进生产者池并发跑，
等待就被并行化了，而不是串行地加在关键路径上。

> ⚠ 与 Intern-Register-Tool 的关键差异：**那边分两段的理由是"浏览器很贵"**
> （不能让注册并发度牵着浏览器并发度走）。本项目零浏览器，所以别把那边
> "浏览器并发到 6 就够"的经验搬过来 —— 这里没有那个约束。

`--producers` / `--workers` 的取值依据见 [`run.py`](run.py) 的模块 docstring。
两类的限流现在都是**按出口的滑动窗口**（注册申请 4 次/60s、登录 3 次/10s，
**都是实测出来的**），所以**加出口才是提高吞吐的那个乘数**；
调大 `--workers` 只是去抢同一张窗口里的名额。

### 模块分工

| 文件 | 职责 |
|---|---|
| `common/config.py` | 全局配置 + 启动校验（含生日年龄的本地判据） |
| `src/tempmail.py` | CF Worker 邮箱客户端（建地址 / 按收件人收信 / 抽链接） |
| `src/hark.py` | Hark 协议客户端（7 步链路 + 只读校验 + 错误分类） |
| `src/pipeline.py` | 两段式流水线、`AccountRecord`、`error_kind` 的**唯一**读点、登录限速器 |
| `src/ledger.py` | 台账（合并而不是覆盖；防静默缩水护栏） |
| `src/quota.py` | 本地配额闸门（保守，**不是**服务端权威计量） |
| `src/report.py` | 收尾报告（**纯展示 + 依赖注入**，只依赖 stdlib） |
| `src/redact.py` / `src/fsutil.py` | 脱敏 / 原子写盘（各自唯一实现） |
| `tools/ops/cf_service_doctor.py` | 邮箱 Worker / D1 服务端体检 |
| `tools/ops/verify_accounts.py` | 台账账号存活复核（用台账里的密码重新登录） |
| `tools/data/recover_accounts.py` | 补做「已激活但链没走完」的账号（重登 / magic link 补密码） |
| `tools/data/repair_stages.py` | 修台账里被错误标记的阶段值（带备份 + 新快照） |
| `tools/probes/probe_magic_link.py` | magic-link 端点的对照实验（服务端故障 vs 出口被封） |
| `tools/probes/probe_slots.py` | 出口池体检（真实出口 IP / 两两不同 / hark 可达） |
| `tools/probes/probe_domains.py` | 域名门（哪些收信域名真能收到 hark 的信） |
| `src/proxypool.py` | 槽位出口池（按出口分桶的登录限速器靠它才有意义） |

---

## 输出

### 台账：`ledger/` 目录 + 日期/时间戳快照

`run.py --out` **不填时**（默认），台账落在仓库根的 `ledger/`：

```
ledger/runs/2026-10-07/accounts-20261007-220408.json   ← **读源**（合并后的全量）
ledger/runs/2026-10-07/accounts-20261007-202720.json   ← 上一份快照（回滚点）
ledger/latest.json                                     ← **本批结果**（含失败/跳过）
```

| 文件 | 内容 | 谁读它 |
|---|---|---|
| `runs/<日期>/accounts-<时间戳>.json` | **合并后的累计全量**（按 email 合并） | 所有工具 —— 它就是台账读源 |
| `ledger/latest.json` | **最近一次落盘写进去的记录** | 只给人看"这次写了什么" |

四条规则，缺一条都会把台账搞坏：

1. **读源是「最新的那份快照」，不是 `latest.json`。** 后者只含本批那几十条，
   拿它当读源 ⇒ 下一次运行合并的基准只剩上一批 ⇒ **台账停止累积、每次跑批
   覆盖上一次**（Intern-Register-Tool 栽过两次）。
2. **快照里是「合并后的全量」**，不是本次那几条。
3. **同一 email 合并时不让失败盖掉成功**（rank：`success` 2 > `skipped` 1 > 其他 0）；
   同级取**并集**（字段只增不减）。
4. **防静默缩水护栏**：合并后条数 < 原有条数就直接抛异常、退出码 3，
   宁可报错也不静默丢账号。要显式覆盖得用 `--overwrite`。

> 🔴 **台账在本项目是账号的唯一副本**：邮箱是临时邮箱（收不到找回信），
> 密码只存在于台账里。整个 `ledger/` 目录都在 `.gitignore` 中 ——
> **别把它当临时文件清掉**，定期另存一份到别处。

每条记录（字段顺序 = `AccountRecord.to_json()` 的顺序）：

```json
{
  "email": "oai-xxxxxxxx@<your-mail-domain>",
  "password": "Kx#7mQ2vRt9pLw4zBn6d",
  "user_id": "oR0KhrDVCE9sWj6zQEIZDfMBbP35VViE",
  "session_token": "1yuouvgGV7KxJsziJm9pscfq2MsvbCTu",
  "session_expires_at": "2026-11-06T09:49:31.733Z",
  "region": "us",
  "has_app_access": true,
  "plan": "free",
  "daily_limit": 8000000,
  "pool_limit": 40000000,
  "status": "success",
  "error": "",
  "error_kind": "",
  "stages": {"mailbox": "ok", "register": "ok(exists=False)", "activate": "ok(mail+verify)",
             "password": "ok", "login": "ok", "app": "ok(8000K tokens/day)"},
  "timings": {"mailbox_ms": 2422, "register_ms": 3401, "activate_ms": 3995,
              "mail_wait_ms": 285, "verify_ms": 3710, "password_ms": 292,
              "login_ms": 1417, "app_ms": 904},
  "birthday": "1995-01-01",
  "created_at": "2026-10-07 22:04:08",
  "proxy": ""
}
```

| 字段 | 什么时候有 | 说明 |
|---|---|---|
| `stages` | 总是（没跑到的阶段是 `""`） | 每个阶段的结果。**这是回答"卡在哪一步"的唯一依据** |
| `timings` | 跑到才有 | `activate_ms` 是总，`mail_wait_ms`/`verify_ms` 是它的子项 |
| `has_app_access` | 跑到 `app` 才有 | 平台的**准入**开关。`false` = 注册成功但用不了（报告会单独标 ⚠） |
| `error_kind` | 失败时 | 结构化类别，见下 |
| `proxy` | 配了代理时 | 这个账号用的出口。**记它是为了事后能回答"被封的到底是哪个出口"** |

### `error_kind`：错误的**结构化类别** —— 不要再搜 `error` 文本

| 值 | 含义 | 判据来源 |
|---|---|---|
| `""` | 没有错误 | — |
| `"quota"` | 服务端明确限流/配额（HTTP 429 或配额类业务码） | 服务端响应 |
| `"quota_guard"` | 本地闸门主动中止，**一个请求都没发** | 本地 |
| `"rejected"` | 服务端明确拒绝（非配额）：年龄门 / 密码策略 / 凭据错 / 链接被拒 | 服务端响应 |
| `"network"` | 网络 / 超时 / 5xx | 异常 |
| `"mail"` | 邮箱侧（建地址失败 / 读信读不出来） | 异常 |

⚠ **`quota` 在链路里有两个位置，而处理方式相反**（别把它们统一）：

* **生产者（注册申请）撞 429** ⇒ 连续两次就**停整批**（剩余标 `quota_guard`）——
  那种配额越打越糟；
* **消费者（密码登录）撞 429** ⇒ **退避重试**（默认最多 3 次）——
  登录的 429 实测是 **3 次/10s** 的限流，**自愈**。把它读成"账号坏了"会得到
  一堆**假阴性**（账号其实好好的）。详见 [`docs/protocol.md`](docs/protocol.md)。

读点统一走 `pipeline.error_kind_of(rec)`：**字段非空即权威**，只有字段为空时
才退化成文本匹配（给手工构造的老记录兜底）。

**为什么必须有这个字段**：我们自己拼的错误文案里也会出现服务端的词
（本项目的 `error` 就把响应体原文附了进去），文本匹配分不清"服务端说的"和
"我们引用的"。Intern-Register-Tool 为此踩过一个具体的坑：一个**一个请求都没发**
的干净出口被按长冷却晾了 120s。

### 报告

```
══════════════════════════════════════════════════════════════
结果：成功 1 / 1（失败 0，跳过 0）｜墙钟 12.4s｜4 路并发 → 12.45s/账号
──────────────────────────────────────────────────────────────
  ✓ oai…@<your-mail-domain>    plan=free｜8000K tokens/天
──────────────────────────────────────────────────────────────
阶段耗时（中位 / 最大）
  mailbox        2.4s /     2.4s
  register       3.4s /     3.4s
  activate       4.0s /     4.0s   ← 其中等信 中位 285ms
  ...
最慢账号（决定本批总时长）：oai…@<your-mail-domain>
  mailbox=2.4s register=3.4s mail_wait=285ms verify=3.7s ...
```

三件事值得注意：**打印最慢的账号**（批量总时长由它决定，不是均值）、
**把"等信"从 `activate` 里拆出来**（否则会被读成"校验慢"）、
**邮箱打码**（但保留域名 —— 域名是排查时要的信息）。

---

## 已知限制

* **出口池实测（2026-10-08）**：5 个 mihomo 槽位（`HR_PROXY_SLOTS`），出口 IP
  两两不同且稳定，hark 可达。**登录侧与注册侧都按 IP 限**，所以两边都受益。
* **10 批全链路实测：43/50（86%），5/10 批完全干净**（每批 5 个账号、5 路并发，
  批次串行）。前 5 批全绿（10~12s/批），后 5 批共 7 个失败。详细归因与两种 429 的
  形状对比在 [`docs/protocol.md`](docs/protocol.md)「10 批全链路实测」。
* 🔴 **注册侧的 429 是 per-IP，且只限 `magic-link` 这一个端点**（2026-10-08 实测，
  `tools/probes/probe_register_limit.py`）：
  · 只打只读的 `discovery` 一直 200 ⇒ 限的是**写**端点
  · 同出口第 6 次 `magic-link` → 429；**换收件域名仍 429**、**换出口立刻通过**
  ⇒ **加出口确实能提高注册吞吐**（每出口一张窗口，约 4~5 次/分/出口）。

  ⚠ 本文早前写过一个**相反且错误**的结论：“批次 6 里 3 个**不同**出口同时 429
  ⇒ 不是 per-IP”。那个推理的漏洞是：那 3 个出口各自在前 60s 里都积了 ~5 次
  （每批 1 次 × 5 批），所以它们**同时**触顶 —— **同时性 ≠ 同一个键**，
  它只说明它们的**历史相同**。事后看，那个结论还直接导致了一个错误的参数
  （全局最小间隔），已一并改掉。
* 🔴 **登录侧的 CF 429 已改成换出口**（原来是原地退避重试）：实测在一个被 CF
  限住的出口上重试 4 次（跨 ~17s）仍然 429，而换个出口立刻过。换过的路径会写进
  台账的 `proxy_rotated`，`proxy` 记**最终**出口。
  那 ~17s 是白等的。Intern-Register-Tool 的 `ExitRotationNeeded` 就是这个思路，
  这是本项目下一个该改的地方。
* **自有域名里有的收不到 hark 的信**（实测 120s 超时 × 2）：它在 Worker 的
  `DOMAINS` 里、邮箱也建得出来，但 magic link 从来没到。跑批量前先用
  `tools/probes/probe_domains.py` 过一遍域名（零副作用：不验证链接就不会建号）。
* **不要并行跑多个 `run.py` 进程**：登录限流是服务端按 IP 计的，而我们的滑动窗口
  限速器是**进程内**的 —— 10 个进程会各算各的，把 3 次/10s 打穿。
  要提吞吐就加出口（每出口一张窗口），或者等跨进程限速器。
* **注册侧的限流已实测**（2026-10-08）：限 `magic-link` 这一个写端点、**按 IP**、
  约 **5 次/分钟**（第 6 次 429），**窗口 = 60s**（对照实验确认失败探测不推窗口）。
  ⇒ `HR_REGISTER_MAX_PER_WINDOW=4` / `HR_REGISTER_WINDOW_S=60` 都是实测值。
  ⚠ 早前那条“magic-link 5 连发 / 2.5s 全部 200 ⇒ 没有突发限流”的结论**已被推翻**：
  那 5 次**正好卡在额度上**，它证明的是“额度至少 5”，不是“没有限流”。2026-10-07 那次 4/4 失败是
  **服务端 magic-link 端点整体 500**（换域名、换收件人、新号老号都一样，
  而 discovery/signup-age 照常 200；30 分钟后仍 500），**不是限流** ——
  排查过程与**一个没有排除干净的可能**（出口维度）记在
  [`docs/protocol.md`](docs/protocol.md)「限流与配额：实测」。
  ⚠ `HR_REG_QUOTA_MAX` 已从 20 改成 **50**：它的角色被重新定位成“爆炸半径上限”
  （真正的限流是按出口的滑动窗口，两个都是实测值）—— 旧的 20 被现实跨过
  （2026-10-08 一天成功 53 个），而**总是被跨过的闸门等于不存在**。
* **单账号关键路径约 6.3s**（10 批实测的**中位**）：`mailbox` 0.81s +
  `register`（三个写请求）1.30s + `activate` 2.25s（其中等信 ~0.3–1.3s、
  `verify` 1.0–3.7s）+ `password` 0.27s + `login` 0.81s + `app` 0.89s。
  ⇒ **瓶颈不在收信**（等信中位只有几百毫秒），而在 `verify` 与 `register`。
* **批内并发把关键路径隐藏得很好**：5 个账号 5 路并发跑完一批只要 **8.7–9.0s**
  （≈ 1.8s/账号），而单账号串行是 6.3s。
  ⚠ 这一数字在 2026-10-08 改掉“全局最小间隔”**之前**是 10.2–12.1s ——
  旧闸门把 5 路生产者串行化成 1 次/秒，白花了 ~2–3s/批。
* **`verify` 那一两秒压不掉**：它是一次 302，服务端要建会话 + 写库。
  只能靠并发掩盖。
* **`set-password` 是一次性的**。所以这一步**不重试**；撞到
  `PASSWORD_ALREADY_SET` 时按成功处理但标注出来（`ok(已存在(未覆盖))`）。
* **`--workers` / `--producers` 没有实测安全上限**。默认 4/4 落在"没观测到
  问题"的区间内，不代表它是极限（且登录侧已被 3/10s 限住）。
* **`account-discovery` 不校验邮箱格式**（`not-an-email` 也返回 200
  `exists:false`），所以它不能当"注册前置校验"。
* **年龄门是硬门槛**：未满 18 会要求绑卡（`CARD_VERIFICATION_REQUIRED`），
  而那是**每个账号**都要撞一次的失败 ⇒ 本地在启动阶段就拦。
* **邮箱 Worker 与 Intern-Register-Tool 共用**。它同时服务别的项目，
  所以 `/admin/all` 那条路既烧 D1 读配额又会被别人的邮件挤出窗口 ——
  本项目一律走 `/api/inbox?email=`（按收件人索引）。实测 D1 的 `rows_read`
  只用掉免费额度的 **0.3%**，目前不是瓶颈。
* **文件级静默缩水**：任何"拿一份导出快照当分母"的结论都可能**看着全绿**
  （Intern-Register-Tool 实测过"53/53 存活"而台账里已有 605 把）。所以
  `verify_accounts.py` 默认读**台账读源**，并在覆盖不足时返回退出码 3；
  它还会把"注册未走完（密码没设上）"的记录**排除在存活率之外并打印数量** ——
  把它们算进分母会得到一堆 `INVALID_EMAIL_OR_PASSWORD`，那不是"账号坏了"，
  是"这个账号从来没成过"。
* **免费额度是观测值**：`dailyLimit` 8,000,000 / `poolLimit` 40,000,000。
  平台随时可能改；台账里按账号记下来正是为了能回答"额度什么时候变的"。

## 测试

```bash
python -m pytest        # 135 个用例，**零网络请求**
```

⚠ 这里的数字会随用例增减而漂移。**判据是 `pytest` 的汇总行**，不是这句话 ——
有 CI（`.github/workflows/ci.yml`）之后，它每次都重新量一遍。

覆盖：协议错误映射与重试边界、链接提取（含 HTML 反转义与尾部标点）、
流水线编排与打标、配额闸门、出口池（分摊 / 并发 / 异常归还）、按出口分桶的
登录限速器、台账合并与防缩水护栏、报告契约、CLI 契约（调用点 vs 签名）、
`tools/` 下的纯函数、仓库卫生（凭据不进仓库 / `.gitignore` 真的生效 /
报告层不 import 重依赖）。

跑真实链路（会**真的建账号**）：

```bash
python run.py --count 1
```

## 免责声明

本项目是**协议与自动化流程的技术研究**，目的是搞清 magic link 注册链路的
HTTP 交互机制。

* 请勿用于批量刷取、账号转售或任何违反目标平台服务条款的用途
* 工具**不绕过任何付费环节**，也不篡改额度 —— 领的就是平台公开提供的免费额度
* 使用产生的任何后果由使用者自行承担

仓库内**不含任何真实账号数据**。`ledger/`（明文账号/密码/session token）、
`state/`（本地计数）、`.env`（含 Worker Admin Token 与 CF API Token）
均已在 `.gitignore` 中**按目录**排除，并由 `tests/test_repo_hygiene.py` 钉住。

## 许可

MIT
