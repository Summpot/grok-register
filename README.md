<div align="center">

[![Grok Register — CLI registration + optional grok2api upload](assets/banner.png)](https://github.com/van7517/grok-register-mint)

**Grok Register** — 面向 Windows 的 Grok 账号注册自动化工具（二次开发版）  
支持 CLI、临时邮箱、多线程批量注册，以及可选写入 **grok2api**（Web 池；可选本地 Device Flow 后导入 Build）。

<p>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/Python-3.13-3776AB.svg" alt="Python 3.13">
  <img src="https://img.shields.io/badge/Interface-CLI-success.svg" alt="CLI">
  <img src="https://img.shields.io/badge/Browser-Chromium%2FChrome-4285F4.svg" alt="Chromium/Chrome">
  <img src="https://img.shields.io/badge/Upload-grok2api%20Web-orange.svg" alt="grok2api Web">
  <a href="https://github.com/AaronL725/grok-register"><img src="https://img.shields.io/badge/Upstream-AaronL725%2Fgrok--register-lightgrey.svg" alt="Upstream"></a>
</p>

</div>

---

> **免责声明**：本项目仅用于自动化流程研究、测试环境验证和个人学习。请遵守目标网站服务条款、当地法律法规和第三方服务限制。滥用风险自负。

## 关于本仓库

本仓库基于上游 [AaronL725/grok-register](https://github.com/AaronL725/grok-register) **二次开发**，聚焦批量注册与 **grok2api Web** 自动入池。

| | 上游原版 | 本仓库 |
| --- | --- | --- |
| 定位 | 注册自动化（GUI / CLI） | 注册 + 可选 **grok2api Web** 上传（**仅 CLI**） |
| 推荐入口 | `grok_register_ttk.py` | **`register_cli.py`** |
| Python | 文档曾写 3.9+ | **3.13**（`>=3.13,<3.14`） |
| 上传 | 可选 | 本地/远端 grok2api（v3/legacy；默认关） |
| 并发模型 | 偏单机/配置线程 | 多注册线程，浏览器复用/回收 |

上游项目与社区讨论仍见 [AaronL725/grok-register](https://github.com/AaronL725/grok-register) 与 [linux.do](https://linux.do)。本 README 只描述 **本仓库当前行为**。

主路径：注册拿 SSO → 写 `accounts_*.txt` →（可选）上传 grok2api Web 池 →（可选）本地 Device Flow 后导入 Build 凭据。

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

| 位置 | 内容 |
| --- | --- |
| 根目录 | 薄启动入口、`config.json`、依赖清单、`turnstilePatch/`、文档 |
| `grok_register/` | **核心 Python 包**（CLI/邮箱/浏览器池/grok2api） |
| `output/` | 运行产物：账号、token（gitignore） |
| `scripts/` | 回填、校验、辅助启动 |
| `docs/` | 本机/打包说明（主文档仍是 `README.md`） |
| `tests/` | unittest |

默认输出指向 `output/`。旧路径根目录 `accounts_*.txt` 仍被 gitignore。

### 启动方式（兼容旧命令）

```powershell
# 推荐（根目录兼容入口，内部转到包）
uv run python -u register_cli.py --count 1 --threads 1

# 等价包入口
uv run python -m grok_register.cli --count 1 --threads 1
```

## 功能

- CLI 多线程批量注册
- Cloudflare / Exchange（@*.onmicrosoft.com）/ DuckMail / YYDS 临时邮箱
- Chrome / Chromium 真实浏览器注册流程（含 Turnstile 扩展 `turnstilePatch/`）
- 成功账号实时写入 `output/accounts_*.txt` / `output/accounts_cli.txt`
- 可选写入本地 / 远端 **grok2api Web** 池（**默认关闭**；Build 仅走本地 Device Flow 后导入）
- 页面卡住检测、邮箱重试、浏览器复用与回收

## 环境要求

- **Python 3.13**（`pyproject.toml` 要求 `>=3.13,<3.14`）
- Google Chrome 或 Chromium
- 可运行浏览器自动化的环境（CLI 会启动真实浏览器）
- 可访问：
  - `accounts.x.ai` / Grok 注册页
  - 你的临时邮箱 API
  - （可选）远端 grok2api

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

下面这些是跑通注册通常必须正确的项。完整字段见 `config.example.json`。

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
  "defaultDomains": "你的收信域名.com",
  "enable_random_subdomain": true
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
  "defaultDomains": "你的收信域名.com",
  "enable_random_subdomain": true
}
```

说明：

- `cloudflare_api_base`：邮箱 Worker / API 根地址
- `defaultDomains`：基础收信域名（逗号分隔），必须与 Worker `DOMAINS` / `RANDOM_SUBDOMAIN_DOMAINS` 一致
- `enable_random_subdomain`：`true` 时每个账号创建 `name@随机串.基础域`（cloudflare_temp_email 的 `enableRandomSubdomain`）
- Admin 密码只用于创建邮箱；读信仍用接口返回的 JWT

随机三级域名前置条件（在 temp-mail / Cloudflare 侧完成，非本仓库配置）：

1. Worker 变量 `RANDOM_SUBDOMAIN_DOMAINS` 包含 `defaultDomains` 中的基础域
2. 基础域 DNS 已配置通配 MX（`*` 复制 apex 的 Email Routing MX）
3. Catch-all 已绑定到该 Worker

先测邮箱是否通：

```powershell
uv run python cf_mail_debug.py `
  --api-base "https://你的-邮箱-API域名" `
  --auth-mode x-admin-auth `
  --api-key "你的 ADMIN_PASSWORD" `
  --create-path /admin/new_address `
  --domain "你的收信域名.com" `
  --random-subdomain
```

#### 方案 C：Exchange Online catch-all（`@tenant.onmicrosoft.com`）

**不创建用户**。流程：随机生成 `tmp…@域名` → 邮件进固定 catch-all 邮箱 → Graph 按收件人过滤取验证码。与 Cloudflare 通道互斥，改 `email_provider` 即可切换：

```json
{
  "email_provider": "exchange",
  "exchange_tenant_id": "你的-Directory-(tenant)-ID",
  "exchange_client_id": "应用(客户端)ID",
  "exchange_client_secret": "客户端密码",
  "exchange_mailbox": "catchall@你的租户.onmicrosoft.com",
  "exchange_domains": "你的租户.onmicrosoft.com"
}
```

切回 Cloudflare Temp Mail：

```json
{
  "email_provider": "cloudflare"
}
```

**租户侧前置：**

1. 在 Exchange / M365 为域名开启 **catch-all**（或传输规则：未知收件人转发到 `exchange_mailbox`）
2. Entra ID → 应用注册 → 记下 **应用程序(客户端) ID** 与 **目录(租户) ID**
3. 证书和密码 → 新建客户端密码 → `exchange_client_secret`
4. API 权限 → Microsoft Graph **应用程序权限** `Mail.Read` → **管理员同意**（需能读 catch-all 邮箱）
5. `exchange_domains` 填 catch-all 域（如 `xxx.onmicrosoft.com`）；留空时回退 `defaultDomains`

| 字段 | 说明 |
| --- | --- |
| `exchange_tenant_id` / `client_id` / `client_secret` | 应用身份（client credentials） |
| `exchange_mailbox` | catch-all **实际收信邮箱** 的 UPN 或对象 ID（Graph 轮询目标） |
| `exchange_domains` | 对外使用的域名，逗号分隔，如 `contoso.onmicrosoft.com` |
| `exchange_username_prefix` / `length` | 随机本地部分，默认 `tmp` + 12 位 |
| `exchange_list_top` | 每轮拉取最近邮件条数，默认 50 |

> 并发时多个随机地址共用同一 catch-all 邮箱；程序按 `toRecipients` / 原始收件人头匹配目标地址，互不干扰。

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
| `register_threads` | 建议 | 默认并发参考；**CLI 真正并发以 `--threads` 为准** |
| `register_browser_background` | 可选 | 默认 `true`：注册浏览器后台运行、不抢前台 |
| `register_browser_background_mode` | 可选 | 默认 `headless`（Camoufox 无窗口，推荐）；`offscreen` 为有界面+屏外（可能闪一下） |
| `register_browser_window_position` | 可选 | `offscreen` 模式用，默认 `-2400,100` |
| `register_browser_window_size` | 可选 | 默认 `1000,800` |
| `code_poll_timeout` | 建议 | 等验证码超时秒数，批量建议 60–90 |
| `code_poll_interval` | 可选 | 轮询间隔秒数 |

### 3. 远端 grok2api（可选，**默认关闭**）

本地 Web 池与远端上传默认均为 **关闭**（`grok2api_auto_add_local` / `grok2api_auto_add_remote` / `grok2api_auto_add_build` = `false`）。需要时再打开。

支持两种远端：

| mode | 目标 | 认证 | 接口 |
| --- | --- | --- | --- |
| `v3` | [chenyme/grok2api](https://github.com/chenyme/grok2api) Go v3 | 管理员用户名/密码登录拿 Bearer | Web: `POST /api/admin/v1/accounts/web/import`；Build: 本地 Device Flow 后 `accounts` 导入 |
| `legacy` | 旧版 Python / jiujiu 池 | `app_key` 查询参数 | `/tokens/add`、`/admin/api/tokens/add` 等 |
| `auto`（默认） | 先 v3，失败再 legacy | 两种都配齐时可用 | 自动降级 |

```json
{
  "grok2api_auto_add_local": false,
  "grok2api_local_token_file": "./output/grok2api_tokens.json",
  "grok2api_pool_name": "ssoBasic",
  "grok2api_auto_add_remote": true,
  "grok2api_auto_add_build": true,
  "grok2api_remote_base": "http://你的服务器:5003",
  "grok2api_remote_mode": "auto",
  "grok2api_remote_username": "admin",
  "grok2api_remote_password": "v3管理密码",
  "grok2api_remote_app_key": "legacy app_key（仅旧版）",
  "grok2api_v3_web_tier": "auto"
}
```

说明：

- **v3 Web**：注册成功后的 SSO JWT 作为 Grok Web 账号导入（`grok2api_auto_add_remote`）。
- **远端 Build**：本地 Device Flow 拿到 OAuth 后导入 Build 池（`grok2api_auto_add_build`）；**不再**调用远端 `web/convert-to-build`。
- Web tier：`ssoBasic`→`basic`，`ssoSuper`→`super`，可用 `grok2api_v3_web_tier` 覆盖。
- **legacy**：`grok2api_remote_base` 可填站点根或 `/admin/api`；优先 `/tokens/add`。
- 打开远端：设 `grok2api_auto_add_remote: true`，并填 `remote_base` +（v3 密码 **或** legacy `app_key`）。
- **bot 标记**：Device Flow 后若 Build `access_token` 含 `bot_flag_source=1`，默认视为失败且不上传；设 `allow_bot_flagged: true` 可仍保存/导入（部分场景账号仍可用）。
- **注册参数采集**（分析 bot_flag / 失败原因）：默认开启，每次尝试写入 `output/reg_stats.jsonl`（含 Turnstile 鼠标路径随机数、pace 时延参数、点击次数、代理 host、邮箱域名、JWT 安全 claims 子集等，**不含**密码/完整 token）。
- **成功口径**（primary）：
  - `build_clean`：拿到无 `bot_flag` 的 Build token（真正成功）
  - `build_bot`：Build token 含 `bot_flag_source=1`
  - `web_only`：有 SSO / 可能写账号文件，但无干净 Build token
- **放慢节奏**（`register_pace_*`）：阶段 dwell + 账号间隔；参数写入 `pace` / `pace_summary`。
- **禁用邮箱域**：`email_blocked_domains`（后缀匹配），默认暂时禁用 `ohmyaitrash.cloud`。

```json
{
  "reg_stats_enabled": true,
  "reg_stats_file": "output/reg_stats.jsonl",
  "register_pace_enabled": true,
  "register_pace_scale": 1.6,
  "register_pace_between_accounts_s": [12, 28],
  "email_blocked_domains": "ohmyaitrash.cloud"
}
```

分析汇总：

```bash
python -m grok_register.reg_stats
python -m grok_register.reg_stats --file output/reg_stats.jsonl --json
```

### 4. 代理（按网络情况）

```json
{
  "proxy": ""
}
```

- `proxy`：注册浏览器 / 邮箱请求代理

### 代理池（可选）

```json
{
  "proxy_pool_enabled": true,
  "proxy_pool_file": "all_proxies.txt",
  "proxy_pool_mode": "round_robin",
  "proxy_pool_rotate_each_account": true
}
```

- `all_proxies.txt`：每行一个代理，支持 `http://user:pass@host:port`
- 启用后**每个账号**从池中取代理（注册浏览器绑定）
- 带账号密码的代理通过临时 Chrome 扩展注入认证（Chromium 本身不支持 URL 内嵌 user:pass）

访问不了 `accounts.x.ai` 时必须配置可用代理。

## 常用可选配置

| 字段 | 默认建议 | 说明 |
| --- | --- | --- |
| `enable_random_subdomain` | `true`（示例） | 每个账号 `name@随机串.基础域`；需 Worker `RANDOM_SUBDOMAIN_DOMAINS` + 通配 MX |
| `enable_nsfw` | `false` | 注册后尝试开 NSFW；常被 Cloudflare 403，不影响出号 |
| `user_agent` | 保持示例 | 浏览器 UA |
| `yyds_*` / `duckmail_api_key` | 按需 | 换邮箱供应商时使用 |
| `email_provider` | `cloudflare` | `cloudflare` / `exchange` / `duckmail` / `yyds` |
| `exchange_*` | 按需 | Graph 临时邮箱；见上文「方案 C」 |

### 最小可跑示例（本地出号 + 远端 grok2api Web）

```json
{
  "email_provider": "cloudflare",
  "cloudflare_api_base": "https://mail.example.com",
  "cloudflare_api_key": "ADMIN_PASSWORD",
  "cloudflare_auth_mode": "x-admin-auth",
  "cloudflare_path_accounts": "/admin/new_address",
  "defaultDomains": "example.com",
  "enable_random_subdomain": true,
  "register_count": 10,
  "code_poll_timeout": 90,
  "grok2api_auto_add_local": false,
  "grok2api_auto_add_remote": true,
  "grok2api_auto_add_build": true,
  "grok2api_remote_base": "https://your-grok2api.example.com",
  "grok2api_remote_username": "admin",
  "grok2api_remote_password": "YOUR_PASSWORD"
}
```

## 启动命令

所有命令在项目根目录执行。

### CLI 单账号试跑（推荐先跑通）

```powershell
uv run python -u register_cli.py --count 1 --threads 1
```

### 交互式单线程 CLI（兼容）

```powershell
uv run python -m grok_register.app
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

### 常用：2 线程注册

```powershell
uv run python -u register_cli.py --count 100 --threads 2 --fast
```

- `--threads 2`：2 个注册浏览器并发
- 注册成功后按配置写入 grok2api（若已开启）

### 更高并发（机器和邮箱扛得住再上）

```powershell
uv run python -u register_cli.py --count 100 --threads 4 --fast
```

注意：

- 线程越高，Chrome 内存占用越高
- 建议从 `2` 开始，稳定后再加

### 指定账号输出文件

```powershell
uv run python -u register_cli.py `
  --count 100 `
  --threads 2 `
  --accounts-file ".\output\accounts_batch100.txt" `
  --fast
```

### 在已有账号文件上再追加 N 个

```powershell
uv run python -u register_cli.py `
  --extra 50 `
  --threads 2 `
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
| `--accounts-file PATH` | `output/accounts_cli.txt` | 账号输出文件 |
| `--fast` | 默认开 | 压缩等待、减少调试 IO |
| `--no-fast` | 关 | 关闭快速模式 |
| `--no-browser-reuse` | 关 | 每号强制关闭浏览器 |
| `--browser-recycle-every N` | `25` | 浏览器复用 N 次后完整回收 |
| `--cookie-snapshot` | 关 | 注册成功额外写 cookie 快照 |

峰值浏览器数约等于注册线程数 **R**。

## 运行流程

`register_cli.py` 流水线：

```text
注册线程 (R)
  → 打开 xAI 注册页
  → 创建临时邮箱 / 收验证码
  → 完成注册，拿到 SSO
  → 写入 accounts 文件
  → （可选）写 grok2api 本地/远端 Web 池
  → （可选）本地 Device Flow → 导入远端 Build 凭据
```

因此：

- **只要账号文件**：注册成功即可
- **要进 grok2api**：打开 `grok2api_auto_add_remote`（Web）与可选 `grok2api_auto_add_build`（本地 Build 导入）

日志末尾示例：

```text
=== 完成: 注册成功 100, 注册失败 0 ===
```

## 输出文件

| 文件/目录 | 内容 |
| --- | --- |
| `output/accounts_*.txt` / `output/accounts_cli.txt` | `邮箱----密码----ssoToken` |
| `output/grok2api_tokens.json` | 本地 grok2api token 池（若开启） |
| `output/mail_credentials.txt` | 临时邮箱凭证 |
| `batch*_run_*.log` | 批量运行日志 |

这些文件含敏感信息，默认已被 `.gitignore` 忽略。

## 实测参考

本机配置示例：`--count 100 --threads 2 --fast`

| 批次 | 耗时 | 注册 |
| --- | --- | --- |
| 参考 A | ~21.7 分钟 | 100（失败 3） |
| 参考 B | **~18.4 分钟** | **100（失败 0）** |

实际速度取决于邮箱验证码延迟、Turnstile、机器与代理。

## 常见问题

### 1. CLI 为什么还弹浏览器？

注册页、Turnstile、SSO cookie 依赖真实浏览器（Camoufox）。CLI 只是不启动 Tk 窗口。

默认 `register_browser_background=true` 且 `register_browser_background_mode=headless`：注册用 Camoufox **stealth headless**，**操作系统层不出现窗口**（无闪屏）。若要看见窗口，设 `register_browser_background: false`。若只要屏外有界面窗口（可能闪一下），设 `register_browser_background_mode: "offscreen"`。

### 2. 账号有了，但 grok2api 里没有？

检查：

- `grok2api_auto_add_remote` / `grok2api_auto_add_local` 是否为 `true`
- `grok2api_remote_base` 与 v3 密码（或 legacy `app_key`）是否正确
- 日志里是否有 `[+] 已写入 grok2api` 或跳过/失败信息

### 3. 多线程会不会更快？

通常 `2–4` 线程有收益；再高可能被邮箱、Turnstile、CPU/内存卡住。

### 4. 配置改了不生效？

- 确认改的是项目根目录 `config.json`
- CLI 的 `--count` / `--threads` 会覆盖配置中的数量/并发
- 不要改错目录下的备份配置

### 5. NSFW 开启失败？

日志出现 `Cloudflare 防护拦截，HTTP 403` 时，程序仍会保存账号并继续后续流程。

## 目录结构

```text
.
├── register_cli.py          # 薄入口 → grok_register.cli
├── cf_mail_debug.py         # 薄入口 → grok_register.cf_mail_debug
├── grok_register/           # 核心包
│   ├── app.py               # 注册主逻辑 + grok2api（CLI 共享）
│   ├── cli.py               # 多线程注册 CLI
│   ├── tab_pool.py
│   ├── cf_mail_debug.py
│   ├── paths.py             # 项目根 / output / config 路径
│   └── proxyutil.py         # 代理解析 / 代理池
├── turnstilePatch/          # Chromium 扩展（勿删除）
├── output/                  # 运行产物（gitignore）
├── scripts/
├── docs/
├── tests/
├── config.example.json
├── config.json              # 本地私有（勿提交）
├── pyproject.toml
├── uv.lock
├── mise.toml
├── requirements.txt
└── README.md
```

## 安全提示

- 不要提交或泄露：`config.json`、API Key、JWT、Cookie、密码、SSO Token、代理凭据、账号文件、管理密钥
- grok2api 管理后台不要长期裸奔公网；建议本机端口或 Tunnel / 反代
- 遵守目标站点条款与当地法律

## 开发与检查

普通代码修改后优先：

```powershell
uv run python -m unittest discover -s tests -v
uv run python -m py_compile register_cli.py cf_mail_debug.py grok_register/app.py grok_register/cli.py
# 或
mise run check
```

- 未配置 Ruff / Black / mypy / pytest 为必需项
- `scripts/optimization_checks.py`、`scripts/verify_config_safe.py` 可能访问外部服务；后者默认只打印脱敏配置，`--probe-mail` / `--probe-remote` 才会打真实网络，不宜当作离线 CI
- 不要为验证普通改动而跑真实注册 / 远端上传

新增配置键时，请同步更新：**代码默认值**、`config.example.json`、本 README。

## License 与致谢

- License：[MIT](LICENSE)（保留上游 AaronL725 版权，并标注本仓 van7517 修改）
- 上游原版：[AaronL725/grok-register](https://github.com/AaronL725/grok-register) — 本项目在其基础上二次开发
- 社区讨论：[linux.do](https://linux.do)
