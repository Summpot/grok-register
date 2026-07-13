# SSO ➔ Grok Build (CPA) 独立授权刷新工具

> **修改自**：https://github.com/dongguatanglinux/grok-build-auth

这是一个精简、专注的独立工具包，不包含任何注册和邮箱后端逻辑。仅用于读取已有的 `sso_output/` 账号信息并批量刷新生成 `cliproxyapi_auth/` 的授权配置文件。

---

## 🔑 验证码与环境变量设置 (重要)

本工具在 SSO Token 失效时会自动回退到“密码登录”，密码登录需要绕过 Cloudflare Turnstile 人机验证，因此**必须使用 YesCaptcha 自动打码服务**。

请在项目根目录下创建 `.env` 文件，并填写您的 API 密钥：

```bash
# 在同级目录下创建名为 .env 的文件，内容如下：
YESCAPTCHA_API_KEY="您的_yescaptcha_client_key_密钥"
```

---

## 📂 云端部署目录结构

部署时，仅需将本工具包文件夹内的内容放置于云端目录下，并将您的 `.env` 配置文件与 `sso_output/` 文件夹放到同级目录下即可：

```
~/grok-refresh-tool/                 # 您的云端工作目录
├── refresh_build_auth.py            # 刷新控制脚本
├── requirements.txt                 # 精简依赖文件
├── README.md                        # 本说明文件
├── .env                             # 配置文件（需包含 YESCAPTCHA_API_KEY）
├── sso_output/                      # 📂 存放您所有账号 .json 的文件夹
│   ├── sso_xxx_1.json
│   └── sso_xxx_2.json
├── cliproxyapi_auth/                # 📂 自动生成的凭证目录（运行后自动创建）
└── xconsole_client/                 # 📂 核心网络协议包
    ├── __init__.py
    ├── oauth_protocol.py
    ├── xai_oauth.py
    ├── solver.py
    ├── sso.py
    ├── grpcweb.py
    └── config.py
```

---

## 🚀 命令行操作指南

### 1. 初始化环境（仅首次）
```bash
# 创建虚拟环境
python3 -m venv .venv

# 激活虚拟环境
source .venv/bin/activate  # Windows 下运行: .venv\Scripts\activate

# 安装精简版依赖
pip install -r requirements.txt
```

### 2. 执行批量刷新
```bash
# 全量并发刷新：自动跳过 cliproxyapi_auth 中已生成的账号，使用 3 个并发线程加速刷新
python refresh_build_auth.py -t 3

# 限额并发刷新：例如仅处理最近生成的 10 个账号，开 3 个并发线程
python refresh_build_auth.py -l 10 -t 3

# 单账号串行调试：当遇到报错时，可通过 --debug 参数打印极尽详细的 gRPC/HTTP 重定向调试跳转日志
python refresh_build_auth.py -l 1 --debug
```

---

## 💡 代理网络配置提示

1. **若在海外云服务器（可直连 x.ai）执行**：
   - 无需配置任何代理，直接运行刷新脚本即可。
2. **若在国内云服务器/本地环境执行**：
   - 必须在执行命令前配置代理，否则 `curl_cffi` 直连 `accounts.x.ai` 会报错 `TLS connect error`：
   ```bash
   HTTPS_PROXY=http://127.0.0.1:7890 python refresh_build_auth.py -t 3
   ```
