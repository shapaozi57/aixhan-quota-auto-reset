# AixHan Quota Auto Reset

这是一个本地 GUI 程序，用来监控 Codex / ccswitch 的额度信号，并在检测到 402 额度不足后自动进入 AixHan 卡密页点击“充值/重置额度 -> 确认”。

## 使用步骤

1. 双击 `install.bat` 安装依赖；程序会优先复用本机 Chrome / Edge。
2. 双击桌面 `AixHan Quota Auto Reset` 快捷方式启动图形界面；也可以双击 `start.bat`，它会转交给隐藏启动器，不再停留黑色命令窗口。
3. 在 GUI 里填入 `AixHan 卡密`，点击“打开/输入卡密”，确认能看到“账号管理”和“重置额度”。
   - 如果默认打开在“套餐选购”页，程序会先自动点击顶部“卡密激活”，再填入卡密。
4. 在 GUI 里确认：每日限额默认 `$50`，触发阈值默认 `$0.25`。
5. 在顶部“续跑策略”里选择是否 `继续 continue`、是否 `继续目标`，并设置 `当日最多重置`；填 `0` 表示不限次数。
6. 打开“自动重置 ON”。程序会预热浏览器，之后实时轮询 ccswitch / Codex 的 402 日志。
7. 只有当新增请求记录出现真实 `402` 状态码时，程序才会立即执行重置，并把结果写入 `logs/app.log` 和 `logs/reset_history.jsonl`。

## 速度设计

- 自动开启后会预热浏览器并保持卡密页面，触发时不需要重新启动浏览器。
- 402 轮询间隔默认 0.25 秒，适配 Codex 额度不足后的 5 次重连窗口。
- 不从 AixHan 页面读取额度，也不根据页面剩余额度判断是否重置。
- ccswitch 额度卡片只用于展示，默认 120 秒刷新一次；它不参与重置触发判断。
- 自动重置优先由 ccswitch `proxy_request_logs` 新增 `402` 状态码触发，并用 Codex 运行日志 402 兜底。
- 目标模式会按“继续目标”恢复为 `active`，不会额外发送 `continue`；非目标 402 会话才按“继续 continue”批量 queue `continue`。
- `daily_max_resets` 大于 0 时会限制当天成功重置次数，达到上限后跳过自动重置。
- 浏览器操作统一进入单独的 Playwright 工作线程，避免多个后台线程同时控制页面导致 `Cannot switch to a different thread`。
- 浏览器启动顺序：Playwright Chromium -> Chrome -> Edge；如果本机没有浏览器，可运行 `install_browser.bat`。
- 重置动作使用页面按钮文字定位，兼容截图中的“重置额度”和“确认扣 1 天 并重置额度”。

## 配置说明

首次启动会生成 `config.json`。常用配置：

- `daily_limit_usd`: 每日额度，默认 50。
- `daily_max_resets`: 当日最大成功重置次数，默认 0 表示不限制。
- `aixhan_card_key`: AixHan 卡密；程序会在页面要求卡密时自动填入并确认。
- `poll_interval_seconds`: ccswitch 402 轮询秒数，默认 0.25。
- `ccswitch_usage_refresh_seconds`: ccswitch 额度展示刷新秒数，默认 120。
- `card_action_delay_ms`: 打开/切换到卡密页后，填卡密与点击确认之间的等待时间，默认 1500ms；如果页面加载慢可以调到 2500~3000。
- `reset_when_remaining_lte`: 保留配置项，当前策略下不参与自动触发。
- `codex_logs_sqlite`: Codex 日志数据库，默认 `%USERPROFILE%\.codex\logs_2.sqlite`。
- `ccswitch_db`: ccswitch 数据库，默认 `%USERPROFILE%\.cc-switch\cc-switch.db`。
- `browser_profile_dir`: Playwright 登录态目录，默认 `data/aixhan_browser_profile`。
- `browser_channel`: 默认 `auto`；也可以指定 `chrome` 或 `msedge`。
- `headless`: 默认 false，方便首次输入卡密和观察；稳定后可改 true。
- `auto_continue_after_reset`: 是否对非目标 402 会话发送 `continue`。
- `auto_resume_goal_after_reset`: 是否恢复目标模式；目标模式只恢复目标，不发送 `continue`。

## 验证

```bat
python -m py_compile app.py
python app.py
```

建议在真正跑长任务前手动点一次“立即重置”做页面按钮实测；自动触发判断只看 ccswitch 402。

## 隐藏启动与图标

- `start_hidden.vbs`: 使用 `pythonw.exe` 后台启动 GUI，避免显示命令行窗口。
- `assets/aixhan_quota.ico`: 程序与快捷方式图标。
- `create_shortcut.ps1`: 重新生成项目目录/桌面的 `AixHan Quota Auto Reset.lnk`，并把桌面旧的 `start.bat - 快捷方式.lnk` 指向隐藏启动器和新图标。
