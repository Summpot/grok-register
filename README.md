<div align="center">

[![Grok Register Mint — GUI and CLI registration + Grok Build CPA pipeline](assets/banner.png)](https://github.com/van7517/grok-register-mint)

**Grok Register Mint** — 面向 Windows 的 Grok 账号注册自动化工具（二次开发版）  
支持 GUI / CLI、临时邮箱、多线程批量注册、异步 **Grok Build / Free Build（CPA）** mint，以及可选写入 grok2api 与 CPA Manager Plus。

<p>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/Python-3.13-3776AB.svg" alt="Python 3.13">
  <img src="https://img.shields.io/badge/Interface-GUI%20%2B%20CLI-success.svg" alt="GUI + CLI">
  <img src="https://img.shields.io/badge/Browser-Chromium%2FChrome-4285F4.svg" alt="Chromium/Chrome">
  <img src="https://img.shields.io/badge/CPA-Grok%20Build%20%2F%20Free%20Build-orange.svg" alt="Grok Build / Free Build">
  <a href="https://github.com/AaronL725/grok-register"><img src="https://img.shields.io/badge/Upstream-AaronL725%2Fgrok--register-lightgrey.svg" alt="Upstream"></a>
</p>

</div>

---

> **免责声明**：本项目仅用于自动化流程研究、测试环境验证和个人学习。请遵守目标网站服务条款、当地法律法规和第三方服务限制。滥用风险自负。

## 关于本仓库

本仓库基于上游 [AaronL725/grok-register](https://github.com/AaronL725/grok-register) **二次开发**，在原有 GUI / CLI 注册能力上扩展了批量流水线与 Grok Build 认证产出。

| | 上游原版 | 本仓库（Mint） |
| --- | --- | --- |
| 定位 | 注册自动化（GUI / CLI） | 注册 + **异步 CPA mint 流水线** |
| 推荐入口 | `grok_register_ttk.py` | **`register_cli.py`** |
| Python | 文档曾写 3.9+ | **3.13**（`>=3.13,<3.14`） |
| CPA / Grok Build | 无完整 mint 流水线 | OAuth mint → `output/cpa_auths/xai-*.json` |
| 并发模型 | 偏单机/配置线程 | 注册线程 R + mint workers M，峰值浏览器约 **R + M** |
| 云端 | 可选 grok2api | grok2api + 可选 **CPA Manager Plus** 批量上传 |

上游项目与社区讨论仍见 [AaronL725/grok-register](https://github.com/AaronL725/grok-register) 与 [linux.do](https://linux.do)。本 README 只描述 **本仓库当前行为**。

当前 CPA 通道是 **Grok Build / Free Build**：

- `cpa_base_url` 应为 `https://cli-chat-proxy.grok.com/v1`
- **不是** `api.x.ai` 付费 API
- CPAMP 上「额度」可能为空；billing 常为 0，真实限流多见于调用响应头

## 目录

- [功能](#功能)
- [环境要求](#环境要求)
- [安装](#安装)
- [必须配置](#必须配置)
- [常用可选配置](#常用可选配置)
- [启动命令](#启动命令)
- [多线程 / 批量](#多线程--批量)
- [CLI 参数](#cli-参数)
- [运行流程](#运行流程)
- [输出文件](#输出文件)
- [实测参考](#实测参考)
- [常见问题](#常见问题)
- [目录结构](#目录结构)
- [安全提示](#安全提示)
- [开发与检查](#开发与检查)
- [License 与致谢](#license-与致谢)


## 目录布局

为避免运行产物和工具脚本堆在根目录，本仓库约定：

| 位置 | 内容 |
| --- | --- |
| 根目录 | 入口脚本、`config.json`、核心模块、`turnstilePatch/` |
| `output/` | 账号、CPA 认证、token 池、导出、日志类运行产物 |
| `scripts/` | 回填、配置校验、GUI/CLI 辅助启动、一次性补丁 |
| `docs/` | 本机/打包类说明（主文档仍是 `README.md`） |
| `tests/` | 单元测试 |

默认 `cpa_auth_dir`、`accounts` 输出等均指向 `output/`。旧版根目录下的 `cpa_auths/`、`accounts_*.txt` 已被 gitignore，新跑任务请用新路径。

## 功能

- GUI（Tkinter）与 CLI 两种运行方式
- Cloudflare / DuckMail / YYDS 临时邮箱
- Chrome / Chromium 真实浏览器注册流程（含 Turnstile 扩展 `turnstilePatch/`）
- **多线程批量注册 + 异步 CPA / Grok Build OAuth mint**
- 成功账号实时写入 `output/accounts_*.txt` / `output/accounts_cli.txt`
- 可选写入本地 / 远端 grok2api token 池
- 可选批量上传 CPA 认证到 CPA Manager Plus（CPAMP）
- 可选 sub2api 格式导出、本地热加载目录复制
- 页面卡住检测、邮箱重试、浏览器复用与回收

## 环境要求

- **Python 3.13**（`pyproject.toml` 要求 `>=3.13,<3.14`）
- Google Chrome 或 Chromium
- 桌面环境（GUI / CLI 都会启动真实浏览器）
- 可访问：
  - `accounts.x.ai` / Grok 注册页
  - 你的临时邮箱 API
  - （可选）远端 grok2api、CPA Manager Plus

推荐工具链：

- [`uv`](https://github.com/astral-sh/uv) 管理依赖（按 `uv.lock`）
- 可选 [`mise`](https://mise.jdx.dev/) 管理 Python 与任务

## 安装

```powershell
git clone https://github.com/van7517/grok-register-mint.git
cd grok-register-mint

# 推荐：按锁文件安装
uv sync

# 兼容方式（非首选）
# pip install -r requirements.txt

# 复制配置
copy config.example.json config.json
```

然后编辑 `config.json`，填写下方「必须配置」。

> `config.json` 含密钥，**不要**提交到 Git。  
> 所有命令从**项目根目录**执行。其他机器上的绝对路径（如 `RUN_LOCAL.md`）请勿照抄。

## 必须配置

下面这些是跑通注册 / 出 CPA 通常必须正确的项。完整字段见 `config.example.json`。

### 1. 临时邮箱（必填）

默认通道：Cloudflare Temp Mail（[dreamhunter2333/cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email)）。

#### 方案 A：匿名模式（示例默认）

```json
{
  "email_provider": "cloudflare",
  "cloudflare_api_base": "https://你的-邮箱-API域名",
  "cloudflare_api_key": "",
  "cloudflare_auth_mode": "none",
  "cloudflare_path_domains": "/api/domains",
  "cloudflare_path_accounts": "/api/new_address",
  "cloudflare_path_token": "/api/token",
  "cloudflare_path_messages": "/api/mails",
  "defaultDomains": "你的收信域名.com"
}
```

#### 方案 B：Admin 模式（匿名接口开了 Turnstile 时推荐）

```json
{
  "email_provider": "cloudflare",
  "cloudflare_api_base": "https://你的-邮箱-API域名",
  "cloudflare_api_key": "你的 ADMIN_PASSWORD",
  "cloudflare_auth_mode": "x-admin-auth",
  "cloudflare_path_accounts": "/admin/new_address",
  "cloudflare_path_messages": "/api/mails",
  "defaultDomains": "你的收信域名.com"
}
```

说明：

- `cloudflare_api_base`：邮箱 Worker / API 根地址
- `defaultDomains`：实际收信域名，必须和邮箱服务一致
- Admin 密码只用于创建邮箱；读信仍用接口返回的 JWT

先测邮箱是否通：

```powershell
uv run python cf_mail_debug.py `
  --api-base "https://你的-邮箱-API域名" `
  --auth-mode x-admin-auth `
  --api-key "你的 ADMIN_PASSWORD" `
  --create-path /admin/new_address `
  --domain "你的收信域名.com"
```

### 2. 注册数量 / 并发（强烈建议）

```json
{
  "register_count": 100,
  "register_threads": 2,
  "thread_start_interval": 0.8,
  "max_mail_retry": 3,
  "code_poll_timeout": 90,
  "code_poll_interval": 3
}
```

| 字段 | 必须性 | 说明 |
| --- | --- | --- |
| `register_count` | 建议 | 默认目标数量；CLI 可用 `--count` 覆盖 |
| `register_threads` | 建议 | GUI/部分逻辑读取；**CLI 真正并发以 `--threads` 为准** |
| `code_poll_timeout` | 建议 | 等验证码超时秒数，批量建议 60–90 |
| `code_poll_interval` | 可选 | 轮询间隔秒数 |

### 3. Grok Build / CPA 通道（要产出 `xai-*.json` 时必填）

```json
{
  "cpa_export_enabled": true,
  "cpa_auth_dir": "./output/cpa_auths",
  "cpa_base_url": "https://cli-chat-proxy.grok.com/v1",
  "cpa_force_standalone": true,
  "cpa_mint_timeout_sec": 300,
  "cpa_mint_required": false,
  "cpa_mint_cookie_inject": true,
  "cpa_mint_browser_reuse": true,
  "cpa_mint_browser_recycle_every": 8
}
```

| 字段 | 必须性 | 说明 |
| --- | --- | --- |
| `cpa_export_enabled` | 必填（若要 CPA） | `true` 才 mint OAuth 并写 `output/cpa_auths/xai-*.json` |
| `cpa_auth_dir` | 必填（若要 CPA） | 本地 CPA 认证文件输出目录 |
| `cpa_base_url` | 必填（若要 CPA） | 必须是 `https://cli-chat-proxy.grok.com/v1`（Free Build） |
| `cpa_mint_required` | 可选 | `true` 时 mint 失败更严格；默认 `false` 不阻断注册结果 |

### 4. 远端 grok2api（可选）

```json
{
  "grok2api_auto_add_local": true,
  "grok2api_local_token_file": "./output/grok2api_tokens.json",
  "grok2api_pool_name": "ssoBasic",
  "grok2api_auto_add_remote": true,
  "grok2api_remote_base": "http://你的服务器:5003",
  "grok2api_remote_app_key": "你的管理密钥"
}
```

`grok2api_remote_base` 可填站点根（`http://IP:5003`）或管理 API（`http://IP:5003/admin/api`）。  
程序会优先尝试 `/tokens/add`，并兼容 `/admin/api/tokens/add` 等旧路径。

### 5. 上传到 CPA Manager Plus（可选）

```json
{
  "cpa_cloud_upload_enabled": true,
  "cpa_cloud_api_base": "http://你的服务器:50001",
  "cpa_cloud_management_key": "你的 CPAMP 管理密钥",
  "cpa_cloud_upload_timeout": 30,
  "cpa_cloud_upload_retries": 3
}
```

关闭时只会把认证文件写到本地 `output/cpa_auths/`。

### 6. 代理（按网络情况）

```json
{
  "proxy": "",
  "cpa_proxy": ""
}
```

- `proxy`：注册浏览器 / 邮箱请求代理
- `cpa_proxy`：CPA mint 专用代理；空则跟随主流程

访问不了 `accounts.x.ai` 时必须配置可用代理。

## 常用可选配置

| 字段 | 默认建议 | 说明 |
| --- | --- | --- |
| `enable_nsfw` | `false` | 注册后尝试开 NSFW；常被 Cloudflare 403，不影响出号 |
| `user_agent` | 保持示例 | 浏览器 UA |
| `cpa_probe_after_write` | 按需 | mint 后探测模型；批量可关以加快速度 |
| `cpa_probe_chat` | `false` | 额外聊天探测，更慢 |
| `cpa_copy_to_hotload` | `false` | 复制到本地 CPA 热加载目录 |
| `cpa_hotload_dir` | 空 | 热加载目录路径 |
| `sub2api_export_enabled` | 按需 | 导出 sub2api 格式 |
| `yyds_*` / `duckmail_api_key` | 按需 | 换邮箱供应商时使用 |

### 最小可跑示例（本地出号 + 本地 CPA）

```json
{
  "email_provider": "cloudflare",
  "cloudflare_api_base": "https://mail.example.com",
  "cloudflare_api_key": "ADMIN_PASSWORD",
  "cloudflare_auth_mode": "x-admin-auth",
  "cloudflare_path_accounts": "/admin/new_address",
  "defaultDomains": "example.com",
  "register_count": 10,
  "code_poll_timeout": 90,
  "cpa_export_enabled": true,
  "cpa_auth_dir": "./output/cpa_auths",
  "cpa_base_url": "https://cli-chat-proxy.grok.com/v1",
  "grok2api_auto_add_local": true,
  "grok2api_local_token_file": "./output/grok2api_tokens.json",
  "grok2api_auto_add_remote": false,
  "cpa_cloud_upload_enabled": false
}
```

## 启动命令

所有命令在项目根目录执行。

### GUI

```powershell
uv run python grok_register_ttk.py
# 或
mise run gui
```

适合改配置、看日志；**批量更推荐 CLI**。

### CLI 单账号试跑（推荐先跑通）

```powershell
uv run python -u register_cli.py --count 1 --threads 1
```

### 旧 CLI 入口（兼容，非完整 mint 流水线）

```powershell
uv run python grok_register_ttk.py cli
```

进入后输入 `start` 开始。批量请用 `register_cli.py`。

### mise 快捷

```powershell
mise run deps
mise run register
mise run register -- --count 10
mise run batch10
mise run check
```

## 多线程 / 批量

### 常用：2 线程注册 + 自动 mint

```powershell
uv run python -u register_cli.py --count 100 --threads 2 --fast
```

- `--threads 2`：2 个注册浏览器并发
- 开启 `cpa_export_enabled` 时，mint workers 默认 **auto = min(threads, 4)**
- 因此 2 线程时通常是：注册 2 + mint 2

### 明确指定 mint 并发

```powershell
uv run python -u register_cli.py --count 100 --threads 2 --mint-workers 2 --fast
```

### 更高并发（机器和邮箱扛得住再上）

```powershell
uv run python -u register_cli.py --count 100 --threads 4 --mint-workers 4 --fast
```

注意：

- 线程越高，Chrome 内存占用越高
- Grok OAuth 可能出现 `rate_limited`
- 建议从 `2` 开始，稳定后再加

### 指定账号输出文件

```powershell
uv run python -u register_cli.py `
  --count 100 `
  --threads 2 `
  --mint-workers 2 `
  --accounts-file ".\output\accounts_batch100.txt" `
  --fast
```

### 在已有账号文件上再追加 N 个

```powershell
uv run python -u register_cli.py `
  --extra 50 `
  --threads 2 `
  --mint-workers 2 `
  --accounts-file ".\output\accounts_batch100.txt" `
  --fast
```

`--count` 表示「文件最终总行数目标」；已有 100 行时 `--count 100` 会直接结束。续跑用 `--extra`。

## CLI 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--count N` | `1` | 账号总数目标（含已有文件行数；`0`=不限） |
| `--extra N` | `0` | 在已有账号基础上再新注册 N 个 |
| `--threads N` | `1` | 注册并发线程（1–10） |
| `--mint-workers N` | `-1` | CPA mint 并发：`-1` 自动；`0` 内联；`1–10` 固定 |
| `--mint-queue-max N` | `-1` | mint 队列背压；`-1` 自动（约 `2 × mint_workers`） |
| `--accounts-file PATH` | `output/accounts_cli.txt` | 账号输出文件 |
| `--fast` | 默认开 | 压缩等待、减少调试 IO |
| `--no-fast` | 关 | 关闭快速模式 |
| `--no-browser-reuse` | 关 | 每号强制关闭浏览器 |
| `--browser-recycle-every N` | `25` | 浏览器复用 N 次后完整回收 |
| `--cookie-snapshot` | 关 | 注册成功额外写 cookie 快照 |
| `--inline-mint` | 关 | 强制注册线程内联 mint |

峰值浏览器数约 **R + M**（注册线程 + mint workers）。

## 运行流程

`register_cli.py` 流水线：

```text
注册线程 (R)
  → 打开 xAI 注册页
  → 创建临时邮箱 / 收验证码
  → 完成注册，拿到 SSO
  → 写入 accounts 文件
  → （可选）写 grok2api 本地/远端池
  → 把账号丢进 mint 队列

mint 线程 (M)
  → Grok Build OAuth device flow
  → 写出 output/cpa_auths/xai-邮箱.json
  → （可选）批量上传到 CPA Manager Plus
```

因此：

- **只看账号文件**：注册成功即可
- **要 CPA / Grok Build 调用**：还要等 mint 成功

日志末尾示例：

```text
=== 完成: 注册成功 100, 注册失败 0, CPA成功 100, CPA失败 0, CPA跳过 0, 云上传成功 100, 云上传失败 0 ===
```

## 输出文件

| 文件/目录 | 内容 |
| --- | --- |
| `output/accounts_*.txt` / `output/accounts_cli.txt` | `邮箱----密码----ssoToken` |
| `output/cpa_auths/xai-*.json` | CPA / Grok Build OAuth 认证文件 |
| `output/grok2api_tokens.json` | 本地 grok2api token 池（若开启） |
| `output/mail_credentials.txt` | 临时邮箱凭证 |
| `batch*_run_*.log` | 批量运行日志 |
| `output/sub2api_exports/` | sub2api 导出（若开启） |

这些文件含敏感信息，默认已被 `.gitignore` 忽略。

## 实测参考

本机配置示例：`--count 100 --threads 2 --mint-workers 2 --fast`

| 批次 | 耗时 | 注册 | CPA | 云上传 |
| --- | --- | --- | --- | --- |
| 参考 A | ~21.7 分钟 | 100（失败 3） | 100 | 100 |
| 参考 B | **~18.4 分钟** | **100（失败 0）** | **100** | **100** |

粗算约 **5.4–5.9 个/分钟**（约 11 秒/账号，含 mint 与上传）。  
实际速度取决于邮箱验证码延迟、Turnstile、OAuth 限流、机器与代理。

## 常见问题

### 1. CLI 为什么还弹浏览器？

注册页、Turnstile、SSO cookie、Grok Build device 授权都依赖真实 Chrome/Chromium。CLI 只是不启动 Tk 窗口。

### 2. 出了账号但没有 `xai-*.json`？

检查：

- `cpa_export_enabled` 是否为 `true`
- 日志里 mint 是否失败 / `rate_limited`
- `cpa_auth_dir` 是否可写

### 3. CPA 认证文件有了，但额度显示为空？

这是 Grok Build（`cli-chat-proxy.grok.com`）常见现象：billing 可通，但 `monthlyLimit` / `onDemandCap` 经常为 0。业务是否可用应看调用成功率和 rate-limit 响应头，不是认证列表上的「额度条」。

### 4. 多线程会不会更快？

通常 `2–4` 线程有收益；再高可能被邮箱、Turnstile、OAuth `rate_limited`、CPU/内存卡住。

### 5. 如何只注册、不 mint CPA？

```json
{
  "cpa_export_enabled": false
}
```

### 6. 配置改了不生效？

- 确认改的是项目根目录 `config.json`
- CLI 的 `--count` / `--threads` 会覆盖配置中的数量/并发
- 不要改错目录下的备份配置

### 7. NSFW 开启失败？

日志出现 `Cloudflare 防护拦截，HTTP 403` 时，程序仍会保存账号并继续后续流程。

## 目录结构

```text
.
├── register_cli.py          # 推荐：多线程注册 + 异步 CPA mint
├── grok_register_ttk.py     # 核心逻辑 / GUI / 兼容 CLI
├── tab_pool.py              # 浏览器 / 标签页复用
├── cpa_export.py            # CPA 导出
├── cpa_to_sub2api.py        # sub2api 格式转换
├── cf_mail_debug.py         # Cloudflare 邮箱调试
├── cpa_xai/                 # Grok Build OAuth mint
├── turnstilePatch/          # Chromium 扩展（勿删除）
├── output/                  # 运行产物（gitignore）
│   ├── accounts_*.txt
│   ├── accounts_cli.txt
│   ├── cpa_auths/           # xai-*.json
│   ├── sub2api_exports/
│   └── grok2api_tokens.json
├── scripts/                 # 回填 / 校验 / 启动辅助
├── docs/                    # 本地说明（非主文档）
├── tests/                   # unittest
├── assets/                  # banner 等
├── config.example.json      # 配置模板
├── config.json              # 本地私有配置（勿提交）
├── pyproject.toml
├── uv.lock
├── mise.toml
├── requirements.txt
└── README.md                # 本说明（完整中文）
```

## 安全提示

- 不要提交或泄露：`config.json`、API Key、JWT、Cookie、密码、SSO Token、代理凭据、账号文件、`output/cpa_auths/`、管理密钥
- 管理后台（CPAMP / grok2api）不要长期裸奔公网；建议本机端口或 Tunnel / 反代
- 遵守目标站点条款与当地法律

## 开发与检查

普通代码修改后优先：

```powershell
uv run python -m unittest discover -s tests -v
uv run python -m py_compile grok_register_ttk.py register_cli.py cf_mail_debug.py
# 或
mise run check
```

- 未配置 Ruff / Black / mypy / pytest 为必需项
- `optimization_checks.py`、`verify_config_safe.py` 可能访问外部服务，不宜当作离线 CI
- 不要为验证普通改动而跑真实注册 / mint / 云上传

新增配置键时，请同步更新：**代码默认值**、`config.example.json`、本 README。

## License 与致谢

- License：[MIT](LICENSE)
- 上游原版：[AaronL725/grok-register](https://github.com/AaronL725/grok-register) — 本项目在其基础上二次开发
- 社区讨论：[linux.do](https://linux.do)
