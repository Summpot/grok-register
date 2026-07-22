"""One-off deep analysis of output/reg_stats.jsonl (not part of package API)."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median, pstdev

path = Path("output/reg_stats.jsonl")
rows = []
for line in path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    rows.append(json.loads(line))

print(f"n={len(rows)}")
print("outcomes", Counter(r.get("outcome") for r in rows))


def root_domain(d: str) -> str:
    d = (d or "").lower().strip()
    if not d:
        return "(none)"
    for root in (
        "ohmyaitrash.cloud",
        "ohmyaitrash.org",
        "ohmyaitrash.online",
        "ohmyaitrash.it.com",
        "627500.xyz",
    ):
        if d == root or d.endswith("." + root):
            return root
    parts = d.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else d


by_root: dict[str, Counter] = defaultdict(Counter)
for r in rows:
    by_root[root_domain(r.get("email_domain") or "")][r.get("outcome")] += 1

print("\n== by root domain ==")
for dom, c in sorted(by_root.items(), key=lambda x: -sum(x[1].values())):
    n = sum(c.values())
    bot = c.get("bot_flag", 0)
    ok = c.get("success", 0)
    print(
        f"  {dom}: n={n} success={ok} bot={bot} "
        f"bot_rate={bot/n:.3f} success_rate={ok/n:.3f}"
    )

print("\n== jwt_claims keys frequency ==")
key_c: Counter = Counter()
bot_flag_vals: Counter = Counter()
for r in rows:
    jc = r.get("jwt_claims") or {}
    for k in jc:
        key_c[k] += 1
    if "bot_flag_source" in jc:
        bot_flag_vals[str(jc.get("bot_flag_source"))] += 1
print("  keys", key_c.most_common(20))
print("  bot_flag_source values", bot_flag_vals)

print("\n== given_name success rate (n>=3) ==")
name_c: dict[str, Counter] = defaultdict(Counter)
for r in rows:
    gn = (r.get("profile") or {}).get("given_name") or "?"
    name_c[gn][r.get("outcome")] += 1
for name, c in sorted(name_c.items(), key=lambda x: -sum(x[1].values())):
    n = sum(c.values())
    if n < 3:
        continue
    bot = c.get("bot_flag", 0)
    print(f"  {name}: n={n} bot_rate={bot/n:.2f} success={c.get('success',0)}")


def num(x, default=None):
    try:
        return float(x)
    except Exception:
        return default


def collect_mouse(outcome):
    xs: dict[str, list] = defaultdict(list)
    for r in rows:
        if r.get("outcome") != outcome:
            continue
        for m in r.get("mouse") or []:
            for k in (
                "target_x",
                "target_y",
                "start_x",
                "start_y",
                "mid_x",
                "mid_y",
                "over_x",
                "over_y",
                "final_x",
                "final_y",
                "steps_start",
                "steps_mid",
                "steps_over",
                "steps_final",
                "steps_jitter",
                "click_delay_ms",
                "checkbox_base_x",
                "checkbox_base_y",
            ):
                v = num(m.get(k))
                if v is not None:
                    xs[k].append(v)
            box = m.get("box") or {}
            for k in ("x", "y", "width", "height"):
                v = num(box.get(k))
                if v is not None:
                    xs[f"box_{k}"].append(v)
    return xs


print("\n== mouse path means (success vs bot_flag) ==")
ms_ok = collect_mouse("success")
ms_bot = collect_mouse("bot_flag")
for k in sorted(set(ms_ok) | set(ms_bot)):
    a, b = ms_ok.get(k, []), ms_bot.get(k, [])
    if not a or not b:
        continue
    print(
        f"  {k}: success mean={mean(a):.2f} med={median(a):.2f} | "
        f"bot mean={mean(b):.2f} med={median(b):.2f} | d={mean(b)-mean(a):+.2f}"
    )

print("\n== turnstile events ==")
for outcome in ("success", "bot_flag"):
    methods: Counter = Counter()
    clicks = []
    durs = []
    token_lens = []
    auto = 0
    for r in rows:
        if r.get("outcome") != outcome:
            continue
        ts = r.get("turnstile_summary") or {}
        methods[ts.get("method") or "?"] += 1
        if ts.get("clicks_done") is not None:
            clicks.append(int(ts["clicks_done"]))
        if ts.get("duration_ms") is not None:
            durs.append(int(ts["duration_ms"]))
        if ts.get("token_len") is not None:
            token_lens.append(int(ts["token_len"]))
        for e in r.get("turnstile") or []:
            if e.get("event") == "auto_solved":
                auto += 1
    print(
        f"  {outcome}: methods={dict(methods)} "
        f"clicks={mean(clicks) if clicks else None:.2f} "
        f"ts_dur_ms={mean(durs) if durs else None:.0f} "
        f"token_len={mean(token_lens) if token_lens else None:.0f} "
        f"auto_events={auto}"
    )

print("\n== duration_ms buckets ==")
buckets = [(0, 45), (45, 60), (60, 75), (75, 90), (90, 120), (120, 999)]
for lo, hi in buckets:
    c: Counter = Counter()
    for r in rows:
        d = (r.get("duration_ms") or 0) / 1000.0
        if lo <= d < hi:
            c[r.get("outcome")] += 1
    n = sum(c.values())
    if n:
        print(
            f"  {lo}-{hi}s: n={n} bot_rate={c.get('bot_flag',0)/n:.2f} "
            f"success={c.get('success',0)}"
        )

print("\n== by worker ==")
by_w: dict[str, Counter] = defaultdict(Counter)
for r in rows:
    by_w[str(r.get("worker_id"))][r.get("outcome")] += 1
for w, c in sorted(by_w.items(), key=lambda x: -sum(x[1].values())):
    n = sum(c.values())
    print(
        f"  W{w}: n={n} bot_rate={c.get('bot_flag',0)/n:.3f} "
        f"success={c.get('success',0)}"
    )

print("\n== chronological chunks of 20 ==")
for i in range(0, len(rows), 20):
    chunk = rows[i : i + 20]
    c = Counter(r.get("outcome") for r in chunk)
    n = len(chunk)
    print(
        f"  #{i+1}-{i+n}: success={c.get('success',0)} bot={c.get('bot_flag',0)} "
        f"bot_rate={c.get('bot_flag',0)/n:.2f}"
    )

print("\n== user_agent ==")
ua_c = Counter((r.get("user_agent") or "")[:100] for r in rows)
for ua, n in ua_c.most_common(5):
    print(f"  n={n} {ua!r}")

ok = next(r for r in rows if r.get("outcome") == "success")
bot = next(r for r in rows if r.get("outcome") == "bot_flag")
print("\n== sample jwt ==")
print("  success", ok.get("jwt_claims"))
print("  bot    ", bot.get("jwt_claims"))

print("\n== click_delay_ms distribution ==")
for outcome in ("success", "bot_flag"):
    vals = []
    for r in rows:
        if r.get("outcome") != outcome:
            continue
        for m in r.get("mouse") or []:
            v = num(m.get("click_delay_ms"))
            if v is not None:
                vals.append(v)
    if vals:
        print(
            f"  {outcome}: n={len(vals)} mean={mean(vals):.1f} med={median(vals):.1f} "
            f"min={min(vals):.0f} max={max(vals):.0f} std={pstdev(vals):.1f}"
        )

print("\n== click offset from checkbox base ==")
for outcome in ("success", "bot_flag"):
    dx, dy = [], []
    for r in rows:
        if r.get("outcome") != outcome:
            continue
        for m in r.get("mouse") or []:
            if m.get("target_x") is not None and m.get("checkbox_base_x") is not None:
                dx.append(float(m["target_x"]) - float(m["checkbox_base_x"]))
                dy.append(float(m["target_y"]) - float(m["checkbox_base_y"]))
    if dx:
        print(
            f"  {outcome}: dx mean={mean(dx):.2f} std={pstdev(dx):.2f} | "
            f"dy mean={mean(dy):.2f} std={pstdev(dy):.2f}"
        )

print("\n== mouse click count per attempt ==")
for outcome in ("success", "bot_flag"):
    counts = [len(r.get("mouse") or []) for r in rows if r.get("outcome") == outcome]
    print(
        f"  {outcome}: mean={mean(counts):.2f} max={max(counts)} "
        f"zero={sum(1 for c in counts if c == 0)}"
    )

print("\n== turnstile event sequences (top) ==")
seq_c: dict[str, Counter] = defaultdict(Counter)
for r in rows:
    seq = " > ".join(e.get("event", "?") for e in (r.get("turnstile") or []))
    seq_c[seq][r.get("outcome")] += 1
for seq, c in sorted(seq_c.items(), key=lambda x: -sum(x[1].values()))[:12]:
    n = sum(c.values())
    print(f"  [{n}] bot_rate={c.get('bot_flag',0)/n:.2f} {seq[:140]}")

print("\n== duration stats ==")
for outcome in ("success", "bot_flag"):
    vals = [
        r.get("duration_ms")
        for r in rows
        if r.get("outcome") == outcome and r.get("duration_ms")
    ]
    print(
        f"  {outcome}: mean={mean(vals)/1000:.1f}s med={median(vals)/1000:.1f}s "
        f"min={min(vals)/1000:.1f}s max={max(vals)/1000:.1f}s"
    )

print("\n== proxy field ==")
print(Counter((r.get("proxy") or "(empty)") for r in rows))

print("\n== first/last ==")
print(" first", rows[0].get("ts"), rows[0].get("outcome"), rows[0].get("email"))
print(" last ", rows[-1].get("ts"), rows[-1].get("outcome"), rows[-1].get("email"))

# rolling success rate last 30
print("\n== rolling success rate (window=15) ==")
win = 15
for i in range(win - 1, len(rows), 10):
    chunk = rows[i - win + 1 : i + 1]
    ok_n = sum(1 for r in chunk if r.get("outcome") == "success")
    print(f"  end#{i+1}: success_rate={ok_n/win:.2f} ({ok_n}/{win})")
