"""脱敏助手 —— **日志 / 输出边界必须过这里**。

为什么单独成模块
----------------
"读配置"和"打日志"是两件事。混在一起会让调用方以为它们是一回事，
从而在新增输出点时漏掉脱敏。（Intern-Register-Tool 真的漏过一次：
一个探测器会打印 `IR_PROXY=<完整串>`。）

🔴 为什么脱敏是硬要求：代理串是 `scheme://user:pass@host:port` 形式，
   直接 print / 写日志会把**账密**一起落盘；而 session token / 密码
   一旦进了日志，就同时进了终端回滚缓冲、重定向文件、以及别人让你贴的
   "报错截图"。泄漏一次就够。

用法：**凡是把凭据类值写进 stdout / 日志 / 报告的地方，先过这里。**
"""


def redact_url(url: str) -> str:
    """把 URL 的 userinfo（`user:pass@`）换成 `***`，其余保留。

    http://user:pass@203.0.113.30:8080  →  http://***@203.0.113.30:8080
    http://127.0.0.1:7901               →  原样（没有 userinfo）
    """
    if not url or "://" not in url:
        return url or ""
    scheme, _, rest = url.partition("://")
    if "@" not in rest:
        return url
    _userinfo, _, hostpart = rest.rpartition("@")
    return f"{scheme}://***@{hostpart}"


def redact(raw: str, keep: int = 0) -> str:
    """把凭据串打成 `<N chars>` —— **连前缀都不打**。

    `keep>0` 只用于本地排查。默认 0，因为"前 6 位"这类信息也足以在别处做关联。
    """
    if not raw:
        return ""
    if keep and len(raw) > keep:
        return f"{raw[:keep]}…<{len(raw)} chars>"
    return f"<{len(raw)} chars>"


def redact_email(address: str) -> str:
    """邮箱打码：`oai-1234567890abcdef@example.com` → `oai…@example.com`。

    ⚠ 与 `redact()` 不同，这里**保留域名**：域名是"用了哪个邮箱源"这个事实，
      排查时需要它；而本地部分才是与某个具体账号绑定的标识。

    ⚠ 台账里存的是**明文邮箱**（那是账号台账，本来就要能找回账号）。
      本函数只用于**终端输出 / 日志**，不用于台账。
    """
    s = (address or "").strip()
    if "@" not in s:
        return s
    local, _, domain = s.rpartition("@")
    head = local[:3]
    return f"{head}…@{domain}"
