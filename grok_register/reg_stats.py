"""Per-registration attempt telemetry for bot_flag / failure analysis.

Each registration attempt can collect:
  - outcome (success / bot_flag / error) and reason
  - email domain, proxy host, worker id, durations
  - Turnstile solve path (auto vs clicks, widget size, token_len)
  - mouse-path random samples (start/mid/overshoot/checkbox gauss, steps, delays)
  - safe JWT claim subset from Build access_token (never full tokens)

Records are appended as JSON Lines to ``output/reg_stats.jsonl`` (configurable).

Usage:
  begin_attempt(worker_id=1, ...)
  set_meta(email=..., proxy=...)
  record_turnstile_solve(...)
  record_mouse_click(...)
  finish_attempt(outcome="bot_flag", reason="bot_flag_source=1")

Analyze:
  python -m grok_register.reg_stats
  python -m grok_register.reg_stats --file output/reg_stats.jsonl
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from grok_register.paths import OUTPUT_DIR, ensure_output_dir

_tls = threading.local()
_write_lock = threading.Lock()
_enabled_override: bool | None = None  # None = read from config/env
_path_override: str | None = None

# Sensitive JWT keys we never persist even in "safe claims".
_JWT_DENY = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "sso",
        "password",
        "email",  # full email stored separately when needed
    }
)
# Interesting claim keys for bot-flag correlation (others still kept if short).
_JWT_PRIORITY = (
    "bot_flag_source",
    "bot_flag",
    "risk",
    "risk_score",
    "risk_level",
    "score",
    "sub",
    "iss",
    "aud",
    "azp",
    "scope",
    "team_id",
    "user_id",
    "sid",
    "jti",
    "iat",
    "exp",
    "nbf",
    "auth_time",
    "amr",
    "acr",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def set_enabled(enabled: bool | None) -> None:
    """Force enable/disable (None restores config/env default)."""
    global _enabled_override
    _enabled_override = enabled


def set_stats_path(path: str | None) -> None:
    global _path_override
    _path_override = (path or "").strip() or None


def is_enabled() -> bool:
    if _enabled_override is not None:
        return bool(_enabled_override)
    env = (os.environ.get("GROK_REG_STATS") or "").strip().lower()
    if env in ("0", "false", "off", "no"):
        return False
    if env in ("1", "true", "on", "yes"):
        return True
    try:
        from grok_register import app as reg_app

        return bool(reg_app.config.get("reg_stats_enabled", True))
    except Exception:
        return True


def stats_path() -> Path:
    if _path_override:
        return Path(_path_override)
    try:
        from grok_register import app as reg_app
        from grok_register.paths import PROJECT_ROOT

        configured = str(reg_app.config.get("reg_stats_file", "") or "").strip()
        if configured:
            p = Path(configured)
            if not p.is_absolute():
                p = (PROJECT_ROOT / configured).resolve()
            return p
    except Exception:
        pass
    return OUTPUT_DIR / "reg_stats.jsonl"


def proxy_host_label(proxy: str | None) -> str:
    """host:port only — never user:pass."""
    p = (proxy or "").strip()
    if not p:
        return ""
    try:
        if "://" not in p:
            p = "http://" + p
        u = urlparse(p)
        host = u.hostname or ""
        port = u.port
        if host and port:
            return f"{host}:{port}"
        return host or p.split("@")[-1][:80]
    except Exception:
        return p.split("@")[-1][:80]


def email_domain(email: str | None) -> str:
    e = str(email or "").strip().lower()
    if "@" not in e:
        return ""
    return e.rsplit("@", 1)[-1]


def _decode_jwt_payload(token: str | None) -> dict[str, Any]:
    """Lightweight JWT payload decode (no crypto verify; no heavy deps)."""
    import base64

    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    pad = "=" * (-len(payload) % 4)
    try:
        data = base64.urlsafe_b64decode(payload + pad)
        claims = json.loads(data.decode("utf-8", errors="replace"))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def safe_jwt_claims(access_token: str | None) -> dict[str, Any]:
    """Decode JWT payload and keep non-sensitive short fields for correlation."""
    claims = _decode_jwt_payload(access_token)
    if not isinstance(claims, dict) or not claims:
        return {}
    out: dict[str, Any] = {}
    for key in _JWT_PRIORITY:
        if key in claims and key not in _JWT_DENY:
            out[key] = _sanitize_claim_value(claims[key])
    # Also keep unknown short scalar keys (may include new bot signals)
    for key, value in claims.items():
        if key in out or key in _JWT_DENY:
            continue
        if not isinstance(key, str) or len(key) > 40:
            continue
        if isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str) and len(value) <= 64:
            out[key] = value
        elif isinstance(value, list) and len(value) <= 8:
            # e.g. amr: ["pwd"]
            if all(isinstance(x, (str, int, float, bool)) and len(str(x)) <= 32 for x in value):
                out[key] = value
    return out


def _sanitize_claim_value(value: Any) -> Any:
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:120]
    if isinstance(value, list):
        return [_sanitize_claim_value(v) for v in value[:12]]
    if isinstance(value, dict):
        return {str(k)[:40]: _sanitize_claim_value(v) for k, v in list(value.items())[:12]}
    return str(value)[:80]


def current_attempt() -> dict[str, Any] | None:
    return getattr(_tls, "attempt", None)


def begin_attempt(**meta: Any) -> dict[str, Any] | None:
    """Start a new thread-local registration attempt. Returns the attempt dict or None if disabled."""
    if not is_enabled():
        _tls.attempt = None
        return None
    attempt: dict[str, Any] = {
        "attempt_id": uuid.uuid4().hex[:16],
        "ts": _now_iso(),
        "t0": time.time(),
        "worker_id": meta.get("worker_id"),
        "idx": meta.get("idx"),
        "email": "",
        "email_domain": "",
        "proxy": "",
        "user_agent": "",
        "viewport": {},
        "profile": {},
        "stages": {},
        "turnstile": [],
        "mouse": [],
        "timings_ms": {},
        "jwt_claims": {},
        "outcome": "",
        "reason": "",
        "bot_flagged": False,
        "error": "",
        "meta": {},
    }
    for k, v in meta.items():
        if k in ("worker_id", "idx"):
            continue
        if k == "proxy":
            attempt["proxy"] = proxy_host_label(str(v or ""))
        elif k == "email":
            attempt["email"] = str(v or "")
            attempt["email_domain"] = email_domain(str(v or ""))
        elif k == "user_agent":
            attempt["user_agent"] = str(v or "")[:200]
        else:
            attempt["meta"][k] = v
    _tls.attempt = attempt
    return attempt


def update_attempt(**fields: Any) -> None:
    attempt = current_attempt()
    if not attempt:
        return
    for k, v in fields.items():
        if k == "proxy":
            attempt["proxy"] = proxy_host_label(str(v or ""))
        elif k == "email":
            attempt["email"] = str(v or "")
            attempt["email_domain"] = email_domain(str(v or ""))
        elif k == "user_agent":
            attempt["user_agent"] = str(v or "")[:200]
        elif k == "profile" and isinstance(v, dict):
            # never store password
            attempt["profile"] = {
                "given_name": str(v.get("given_name") or ""),
                "family_name": str(v.get("family_name") or ""),
                "password_len": len(str(v.get("password") or "")),
            }
        elif k == "viewport" and isinstance(v, dict):
            attempt["viewport"] = {
                "width": v.get("width"),
                "height": v.get("height"),
            }
        elif k == "jwt_claims" and isinstance(v, dict):
            attempt["jwt_claims"] = v
        elif k == "access_token":
            attempt["jwt_claims"] = safe_jwt_claims(str(v or ""))
        elif k in attempt and k not in ("turnstile", "mouse", "stages", "timings_ms", "t0"):
            attempt[k] = v
        else:
            attempt["meta"][k] = v


def mark_stage(name: str, *, status: str = "ok", **extra: Any) -> None:
    attempt = current_attempt()
    if not attempt:
        return
    entry = {
        "status": status,
        "t_ms": int((time.time() - float(attempt.get("t0") or time.time())) * 1000),
    }
    entry.update(extra)
    attempt.setdefault("stages", {})[str(name)] = entry


def record_mouse_click(sample: dict[str, Any]) -> None:
    """Record one humanized mouse path used for Turnstile (or other) clicks."""
    attempt = current_attempt()
    if not attempt:
        return
    if not isinstance(sample, dict):
        return
    # Cap size so a stuck loop cannot blow disk
    mice = attempt.setdefault("mouse", [])
    if len(mice) >= 40:
        return
    clean = {
        "t_ms": int((time.time() - float(attempt.get("t0") or time.time())) * 1000),
        "purpose": str(sample.get("purpose") or "turnstile"),
        "target_x": _r2(sample.get("target_x")),
        "target_y": _r2(sample.get("target_y")),
        "start_x": _r2(sample.get("start_x")),
        "start_y": _r2(sample.get("start_y")),
        "mid_x": _r2(sample.get("mid_x")),
        "mid_y": _r2(sample.get("mid_y")),
        "over_x": _r2(sample.get("over_x")),
        "over_y": _r2(sample.get("over_y")),
        "final_x": _r2(sample.get("final_x")),
        "final_y": _r2(sample.get("final_y")),
        "steps_start": sample.get("steps_start"),
        "steps_mid": sample.get("steps_mid"),
        "steps_over": sample.get("steps_over"),
        "steps_final": sample.get("steps_final"),
        "steps_jitter": sample.get("steps_jitter"),
        "click_delay_ms": sample.get("click_delay_ms"),
        "box": sample.get("box"),
        "checkbox_base_x": _r2(sample.get("checkbox_base_x")),
        "checkbox_base_y": _r2(sample.get("checkbox_base_y")),
        "gauss_sigma_x": sample.get("gauss_sigma_x"),
        "gauss_sigma_y": sample.get("gauss_sigma_y"),
        "viewport": sample.get("viewport"),
    }
    # Drop Nones for compact lines
    mice.append({k: v for k, v in clean.items() if v is not None})


def record_turnstile_event(event: dict[str, Any]) -> None:
    attempt = current_attempt()
    if not attempt:
        return
    events = attempt.setdefault("turnstile", [])
    if len(events) >= 30:
        return
    if not isinstance(event, dict):
        return
    payload = {
        "t_ms": int((time.time() - float(attempt.get("t0") or time.time())) * 1000),
        **{k: v for k, v in event.items() if v is not None},
    }
    events.append(payload)


def finish_attempt(
    outcome: str,
    *,
    reason: str = "",
    bot_flagged: bool | None = None,
    error: str = "",
    access_token: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Finalize and persist the current attempt. Clears thread-local state."""
    attempt = current_attempt()
    if not attempt:
        return None
    try:
        if access_token:
            attempt["jwt_claims"] = safe_jwt_claims(access_token)
        attempt["outcome"] = str(outcome or "unknown")
        attempt["reason"] = str(reason or "")[:240]
        if bot_flagged is not None:
            attempt["bot_flagged"] = bool(bot_flagged)
        elif outcome == "bot_flag":
            attempt["bot_flagged"] = True
        if error:
            attempt["error"] = str(error)[:400]
        if extra and isinstance(extra, dict):
            attempt.setdefault("meta", {}).update(extra)
        t0 = float(attempt.get("t0") or time.time())
        attempt["duration_ms"] = int((time.time() - t0) * 1000)
        # Summarize turnstile for quick filtering
        attempt["turnstile_summary"] = _summarize_turnstile(attempt.get("turnstile") or [])
        attempt["mouse_summary"] = _summarize_mouse(attempt.get("mouse") or [])
        record = _public_record(attempt)
        _append_jsonl(record)
        return record
    finally:
        _tls.attempt = None


def abandon_attempt() -> None:
    """Drop in-progress attempt without writing (e.g. browser start failed)."""
    _tls.attempt = None


def _r2(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return round(float(v), 2)
    except Exception:
        return None


def _summarize_turnstile(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {"events": 0}
    solves = [e for e in events if e.get("event") in ("solved", "auto_solved", "failed")]
    last = solves[-1] if solves else events[-1]
    clicks = sum(int(e.get("clicks_done") or 0) for e in events if "clicks_done" in e)
    # prefer explicit clicks on solved event
    for e in reversed(events):
        if e.get("event") in ("solved", "auto_solved", "failed") and "clicks_done" in e:
            clicks = int(e.get("clicks_done") or 0)
            break
    return {
        "events": len(events),
        "method": last.get("method") or last.get("event") or "",
        "clicks_done": clicks,
        "token_len": last.get("token_len"),
        "widget_w": last.get("widget_w") or last.get("width"),
        "widget_h": last.get("widget_h") or last.get("height"),
        "duration_ms": last.get("duration_ms"),
        "force_used": bool(last.get("force_used")),
    }


def _summarize_mouse(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        return {"clicks": 0}
    last = samples[-1]
    return {
        "clicks": len(samples),
        "last_target_x": last.get("target_x"),
        "last_target_y": last.get("target_y"),
        "last_start_x": last.get("start_x"),
        "last_start_y": last.get("start_y"),
        "last_click_delay_ms": last.get("click_delay_ms"),
        "last_steps_mid": last.get("steps_mid"),
    }


def _public_record(attempt: dict[str, Any]) -> dict[str, Any]:
    """Strip internal fields before disk write."""
    out = dict(attempt)
    out.pop("t0", None)
    # Keep email for operator debugging but allow redaction via env
    if (os.environ.get("GROK_REG_STATS_REDACT_EMAIL") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        email = str(out.get("email") or "")
        if "@" in email:
            local, _, dom = email.partition("@")
            out["email"] = (local[:2] + "***@" + dom) if local else ("***@" + dom)
    return out


def _append_jsonl(record: dict[str, Any]) -> None:
    path = stats_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        ensure_output_dir()
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with _write_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def load_records(path: str | Path | None = None) -> list[dict[str, Any]]:
    p = Path(path) if path else stats_path()
    if not p.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def analyze_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate outcomes and correlate features with bot_flag vs success."""
    by_outcome: Counter[str] = Counter()
    by_domain: dict[str, Counter[str]] = defaultdict(Counter)
    by_proxy: dict[str, Counter[str]] = defaultdict(Counter)
    by_ts_method: dict[str, Counter[str]] = defaultdict(Counter)
    click_hist: dict[str, list[int]] = defaultdict(list)
    duration_hist: dict[str, list[int]] = defaultdict(list)
    mouse_delay: dict[str, list[float]] = defaultdict(list)
    mouse_steps: dict[str, list[float]] = defaultdict(list)
    force_rate: dict[str, list[int]] = defaultdict(list)
    reasons: Counter[str] = Counter()

    for r in records:
        outcome = str(r.get("outcome") or "unknown")
        by_outcome[outcome] += 1
        if r.get("reason"):
            reasons[str(r.get("reason"))[:120]] += 1
        dom = str(r.get("email_domain") or "") or "(none)"
        by_domain[dom][outcome] += 1
        px = str(r.get("proxy") or "") or "(direct)"
        by_proxy[px][outcome] += 1
        ts = r.get("turnstile_summary") or {}
        method = str(ts.get("method") or "(none)")
        by_ts_method[method][outcome] += 1
        if ts.get("clicks_done") is not None:
            try:
                click_hist[outcome].append(int(ts["clicks_done"]))
            except Exception:
                pass
        if r.get("duration_ms") is not None:
            try:
                duration_hist[outcome].append(int(r["duration_ms"]))
            except Exception:
                pass
        ms = r.get("mouse_summary") or {}
        if ms.get("last_click_delay_ms") is not None:
            try:
                mouse_delay[outcome].append(float(ms["last_click_delay_ms"]))
            except Exception:
                pass
        if ms.get("last_steps_mid") is not None:
            try:
                mouse_steps[outcome].append(float(ms["last_steps_mid"]))
            except Exception:
                pass
        force_rate[outcome].append(1 if ts.get("force_used") else 0)

    def _avg(xs: list[float] | list[int]) -> float | None:
        if not xs:
            return None
        return round(sum(xs) / len(xs), 2)

    def _rate_map(counter_map: dict[str, Counter[str]], key: str = "bot_flag") -> list[dict[str, Any]]:
        rows = []
        for name, c in counter_map.items():
            total = sum(c.values())
            if total <= 0:
                continue
            bot = c.get(key, 0)
            ok = c.get("success", 0)
            rows.append(
                {
                    "key": name,
                    "n": total,
                    "success": ok,
                    "bot_flag": bot,
                    "bot_rate": round(bot / total, 3),
                    "success_rate": round(ok / total, 3),
                    "other": total - ok - bot,
                }
            )
        rows.sort(key=lambda x: (-x["n"], -x["bot_rate"]))
        return rows

    feature_compare = {}
    for outcome in sorted(by_outcome.keys()):
        feature_compare[outcome] = {
            "n": by_outcome[outcome],
            "avg_clicks": _avg(click_hist.get(outcome, [])),
            "avg_duration_ms": _avg(duration_hist.get(outcome, [])),
            "avg_click_delay_ms": _avg(mouse_delay.get(outcome, [])),
            "avg_steps_mid": _avg(mouse_steps.get(outcome, [])),
            "force_used_rate": _avg(force_rate.get(outcome, [])),
        }

    total = sum(by_outcome.values())
    return {
        "total": total,
        "by_outcome": dict(by_outcome),
        "success_rate": round(by_outcome.get("success", 0) / total, 3) if total else 0.0,
        "bot_flag_rate": round(by_outcome.get("bot_flag", 0) / total, 3) if total else 0.0,
        "top_reasons": reasons.most_common(15),
        "by_email_domain": _rate_map(by_domain)[:20],
        "by_proxy": _rate_map(by_proxy)[:20],
        "by_turnstile_method": _rate_map(by_ts_method)[:15],
        "feature_compare": feature_compare,
        "hints": _analysis_hints(by_outcome, feature_compare, by_ts_method, by_domain),
    }


def _analysis_hints(
    by_outcome: Counter[str],
    feature_compare: dict[str, Any],
    by_ts_method: dict[str, Counter[str]],
    by_domain: dict[str, Counter[str]],
) -> list[str]:
    hints: list[str] = []
    bot_n = by_outcome.get("bot_flag", 0)
    ok_n = by_outcome.get("success", 0)
    if bot_n == 0:
        hints.append("暂无 bot_flag 样本；继续采集后再对比 success vs bot_flag 特征。")
        return hints
    bot_f = feature_compare.get("bot_flag") or {}
    ok_f = feature_compare.get("success") or {}
    if ok_f.get("avg_clicks") is not None and bot_f.get("avg_clicks") is not None:
        if bot_f["avg_clicks"] > ok_f["avg_clicks"] + 0.4:
            hints.append(
                f"bot_flag 平均 Turnstile 点击次数更高"
                f"（{bot_f['avg_clicks']} vs success {ok_f['avg_clicks']}），"
                "可尝试更耐心等待 auto-solve、减少 re-click。"
            )
        elif bot_f["avg_clicks"] + 0.4 < ok_f["avg_clicks"]:
            hints.append(
                f"bot_flag 平均点击更少（{bot_f['avg_clicks']} vs {ok_f['avg_clicks']}），"
                "点击次数可能不是主因，关注代理/域名/时延。"
            )
    if ok_f.get("force_used_rate") is not None and bot_f.get("force_used_rate") is not None:
        if bot_f["force_used_rate"] > ok_f["force_used_rate"] + 0.15:
            hints.append(
                "bot_flag 更常使用 force 点击；可关闭/延后 force fallback。"
            )
    # method rates
    method_rates = []
    for method, c in by_ts_method.items():
        total = sum(c.values())
        if total < 3:
            continue
        method_rates.append((method, c.get("bot_flag", 0) / total, total))
    method_rates.sort(key=lambda x: -x[1])
    if method_rates and method_rates[0][1] >= 0.4:
        m, rate, n = method_rates[0]
        hints.append(f"Turnstile method={m!r} bot_flag 率偏高 ({rate:.0%}, n={n})。")
    # domain
    domain_rates = []
    for dom, c in by_domain.items():
        total = sum(c.values())
        if total < 3:
            continue
        domain_rates.append((dom, c.get("bot_flag", 0) / total, total))
    domain_rates.sort(key=lambda x: -x[1])
    if domain_rates and domain_rates[0][1] >= 0.5:
        d, rate, n = domain_rates[0]
        hints.append(f"邮箱域名 {d} bot_flag 率偏高 ({rate:.0%}, n={n})，可考虑换域。")
    if not hints:
        hints.append(
            f"已有 bot_flag={bot_n} / success={ok_n}；"
            "查看 by_proxy / by_email_domain / feature_compare 找最强相关维度。"
        )
    return hints


def format_analysis(report: dict[str, Any]) -> str:
    lines = [
        "=== Registration attempt analysis ===",
        f"total={report.get('total', 0)}  "
        f"success_rate={report.get('success_rate')}  "
        f"bot_flag_rate={report.get('bot_flag_rate')}",
        f"by_outcome: {report.get('by_outcome')}",
        "",
        "-- feature_compare (avg metrics by outcome) --",
    ]
    for outcome, feats in (report.get("feature_compare") or {}).items():
        lines.append(f"  {outcome}: {feats}")
    lines.append("")
    lines.append("-- by_turnstile_method (top) --")
    for row in (report.get("by_turnstile_method") or [])[:8]:
        lines.append(
            f"  {row['key']}: n={row['n']} bot_rate={row['bot_rate']} "
            f"success_rate={row['success_rate']}"
        )
    lines.append("")
    lines.append("-- by_email_domain (top) --")
    for row in (report.get("by_email_domain") or [])[:8]:
        lines.append(
            f"  {row['key']}: n={row['n']} bot_rate={row['bot_rate']} "
            f"success_rate={row['success_rate']}"
        )
    lines.append("")
    lines.append("-- by_proxy (top) --")
    for row in (report.get("by_proxy") or [])[:8]:
        lines.append(
            f"  {row['key']}: n={row['n']} bot_rate={row['bot_rate']} "
            f"success_rate={row['success_rate']}"
        )
    if report.get("top_reasons"):
        lines.append("")
        lines.append("-- top reasons --")
        for reason, n in report["top_reasons"][:10]:
            lines.append(f"  [{n}] {reason}")
    lines.append("")
    lines.append("-- hints --")
    for h in report.get("hints") or []:
        lines.append(f"  * {h}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Analyze grok-register attempt stats (JSONL)."
    )
    parser.add_argument(
        "--file",
        "-f",
        default="",
        help="Path to reg_stats.jsonl (default: output/reg_stats.jsonl)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print full analysis as JSON",
    )
    args = parser.parse_args(argv)
    path = args.file or str(stats_path())
    records = load_records(path)
    if not records:
        print(f"No records found in {path}")
        return 1
    report = analyze_records(records)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_analysis(report))
        print(f"\n(source: {path}, n={len(records)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
