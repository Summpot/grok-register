# AGENTS.md

## 适用范围

本文件中的规则适用于当前目录下的整个项目。

## 文档入口

- **用户文档（完整）**：`README.md` — 安装、必须配置、GUI/CLI 启动、多线程批量命令、CLI 参数、输出文件、FAQ；并说明基于上游二次开发。改运行说明时优先同步这里。
- **配置样例**：`config.example.json` — 与代码默认值保持一致。
- 添加配置键时：同步更新 **代码默认值**、`config.example.json`、`README.md`（以及本文件中相关要点）。

## 项目简介

这是一个主要面向 Windows 的 Python 3.13 自动化项目，用于 Grok 账号注册，并可继续 mint **Grok Build / Free Build**（CPA）认证文件，可选写入远端 grok2api 与 CPA Manager Plus（CPAMP）。

主要文件和目录：

- `register_cli.py`：**推荐 CLI 入口**。注册 worker 线程 + 异步 CPA mint 流水线（`--threads` / `--mint-workers`）。
- `grok_register_ttk.py`：核心逻辑 + Tkinter GUI；`python grok_register_ttk.py cli` 为**兼容/旧 CLI**，不含完整 async mint 流水线，批量请用 `register_cli.py`。
- `tab_pool.py`：每线程浏览器 / 标签页池。
- `cpa_xai/`：Grok Build OAuth、浏览器确认、凭据格式与写入、代理与探测。
- `cpa_export.py`、`cpa_to_sub2api.py`：注册后导出 / 格式转换。
- `scripts/`：历史回填、一次性工具。
- `tests/`：标准库 `unittest`。
- `turnstilePatch/`：浏览器流程依赖的 Chromium 扩展，不要移动或删除。

### 产品事实（改代码时勿搞错通道）

- 当前 CPA 通道是 **Grok Build / Free Build**：`cpa_base_url` 应为 `https://cli-chat-proxy.grok.com/v1`，**不是** `api.x.ai` 付费 API。
- CPAMP 上这类账号的「额度」可能为空；billing 常返回 0，真实限流多见于调用响应头。
- 不要假设 CPAMP 插件能自动显示 Free Build 额度；排查时区分认证文件格式 vs 上游 quota API。

## 环境和依赖

- 必须使用 Python **3.13**。`pyproject.toml` 要求 `>=3.13,<3.14`。README 徽章与文档均以 Python 3.13 为准。
- 推荐：`uv sync`（按 `uv.lock`）。
- 可选：`mise run deps`。
- 兼容：`pip install -r requirements.txt`（非首选）。
- 首次运行：复制 `config.example.json` → `config.json` 并填写真实配置。
- 命令必须从**本项目根目录**执行。`RUN_LOCAL.md` 中的绝对路径可能属于其他机器，勿照抄。

## 必须配置主题（勿在文档/提交中写真实密钥）

| 主题 | 关键字段 / 说明 |
| --- | --- |
| 临时邮箱（必填） | Cloudflare temp mail：`email_provider=cloudflare`；匿名或 Admin（`cloudflare_auth_mode=x-admin-auth` + `cloudflare_api_key`） |
| 并发 | `register_count` / `register_threads`；CLI 以 `--count` / `--threads` 为准 |
| Grok Build / CPA | `cpa_export_enabled`、`cpa_auth_dir`、`cpa_base_url=https://cli-chat-proxy.grok.com/v1` |
| 远端 grok2api（可选） | `grok2api_auto_add_remote`、`grok2api_remote_base`、`grok2api_remote_app_key` |
| CPAMP 云上传（可选） | `cpa_cloud_upload_enabled`、`cpa_cloud_api_base`、`cpa_cloud_management_key` |
| 代理（按网络） | 项目内 proxy 相关字段；批量时注意与浏览器一致 |

细节与 JSON 示例见 `README.md`「必须配置」章节。

## 安全的检查和测试

普通代码修改完成后，优先：

```powershell
uv run python -m unittest discover -s tests -v
uv run python -m py_compile grok_register_ttk.py register_cli.py cf_mail_debug.py
```

mise：

```powershell
mise run check
```

单测示例：

```powershell
uv run python -m unittest tests.test_cloudflare_admin_api -v
uv run python -m unittest tests.test_grok2api_remote_pool -v
```

- 项目未配置 Ruff / Black / mypy / pytest 为必需项；不要虚构这些检查，也不要对全仓做无关批量格式化。
- `optimization_checks.py`、`verify_config_safe.py` / `.ps1` 可能访问真实外部服务或依赖本机代理，**不能**当作离线/普通 CI 检查随意执行。

## 程序运行命令

**只有**在已正确配置外部服务、且任务确实需要时，才跑真实注册 / mint / 云上传。

### 推荐：`register_cli.py`

```powershell
# 单号试跑
uv run python -u register_cli.py --count 1 --threads 1

# 批量：2 注册线程 + 自动 mint workers（默认 min(threads,4)，需 cpa_export_enabled）
uv run python -u register_cli.py --count 100 --threads 2 --mint-workers 2 --fast

# 在已有账号文件上追加
uv run python -u register_cli.py --extra 50 --threads 2 --accounts-file .\accounts_batch100.txt

# mise
mise run register -- --count 10
```

- `--threads`：注册并发，通常 1–10。
- `--mint-workers`：CPA mint 并发；未指定且开启 `cpa_export_enabled` 时约 `min(threads, 4)`；`0` 表示注册线程内联 mint（更慢）。
- 峰值浏览器数约 **R + M**（注册线程 + mint workers）。
- 更多批量 / 断点续跑 / 参数表见 `README.md`。

### GUI / 兼容 CLI

```powershell
mise run gui
uv run python grok_register_ttk.py
uv run python grok_register_ttk.py cli   # 兼容入口，非完整 mint 流水线
```

注意事项：

- CLI 无 Tk 界面，但会启动真实 Chrome/Chromium。
- GUI 与浏览器自动化需要桌面环境。
- `cf_mail_debug.py` 需要真实 API 参数；空参数的 mise 任务不算有效检查。

## 代码修改规范

- 改动尽量集中在任务相关区域，禁止顺手无关重构。
- `grok_register_ttk.py` 体积大、全局状态多，修改需谨慎。
- 除非任务要求配置迁移，保持现有字段与默认值兼容。
- 新增配置项：更新代码默认值、`config.example.json`、`README.md`（及本文件要点）。
- 改注册公共逻辑时同时考虑 GUI 与 CLI（尤其 `register_cli.py` 的 mint 流水线）。
- 网络与浏览器操作应可被测试 mock；单元测试不得依赖真实邮箱 / 浏览器 / 远端服务。
- 新模块可加类型标注；旧核心文件风格不统一时，跟随邻近代码。
- 不要为统一格式重排整文件。
- 浏览器 / 标签页等资源必须在异常与 `finally` 中关闭。
- 多线程共享状态用锁、队列或线程局部变量。
- 优先标准库；新增依赖时同步 `pyproject.toml`、锁文件及依赖清单。
- 保留 UTF-8 与 Windows 中文编码处理；除非任务要求，保留中文 UI/日志文案。
- 不要把 `turnstilePatch/` 当无用目录清理。

## 安全和外部副作用

- 严禁提交或泄露：`config.json`、API Key、JWT、Cookie、密码、SSO Token、代理凭据、生成的账号与管理密钥。
- 敏感运行数据（勿提交、勿在回复中完整粘贴）：

  - `accounts_*.txt`、`accounts_cli.txt`
  - `mail_credentials.txt`
  - `cpa_auths/`
  - `sub2api_exports/`
  - `grok2api_tokens.json`
  - 日志、截图、浏览器用户数据目录

- 不要为验证普通代码修改而执行：真实注册、建邮箱、OAuth mint、远端 token 池写入、CPAMP 云上传、历史回填脚本。
- 这些操作会创建真实账号、消耗额度、开浏览器或改远端数据。
- 真实验证前先检查 `config.json` 是否开启远端写入 / 云上传；若必须验证，范围最小化（通常 1 账号 / 1 请求）。
- 保留使用免责声明。
- 不要增加绕过第三方安全防护、隐藏滥用或扩大批量滥用能力的功能。

## 测试要求

- 对可稳定复现的逻辑补充/更新 `unittest`。
- 优先覆盖：HTTP URL/参数/头构造、鉴权、响应解析、配置默认值与回退、凭据格式与转换。
- HTTP 与浏览器边界必须 mock；单测不得要求安装/启动 Chrome，也不得访问真实邮箱或 grok2api。
- 浏览器流程无法单测覆盖时，交付说明中写明「未进行真实浏览器流程验证」，除非用户明确要求且已提供安全配置。
