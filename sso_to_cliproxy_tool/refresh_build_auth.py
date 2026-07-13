#!/usr/bin/env python3
"""从 sso_output 读账号密码和旧 SSO 凭证，走 OAuth 刷新 cliproxyapi_auth。"""
from __future__ import annotations

import os
import sys
from glob import glob
from pathlib import Path

_FILE_DIR = Path(__file__).resolve().parent
if (_FILE_DIR / "xconsole_client").is_dir():
    _ROOT = _FILE_DIR
elif (_FILE_DIR.parent / "xconsole_client").is_dir():
    _ROOT = _FILE_DIR.parent
else:
    _ROOT = _FILE_DIR

sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    if (Path.cwd() / ".env").is_file():
        load_dotenv(Path.cwd() / ".env")
    else:
        load_dotenv(_ROOT / ".env")
except Exception:
    pass

from xconsole_client.oauth_protocol import login_with_protocol


def load_all_accounts(dirs=("sso_output",)) -> list[dict]:
    import json
    accounts_map = {}
    for d in dirs:
        dir_path = _ROOT / d
        if not dir_path.is_dir():
            continue
        files = glob(str(dir_path / "*.json"))
        for f in files:
            try:
                with open(f, "r", encoding="utf-8") as file:
                    data = json.load(file)
                    email = data.get("email", "").strip()
                    password = data.get("password", "").strip()
                    sso = data.get("sso", "").strip()
                    if email and password:
                        accounts_map[email] = {
                            "email": email,
                            "password": password,
                            "sso": sso,
                            "file": Path(f).name
                        }
            except Exception as e:
                print(f"⚠️ 解析文件错误 {Path(f).name}: {e}")
                
    return list(accounts_map.values())


def main():
    import json
    import argparse
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if sys.platform.startswith('win'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except AttributeError:
            pass

    p = argparse.ArgumentParser(description="Grok 账号批量 OAuth 授权刷新工具（SSO 独立版）")
    p.add_argument("-l", "--limit", type=int, default=None, help="限制刷新的账号数量（从最近注册的开始）")
    p.add_argument("-t", "--threads", type=int, default=1, help="并发刷新线程数")
    p.add_argument("--debug", action="store_true", help="启用协议层调试日志（多线程并发时建议关闭）")
    args = p.parse_args()

    yescaptcha_key = (os.environ.get("YESCAPTCHA_API_KEY") or "").strip()
    if not yescaptcha_key:
        print("❌ 请设置 YESCAPTCHA_API_KEY 环境变量")
        sys.exit(1)

    accounts = load_all_accounts()
    if not accounts:
        print("❌ 未在 sso_output 目录下找到任何有效的账号文件。")
        sys.exit(1)

    # 按文件名降序排序，确保最近注册的账号排在最前面
    accounts.sort(key=lambda x: x.get("file", ""), reverse=True)

    if args.limit is not None and args.limit > 0:
        accounts = accounts[:args.limit]

    total = len(accounts)
    threads = min(args.threads, total)
    
    # 强制在多线程并发模式下关闭调试日志，防止控制台混乱
    is_debug = args.debug and (threads == 1)

    print(f"🔍 找到 {total} 个账号，并发线程数: {threads}，开始刷新授权...")
    print(f"==================================================")

    # 共享统计与锁
    stats_lock = threading.Lock()
    success_count = 0
    fail_count = 0
    skip_count = 0
    failures = []

    def refresh_one(idx: int, acct: dict) -> dict:
        nonlocal success_count, fail_count, skip_count
        email = acct["email"]
        password = acct["password"]
        sso = acct.get("sso", "")

        # 1. 检查凭证是否已存在且非空
        auth_file = _ROOT / "cliproxyapi_auth" / f"{email}.json"
        if auth_file.is_file() and auth_file.stat().st_size > 0:
            print(f"[{idx}/{total}] ⏭️ 账号: {email}  (授权文件已存在，已跳过)")
            with stats_lock:
                skip_count += 1
            return {"email": email, "status": "skipped", "file": auth_file}

        print(f"[{idx}/{total}] 📧 正在刷新账号: {email} (源自: {acct['file']})")
        try:
            session_cookies = {"sso": sso} if sso else None
            result = login_with_protocol(
                email,
                password,
                yescaptcha_key=yescaptcha_key,
                proxy=os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "",
                cliproxyapi_auth_dir=str(_ROOT / "cliproxyapi_auth"),
                debug=is_debug,
                session_cookies=session_cookies,
            )
            
            expired_str = "N/A"
            if result.cliproxyapi_path:
                try:
                    data = json.loads(Path(result.cliproxyapi_path).read_text(encoding="utf-8"))
                    expired_str = data.get('expired', 'N/A')
                except Exception:
                    pass

            print(f"[{idx}/{total}] ✅ 刷新成功: {email} (有效期至: {expired_str})")
            with stats_lock:
                success_count += 1
            return {"email": email, "status": "success", "file": result.cliproxyapi_path}
            
        except Exception as e:
            print(f"[{idx}/{total}] ❌ 刷新失败: {email} -> {e}")
            with stats_lock:
                fail_count += 1
                failures.append((email, str(e)))
            return {"email": email, "status": "failed", "error": str(e)}

    # 分发执行
    if threads <= 1:
        # 串行执行
        for idx, acct in enumerate(accounts, 1):
            refresh_one(idx, acct)
    else:
        # 并发执行
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = [
                executor.submit(refresh_one, idx, acct) 
                for idx, acct in enumerate(accounts, 1)
            ]
            for f in as_completed(futures):
                f.result()

    print(f"\n==================================================")
    print(f"🎉 刷新完成! 成功: {success_count}/{total}, 跳过: {skip_count}/{total}, 失败: {fail_count}/{total}")
    if failures:
        print(f"❌ 失败详情:")
        for f_email, f_err in failures:
            print(f"  - {f_email}: {f_err}")


if __name__ == "__main__":
    main()
