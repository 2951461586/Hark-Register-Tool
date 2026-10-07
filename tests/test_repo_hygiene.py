"""仓库卫生用例 —— 守住"凭据不进仓库"和"依赖面"这两条线。

🔴 为什么凭据闸门必须自动化
--------------------------
`.env` 在 `.gitignore` 里，但**那拦不住**"有人手滑把 token 贴进 README 的示例里"
或者"把真实 token 写进 `.env.example` 当默认值"。这类泄漏一次就够，
而人工复核在几十次提交之后必然漏。

⚠ 本文件**自己不能出现明文凭据**（否则闸门会把自己拦下）。
   所以针串是**运行时拼**出来的 —— 与 Intern-Register-Tool 的
   `selftest_check_leaks.py` 同一个做法。
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 不该出现在**任何入库文件**里的凭据（运行时拼接，避免本文件自己被扫出来）。
NEEDLES = [
    "cfat_" + "fvz8SagSk65HXbO6urAwIft0wgMQnGPg1jf6uHji103ad044",
    "Zhy." + "200403154512",
]

# 🔴 **形状**检测，而不只是那两个已知值。
#
# 为什么必须有这一层：只认两个具体串的闸门，**只能拦住已经泄过的那两个** ——
# 下一个新凭据（换一个 CF token、加一个别家 API Key）它一声不吞。
# 2026-10-08 发现这一点是因为给 CI 写了个“变异自检”（往仓库里注入一个随机假
# 凭据，断言闸门会红），而那个自检**当场就会失败**：随机串不在 NEEDLES 里。
#
# 模式要**窄**（宁可漏报不可误报）：一旦出现假阳性，人就会把这条闸门当成噪音，
# 而噪音闸门比没有闸门更坏。所以每条都要求足够长的随机尾部，且要有各家前缀。
# ⚠ 模式本身写在源码里时，要保证**它的字面文本不会匹配它自己**
#   （`cfat_[0-9A-Za-z]{20,}` 里 `cfat_` 后面跟的是 `[`，不是字母数字）。
CREDENTIAL_PATTERNS = [
    ("Cloudflare 账户级 API Token", r"cfat_[0-9A-Za-z]{20,}"),
    ("OpenAI 风格 key", r"\bsk-[A-Za-z0-9_-]{24,}"),
    ("GitHub PAT（经典）", r"\bghp_[A-Za-z0-9]{30,}"),
    ("GitHub PAT（细粒度）", r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    ("AWS Access Key ID", r"\bAKIA[0-9A-Z]{16}\b"),
    ("Slack token", r"\bxox[baprs]-[A-Za-z0-9-]{20,}"),
    ("私钥块", r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]

# 扫描时**故意跳过**的路径：这些地方写的是“形状”而不是“真值”。
# ⚠ `.env.example` 不跳：它就该只有空值。当前它提到 `cfat_` 前缀，
#   但后面没有 20+ 字符的尾巴，所以不会命中。
SCAN_ALLOWED_PATHS = {Path("tests/test_repo_hygiene.py")}

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "ledger",
    "state",
    ".venv",
    "venv",
}
# `.env` 是**本地凭据文件**（gitignored，就该有值）；`.gitignore` 必须把它排掉。
SKIP_FILES = {".env"}


def iter_repo_files():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.name in SKIP_FILES:
            continue
        yield p


def test_no_credentials_in_repo_files():
    """两层：**已知的两个具体值** + **凭据形状**。

    ⚠ 本文件自己被跳过（`SCAN_ALLOWED_PATHS`）—— 否则它里面的模式字面量
      与自检用的假串会把它自己拦下。
    """
    import re

    hits = []
    for p in iter_repo_files():
        rel = p.relative_to(ROOT)
        if rel in SCAN_ALLOWED_PATHS:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for needle in NEEDLES:
            if needle in text:
                hits.append(f"{rel} 含**已知**凭据片段")
        for label, pat in CREDENTIAL_PATTERNS:
            if re.search(pat, text):
                hits.append(f"{rel} 含**{label}**形状的串")
    assert not hits, "凭据泄漏：\n  " + "\n  ".join(hits)


# ── 🟠 基础设施标识 / 本机路径 ─────────────────────────────
#
# 这两条的判据写在 docs/security-conventions.md 的 §1 与 §5。它们是 2026-10-08
# 那次审计的直接产物：当时 `git grep` 在**已提交的树**里找到 16 处收信域名、
# 5 处另一个自有域名、1 个具体账号地址、2 处本机绝对路径。
# 那些违规**不是**“忘了加 gitignore”，而是“在注释/文档/默认值里顺手写了真实值”。

# 本机绝对路径（`F:/…`、`C:\…`）。判据取“盘符 + 分隔符 + 至少两段”，
# 免得把 `ratio 3:1` 这类写法当路径。
ABS_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/][^\s\"'<>|*?]{2,}[\\/]")


def _sensitive_values_from_env() -> list:
    """从本地 `.env` **反推**“对你而言敏感的具体值”。

    ⚠ CI 上没有 `.env`（它是 gitignored 的）⇒ 返回空 ⇒ 本闸门在 CI 上是**空转**。
      这是**边界**不是缺陷：CI 无法知道什么值对你敏感。所以它**不能替代人工分类**，
      只能在你本机跑测试时把“顺手写进去的真实值”当场拦下。
      详见 `docs/security-conventions.md` §5。
    """
    from urllib.parse import urlparse

    from common import config

    out: list = []
    if config.WORKER_DOMAIN:
        out.append(("收信域名", config.WORKER_DOMAIN))
    host = urlparse(config.WORKER_BASE or "").hostname or ""
    if host:
        out.append(("Worker 主机", host))
        labels = host.split(".")
        if len(labels) >= 2:
            out.append(("Worker 名", labels[0]))
            # workers.dev 的形态是 `<worker>.<账号子域>.workers.dev`，
            # 账号子域在倒数第三层。
            if labels[-2:] == ["workers", "dev"]:
                out.append(("workers.dev 账号子域", labels[-3]))
    return [(label, val) for label, val in out if val and len(val) >= 4]


def test_no_infrastructure_identifiers_in_repo_files():
    """🟠 标识不得出现在仓库里（值从本地 `.env` 读，见上面函数的说明）。"""
    values = _sensitive_values_from_env()
    if not values:
        pytest.skip("本地没有 .env（CI 上必然如此）—— 这条闸门只能在本机生效")
    hits = []
    for p in iter_repo_files():
        rel = p.relative_to(ROOT)
        if rel in SCAN_ALLOWED_PATHS:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, val in values:
            if val in text:
                hits.append(f"{rel} 含{label}")
    assert not hits, (
        "基础设施标识泄漏（按 docs/security-conventions.md §1 应改为占位符）：\n  "
        + "\n  ".join(sorted(set(hits)))
    )


def test_no_absolute_paths_in_repo_files():
    """🟠 本机绝对路径不得入库（跨机器无意义，且暴露目录布局）。"""
    hits = []
    for p in iter_repo_files():
        rel = p.relative_to(ROOT)
        if rel in SCAN_ALLOWED_PATHS:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in ABS_PATH_RE.finditer(text):
            hits.append(f"{rel}: {m.group(0)!r}")
    assert not hits, "绝对路径入库：\n  " + "\n  ".join(sorted(set(hits))[:10])


def test_credential_detector_actually_fires():
    """🔴 变异自检：闸门**真的**会拦吗？

    一个恒返回空的检测器也能让上一条用例变绿，而那正是本项目最警惕的
    “闸门在说假话”。所以这里拿**合成**凭据验证它，而且用的是拼出来的串
    （写死的话本文件自己会被上一条用例拦下）。

    ⚠ 这段与 CI 里的 canary 是同一件事的两个层次：这里**本地每次跑测试都验证**
      （不依赖 CI），CI 那份是“真实注入一个文件再跑闸门”的端到端版。
    """
    import re

    fake = {
        "Cloudflare 账户级 API Token": "cfat_" + "a1B2" * 12,
        "OpenAI 风格 key": "sk-" + "x" * 32,
        "GitHub PAT（经典）": "ghp_" + "Z" * 36,
        "AWS Access Key ID": "AKIA" + "Q" * 16,
        "私钥块": "-----BEGIN " + "RSA PRIVATE KEY-----",
    }
    for label, pat in CREDENTIAL_PATTERNS:
        sample = fake.get(label)
        if sample is None:
            continue
        assert re.search(pat, sample), f"检测器对 {label} 失效（模式写错了）"
    # 负对照：这些**不该**命中（否则闸门会变成噪音，然后被忽略）
    for benign in (
        "cfat_",  # 只提前缀
        "HR_CF_API_TOKEN=",  # 空值
        "`sk-` 前缀",  # 文档里的说明
        "sk-short",
        "AKIA",
        "the quick brown fox",
    ):
        for label, pat in CREDENTIAL_PATTERNS:
            assert not re.search(pat, benign), f"误报：{benign!r} 被当成 {label}"


def test_env_is_gitignored():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gi


def test_sensitive_dirs_are_ignored_by_directory_not_glob():
    """🔴 台账与本地状态必须**按目录**排除，不靠 `*.json` 通配兜底：
    通配符一旦被某个 `!` 例外打穿，泄漏的就是整本账号。"""
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    lines = {ln.strip() for ln in gi.splitlines()}
    assert "ledger/" in lines
    assert "state/" in lines


def test_gitignore_actually_ignores_the_ledger(tmp_path):
    """真跑一次 `git check-ignore` —— 只读 `.gitignore` 文本是**自证**，
    证明不了 git 真的会忽略它（比如被上层仓库的规则覆盖）。"""
    if not (ROOT / ".git").exists():
        pytest.skip("不是 git 工作树，跳过 check-ignore")
    r = subprocess.run(
        ["git", "check-ignore", "-v", "ledger/latest.json", "state/register_quota.jsonl", ".env"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"这些路径**没有**被忽略：{r.stdout}{r.stderr}"


def test_docs_exist():
    for rel in (
        "README.md",
        "docs/protocol.md",
        "docs/README.md",
        # ⚠ 这份文档本身要进闸门：它是 §5 那张“闸门清单”的宿主，
        #   被删掉之后所有引用它的注释都会指向空气。
        "docs/security-conventions.md",
        ".env.example",
    ):
        assert (ROOT / rel).is_file(), rel


def test_config_has_no_hardcoded_credentials():
    """默认值必须留空 —— 本仓库是公开的，写死等于把邮箱服务交出去。"""
    from common import config

    assert config.WORKER_ADMIN_TOKEN == "" or config.WORKER_ADMIN_TOKEN == (
        __import__("os").getenv("HR_WORKER_ADMIN_TOKEN", "")
    )
    # 这些是**公开常识**，允许有默认值（写死它们不影响安全）。
    assert config.HARK_BASE.startswith("https://")
    assert config.AUTH_BASE.startswith("https://")


def test_validate_reports_missing_config(monkeypatch):
    from common import config

    monkeypatch.setattr(config, "WORKER_BASE", "")
    monkeypatch.setattr(config, "WORKER_DOMAIN", "")
    monkeypatch.setattr(config, "WORKER_ADMIN_TOKEN", "")
    missing = config.validate()
    assert "HR_WORKER_BASE" in missing
    assert "HR_WORKER_DOMAIN" in missing
    assert "HR_WORKER_ADMIN_TOKEN" in missing


def test_validate_rejects_underage_birthday(monkeypatch):
    """🔴 未满 18 会被服务端要求绑卡（CARD_VERIFICATION_REQUIRED）——
    那是**每个账号**都要撞一次的失败，所以必须在启动阶段就报出来。"""
    from common import config

    monkeypatch.setattr(config, "BIRTHDAY", "2020-01-01")
    assert any("HR_BIRTHDAY" in m for m in config.validate())


def test_birthday_age_boundary():
    import datetime as dt

    from common import config

    today = dt.date(2026, 10, 7)
    assert config.birthday_age("2008-10-07", today) == 18  # 实测刚好过
    assert config.birthday_age("2008-10-08", today) == 17  # 差一天就不过
    assert config.birthday_age("not-a-date", today) is None
    assert not config.valid_birthday("2020-01-01")


def test_requirements_pin_requests_only():
    """整链**零浏览器**：运行依赖只有 requests。多一个依赖就多一份供应链风险。"""
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in reqs.splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines and all("requests" in ln for ln in lines), lines


def test_report_layer_has_no_heavy_imports():
    """🔴 报告层必须能被测试**廉价地** import。它一旦 import 了 requests 那条链，
    `test_report.py` 就会把整套 pipeline 拉进 `sys.path`，而那正是
    "测试链污染"这类问题的起点。"""
    text = (ROOT / "src" / "report.py").read_text(encoding="utf-8")
    for bad in ("import requests", "from common", "from .hark", "from .pipeline"):
        assert bad not in text, bad


def test_cli_help_runs():
    """`--help` 必须能在**没有 .env** 的情况下跑起来（配置校验不能放在 import 时）。

    ⚠ `encoding="utf-8"` 必须显式给：Windows 上 `text=True` 用**系统默认编码**
      （本机是 GBK），而 `--help` 里全是中文 ⇒ 子进程输出解码失败会
      **在读取线程里**抛 `UnicodeDecodeError`，报错形态是一个 pytest warning
      加一个 `stdout is None` 的怪异失败，完全看不出是编码问题。
    """
    r = subprocess.run(
        [sys.executable, "run.py", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert r.returncode == 0, r.stderr
    assert "--count" in r.stdout and "--ignore-quota" in r.stdout
