"""工具实现包。

模块分工（与 Intern-Register-Tool 对齐，但按 hark 的实际形状裁剪）：

    tempmail.py   CF Worker 临时邮箱客户端（建地址 + 按收件人收信 + 抽链接）
    hark.py       Hark 协议客户端（discovery / age / magic-link / verify /
                  set-password / sign-in / session / profile / billing）
    pipeline.py   两段式流水线（生产者：建邮箱→申请 magic link→等信→验链；
                  消费者：设密码→密码登录→会话与额度校验）
    ledger.py     账号台账（合并而不是覆盖；防静默缩水护栏）
    quota.py      本地注册配额保护（保守闸门，不是服务端权威计量）
    report.py     批量收尾报告（纯展示，依赖注入，不 import 上层）
    redact.py     日志 / 输出边界的脱敏
    fsutil.py     原子写盘（唯一实现）
    cli.py        两个 CLI 入口共用的骨架
"""
