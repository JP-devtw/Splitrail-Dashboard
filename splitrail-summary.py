#!/usr/bin/env python3
"""一鍵彙總 Claude Code / Codex CLI / Antigravity CLI 三家用量。

跑 `splitrail.exe stats`，把 JSON 結果聚合成一張總覽表印出來。
費用是 splitrail 用本機 token 記錄反推的估算值，不是官方帳單金額，
只拿來看相對趨勢，對帳請以官方帳單為準。
"""
import json
import os
import re
import ssl
import subprocess
import tempfile
import sys
import datetime
import time
import urllib.error
import urllib.request
import webbrowser

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def find_splitrail_exe():
    """優先找腳本同層目錄的 splitrail.exe，找不到才退回 PATH。"""
    local = os.path.join(SCRIPT_DIR, "splitrail.exe")
    if os.path.isfile(local):
        return local
    return "splitrail.exe"


SPLITRAIL_EXE = find_splitrail_exe()

# 跟 openusage 風格對齊的配色，順序 = 圖例排序
COLORS = {
    "Claude Code": "#E8734A",
    "Codex CLI": "#22B573",
    "Antigravity CLI": "#6C63FF",
}
DEFAULT_COLOR = "#8A8F98"


def run_splitrail():
    result = subprocess.run(
        [SPLITRAIL_EXE, "stats"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        print("splitrail.exe 執行失敗：", result.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print("無法解析 splitrail 輸出的 JSON：", result.stdout[:500], file=sys.stderr)
        sys.exit(1)


def fetch_claude_quota():
    """查 Claude 訂閱額度（claude /usage 背後的 API）：5 小時窗與每週窗的使用率和重置時間。

    token 取自 Claude Code CLI 的 ~/.claude/.credentials.json，只在 CLI 執行時才會刷新
    （約 8 小時過期，桌面 App 不會寫這個檔）。任何失敗都回傳 error，不中斷整頁產生。
    """
    cred_path = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
    try:
        with open(cred_path, encoding="utf-8") as f:
            oauth = json.load(f)["claudeAiOauth"]
        token = oauth["accessToken"]
    except (OSError, ValueError, KeyError):
        return {"error": "找不到 Claude 憑證（~/.claude/.credentials.json）"}
    if oauth.get("expiresAt") and time.time() * 1000 > oauth["expiresAt"]:
        return {"error": "Claude token 已過期，在終端機執行一次 claude 指令即可刷新"}
    headers = {
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        # 額度重置券（cedar_ember）只回給 Claude Code 這個來源，其他 User-Agent 會得到
        # eligible:false, ineligible_reason:"surface"（2026-09-27 實測；OpenUsage 同樣寫死版本號）
        "User-Agent": "claude-cli/2.1.283 (external, cli)",
    }
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage?cedar_ember=1", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        return {"error": f"額度 API 回應 HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"error": f"額度 API 連線失敗：{e}"}
    windows = []
    for key, label, secs in (("five_hour", "5 小時", 18000), ("seven_day", "每週", 604800)):
        w = data.get(key) or {}
        if w.get("utilization") is None:
            continue
        windows.append({"label": label, "pct": w["utilization"], "resets_at": w.get("resets_at"), "secs": secs})
    if not windows:
        return {"error": "額度 API 回應中沒有 5 小時／每週額度資料"}
    return {"windows": windows, "grants": parse_claude_reset_grants(data.get("cedar_ember")),
            "plan": fetch_claude_plan(headers, oauth.get("subscriptionType"), oauth.get("rateLimitTier"))}


def parse_claude_reset_grants(block):
    """額度重置券（Anthropic 偶爾贈送，可清空 5 小時／每週窗）。沒有這個區塊回傳 None；

    不符資格算 0 張；已過期或用完的券略過（同 OpenUsage 的判斷）。
    """
    if not isinstance(block, dict):
        return None
    items = []
    if block.get("eligible"):
        now = datetime.datetime.now(datetime.timezone.utc)
        for g in block.get("grants") or []:
            left = int(g.get("resets_left") or 0)
            ends_at = g.get("ends_at")
            if left < 1:
                continue
            try:
                if ends_at and datetime.datetime.fromisoformat(ends_at) <= now:
                    continue
            except ValueError:
                pass
            items.append({"label": g.get("label") or g.get("id") or "額度重置", "left": left,
                          "ends_at": ends_at, "usable_now": bool(g.get("usable_now"))})
    return {"count": sum(i["left"] for i in items), "items": items}


def format_claude_plan(org_type, tier):
    """claude_max + default_claude_max_20x → "Max 20x"；claude_pro → "Pro"。"""
    if not org_type:
        return None
    name = org_type.removeprefix("claude_").replace("_", " ").title()
    m = re.search(r"\d+x", tier or "")
    return f"{name} {m.group(0)}" if m else name


def fetch_claude_plan(headers, stored_type, stored_tier):
    """方案名稱以即時 profile 為準（升級後不用重新登入就會更新）；查不到就退回憑證檔裡登入當時的方案。"""
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/profile", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            org = json.load(resp).get("organization") or {}
        plan = format_claude_plan(org.get("organization_type"), org.get("rate_limit_tier"))
        if plan:
            return plan
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        pass
    return format_claude_plan(f"claude_{stored_type}" if stored_type else None, stored_tier)


def window_label(seconds):
    if seconds == 18000:
        return "5 小時"
    if seconds == 604800:
        return "每週"
    return f"{seconds // 86400} 天" if seconds >= 86400 else f"{seconds // 3600} 小時"


def fetch_codex_quota():
    """查 Codex（ChatGPT 方案）額度：token 取自 Codex CLI 的 ~/.codex/auth.json。

    依窗長（limit_window_seconds）分辨 5 小時／每週，不依 primary/secondary 位置——
    Codex 有時會把剩下的每週窗搬進 primary（OpenUsage 文件記載的行為）。
    """
    auth_path = os.path.join(os.path.expanduser("~"), ".codex", "auth.json")
    try:
        with open(auth_path, encoding="utf-8") as f:
            tokens = json.load(f)["tokens"]
        headers = {
            "Authorization": "Bearer " + tokens["access_token"],
            "originator": "codex_cli_rs",
            "User-Agent": "codex_cli_rs",
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {"error": "找不到 Codex 憑證（~/.codex/auth.json）"}
    if tokens.get("account_id"):
        headers["chatgpt-account-id"] = tokens["account_id"]
    req = urllib.request.Request("https://chatgpt.com/backend-api/wham/usage", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"error": "Codex token 已失效，在終端機執行一次 codex 指令即可刷新"}
        return {"error": f"Codex 額度 API 回應 HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"error": f"Codex 額度 API 連線失敗：{e}"}
    rl = data.get("rate_limit") or {}
    windows = []
    for w in (rl.get("primary_window"), rl.get("secondary_window")):
        if not w or w.get("used_percent") is None:
            continue
        secs = w.get("limit_window_seconds") or 0
        reset_at = w.get("reset_at")
        windows.append({
            "label": window_label(secs),
            "pct": w["used_percent"],
            "resets_at": datetime.datetime.fromtimestamp(reset_at, datetime.timezone.utc).isoformat() if reset_at else None,
            "secs": secs,
            # 窗還沒開始計時：API 回報的重置時間永遠是「現在＋整個窗長」
            "not_started": w["used_percent"] == 0 and (w.get("reset_after_seconds") or 0) >= secs > 0,
        })
    if not windows:
        return {"error": "Codex 額度 API 回應中沒有額度窗資料"}
    windows.sort(key=lambda w: w["secs"])  # 短窗在上，跟 Claude 卡片同順序
    return {"windows": windows}


AGY_PROC_VBS = r'''Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name LIKE 'language_server_%'")
For Each p In procs
  WScript.Echo p.ProcessId & "|" & p.CommandLine
Next
'''


def _run_text(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, encoding="mbcs", errors="replace", timeout=10, **kw).stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def find_antigravity_targets():
    """找 agy CLI / Antigravity IDE language server 開在 127.0.0.1 的埠。回傳 [(port, csrf_token 或 None)]。

    移植自 ai_usage_dashboard/server.js（Windows 實測過）：agy CLI 不需 token；IDE 的
    language server 要帶啟動參數裡的 --csrf_token。用 cscript+WMI 讀命令列，因為 EDR 會擋子行程叫 powershell。
    """
    pid_token = {}
    for m in re.finditer(r'"agy\.exe","(\d+)"', _run_text('tasklist /FI "IMAGENAME eq agy.exe" /FO CSV /NH', shell=True)):
        pid_token[m[1]] = None
    vbs = os.path.join(tempfile.gettempdir(), "splitrail-lsproc.vbs")
    try:
        with open(vbs, "w", newline="\r\n") as f:
            f.write(AGY_PROC_VBS)
        for line in _run_text(["cscript", "//nologo", vbs]).splitlines():
            pid, _, cmd = line.partition("|")
            tok = re.search(r"--csrf_token[= ]+([0-9a-fA-F-]+)", cmd)
            if pid.strip().isdigit() and tok:
                pid_token[pid.strip()] = tok[1]
    except OSError:
        pass
    if not pid_token:
        return []
    netstat = _run_text("netstat -ano -p TCP", shell=True)
    return [(m[1], pid_token[m[2]])
            for m in re.finditer(r"^\s*TCP\s+127\.0\.0\.1:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$", netstat, re.M)
            if m[2] in pid_token]


def fetch_antigravity_quota():
    """查 Antigravity 額度：只在 agy CLI 或 Antigravity App 執行中才抓得到（讀它的本機服務）。

    回應結構：response.groups[]（Gemini 池、其他模型池）各有 buckets[]（window、remainingFraction、resetTime）。
    """
    targets = find_antigravity_targets()
    if not targets:
        return {"error": "Antigravity 沒有在執行（開著 Antigravity App 或 agy 才抓得到額度）"}
    ctx = ssl._create_unverified_context()  # 本機 loopback 自簽憑證，只對 127.0.0.1 略過驗證
    body = json.dumps({"metadata": {"ideName": "antigravity", "extensionName": "antigravity",
                                    "locale": "en", "ideVersion": "1.0.0"}}).encode()
    for port, token in targets:
        headers = {"Content-Type": "application/json", "Connect-Protocol-Version": "1"}
        if token:
            headers["X-Codeium-Csrf-Token"] = token
        req = urllib.request.Request(
            f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary",
            body, headers)
        try:
            with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
                groups = (json.load(resp).get("response") or {}).get("groups") or []
        except (urllib.error.URLError, OSError, ValueError, AttributeError):
            continue
        windows = []
        for g in groups:
            # 兩個池：Gemini Models、Claude and GPT models（名稱照 OpenUsage 簡稱）
            pool = "Gemini" if re.search("gemini", g.get("displayName") or "", re.I) else "Claude/GPT"
            pool_windows = []
            for b in g.get("buckets") or []:
                if b.get("remainingFraction") is None:
                    continue
                secs = {"weekly": 604800, "daily": 86400}.get(b.get("window"), 18000)
                pct = round((1 - b["remainingFraction"]) * 100, 1)
                pool_windows.append({
                    "label": f"{pool} {window_label(secs)}",
                    "pct": pct,
                    "resets_at": b.get("resetTime"),
                    "secs": secs,
                    # 5 小時窗沒用過時重置時間永遠是「現在＋5 小時」；每週窗照常倒數（同 OpenUsage）
                    "not_started": pct == 0 and secs < 604800,
                })
            windows += sorted(pool_windows, key=lambda w: w["secs"])  # 短窗在上
        if windows:
            return {"windows": windows}
    return {"error": "Antigravity 本機服務沒有回應額度資料"}


QUOTA_FETCHERS = {
    "Claude Code": fetch_claude_quota,
    "Codex CLI": fetch_codex_quota,
    "Antigravity CLI": fetch_antigravity_quota,
}


def build_providers(data, today):
    """整理成 HTML 用的每家資料：Today/Yesterday/30 Days 花費與 token（含分模型）、近 30 天每日趨勢。

    tokens 一律算 input+output+cached（實際處理量）；30 Days＝含今天往回 30 天。
    """
    days30 = [today - datetime.timedelta(days=i) for i in range(29, -1, -1)]
    periods = {"today": {today}, "yesterday": {today - datetime.timedelta(days=1)}, "last30": set(days30)}
    by_name = {item["analyzer_name"]: item for item in data.get("analyzer_stats", [])}
    names = [n for n in COLORS if n in by_name] + [n for n in by_name if n not in COLORS]
    providers = []
    for name in names:
        daily = by_name[name].get("daily_stats", {})
        per_day = {}
        for date_str, day in daily.items():
            try:
                per_day[datetime.date.fromisoformat(date_str)] = day
            except ValueError:
                continue
        spend = {}
        for key, dates in periods.items():
            p = {"cost": 0.0, "in": 0, "out": 0, "cached": 0, "models": {}}
            for d in dates:
                day = per_day.get(d)
                if not day:
                    continue
                s = day.get("stats", {})
                p["cost"] += s.get("costCents", 0) / 100
                p["in"] += s.get("inputTokens", 0)
                p["out"] += s.get("outputTokens", 0)
                p["cached"] += s.get("cachedTokens", 0)
                for model, m in (day.get("model_stats") or {}).items():
                    agg = p["models"].setdefault(model, {"cost": 0.0, "tokens": 0})
                    agg["cost"] += m.get("cost", 0)
                    agg["tokens"] += m.get("inputTokens", 0) + m.get("outputTokens", 0) + m.get("cachedTokens", 0)
            p["cost"] = round(p["cost"], 2)
            p["tokens"] = p["in"] + p["out"] + p["cached"]
            p["models"] = sorted(
                ({"name": k, "cost": round(v["cost"], 2), "tokens": v["tokens"]} for k, v in p["models"].items()),
                key=lambda m: (m["cost"], m["tokens"]), reverse=True)
            spend[key] = p
        trend = []
        for d in days30:
            s = (per_day.get(d) or {}).get("stats", {})
            trend.append({"date": d.isoformat(),
                          "tokens": s.get("inputTokens", 0) + s.get("outputTokens", 0) + s.get("cachedTokens", 0)})
        providers.append({
            "name": name,
            "color": COLORS.get(name, DEFAULT_COLOR),
            "spend": spend,
            "trend": trend,
            "quota": QUOTA_FETCHERS[name]() if name in QUOTA_FETCHERS else None,
        })
    return providers


def summarize(data, today):
    yesterday = today - datetime.timedelta(days=1)
    last7 = today - datetime.timedelta(days=6)  # 含今天共 7 天
    last30 = today - datetime.timedelta(days=29)  # 含今天共 30 天，與網頁的 30 Days 一致
    rows = []
    for item in data.get("analyzer_stats", []):
        name = item["analyzer_name"]
        daily = item.get("daily_stats", {})
        total_cost = last7_cost = last30_cost = today_cost = yesterday_cost = 0
        total_in = total_out = total_cached = total_tool = 0
        active_days = 0
        period_tokens = {
            "today": [0, 0, 0],
            "yesterday": [0, 0, 0],
            "last30": [0, 0, 0],
        }
        for date_str, day in daily.items():
            s = day.get("stats", {})
            cost = s.get("costCents", 0)
            in_tok = s.get("inputTokens", 0)
            out_tok = s.get("outputTokens", 0)
            cached_tok = s.get("cachedTokens", 0)
            if cost or in_tok or out_tok:
                active_days += 1
            total_cost += cost
            total_in += in_tok
            total_out += out_tok
            total_cached += cached_tok
            total_tool += s.get("toolCalls", 0)
            try:
                d = datetime.date.fromisoformat(date_str)
            except ValueError:
                continue
            if d == today:
                today_cost += cost
                period_tokens["today"] = [in_tok, out_tok, cached_tok]
            if d == yesterday:
                yesterday_cost += cost
                period_tokens["yesterday"] = [in_tok, out_tok, cached_tok]
            if d >= last7:
                last7_cost += cost
            if d >= last30:
                last30_cost += cost
                for i, v in enumerate((in_tok, out_tok, cached_tok)):
                    period_tokens["last30"][i] += v
        rows.append({
            "name": name,
            "color": COLORS.get(name, DEFAULT_COLOR),
            "conversations": item.get("num_conversations", 0),
            "active_days": active_days,
            "total_cost": total_cost / 100,
            "last7_cost": last7_cost / 100,
            "last30_cost": last30_cost / 100,
            "today_cost": today_cost / 100,
            "yesterday_cost": yesterday_cost / 100,
            "input_tokens": total_in,
            "output_tokens": total_out,
            "cached_tokens": total_cached,
            "tool_calls": total_tool,
            "period_tokens": period_tokens,
        })
    return rows


def print_table(rows, today):
    print(f"=== AI CLI 用量彙總（{today.isoformat()}，估算值非官方帳單）===\n")
    header = f"{'工具':<18}{'對話數':>8}{'活動天數':>8}{'累計$':>12}{'近7天$':>12}{'近30天$':>12}{'今日$':>10}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['name']:<18}{r['conversations']:>8}{r['active_days']:>8}"
            f"{r['total_cost']:>12.2f}{r['last7_cost']:>12.2f}"
            f"{r['last30_cost']:>12.2f}{r['today_cost']:>10.2f}"
        )
    print()
    for r in rows:
        print(
            f"[{r['name']}] tokens in/out/cached: "
            f"{r['input_tokens']:,}/{r['output_tokens']:,}/{r['cached_tokens']:,}"
            f" | tool calls: {r['tool_calls']:,}"
        )


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="300">
<title>AI 用量總覽</title>
<style>
  :root { color-scheme: dark; --blue: #4C8DF6; --yellow: #E5A54B; --red: #E5484D; }
  body {
    margin: 0; padding: 24px 16px; box-sizing: border-box; min-height: 100vh;
    background: #1B1D23; color: #E7E9EE;
    font-family: -apple-system, "Segoe UI", "Microsoft JhengHei", sans-serif;
  }
  .stack { width: 100%; max-width: 380px; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; }
  .card { background: #22242B; border-radius: 18px; padding: 16px 18px 14px; box-shadow: 0 8px 28px rgba(0,0,0,.35); }
  .card-head { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
  .card-head select {
    background: transparent; color: #E7E9EE; border: none; font: inherit; font-size: 16px; font-weight: 600;
    padding: 0; cursor: pointer;
  }
  .card-head select option { background: #22242B; }
  .info { color: #6C7079; font-size: 12px; cursor: help; }
  .tabs { display: flex; background: #2C2F38; border-radius: 12px; padding: 3px; margin-bottom: 16px; }
  .tab {
    flex: 1; text-align: center; padding: 6px 0; border-radius: 10px; font-size: 13px;
    color: #9AA0AC; cursor: pointer; user-select: none; transition: background .15s, color .15s;
  }
  .tab.active { background: #E7E9EE; color: #1B1D23; font-weight: 600; }
  .donut-row { display: flex; align-items: center; gap: 20px; }
  .donut-wrap { position: relative; width: 140px; height: 140px; flex: none; }
  .donut-wrap svg { transform: rotate(-90deg); }
  .donut-center {
    position: absolute; inset: 0; display: flex; flex-direction: column;
    align-items: center; justify-content: center; text-align: center; cursor: default;
  }
  .donut-center .amount { font-size: 21px; font-weight: 700; }
  .donut-center .unit { font-size: 11px; color: #9AA0AC; margin-top: 2px; }
  .legend { flex: 1; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
  .legend-item { display: flex; align-items: center; font-size: 13px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; margin-right: 8px; flex: none; }
  .legend-name { flex: 1; color: #C7CBD4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .legend-amount { font-weight: 600; margin-left: 6px; }
  .empty-note { font-size: 12px; color: #9AA0AC; text-align: center; padding: 8px 0 0; }
  .caret {
    display: flex; align-items: center; justify-content: center; gap: 4px; width: 100%;
    margin-top: 12px; padding: 6px 0 0; border: none; border-top: 1px solid #33363F; background: none;
    color: #6C7079; font: inherit; font-size: 11px; cursor: pointer;
  }
  .caret:hover { color: #C7CBD4; }
  .caret .chev { transition: transform .15s; }
  .caret.open .chev { transform: rotate(180deg); }
  .expand { display: none; padding-top: 10px; }
  .expand.open { display: block; }
  .sub-title { font-size: 11px; color: #9AA0AC; margin-bottom: 8px; }
  .token-list { display: flex; flex-direction: column; gap: 10px; }
  .token-item { font-size: 12px; }
  .row-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; gap: 8px; }
  .row-name { display: flex; align-items: center; color: #C7CBD4; }
  .row-nums { color: #E7E9EE; font-weight: 600; }
  .token-cached { color: #6C7079; font-weight: 400; margin-left: 6px; }
  .track { background: #2C2F38; border-radius: 6px; height: 6px; overflow: hidden; display: flex; position: relative; }
  .track .seg { height: 100%; flex: none; }
  .track .seg.cached { opacity: .55; }
  .prov-head { display: flex; align-items: center; font-size: 15px; font-weight: 600; margin-bottom: 12px; }
  .meter { font-size: 12px; margin-bottom: 12px; }
  .meter-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 5px; }
  .meter-label { color: #C7CBD4; }
  .meter-note { flex: 1; text-align: right; font-size: 11px; white-space: nowrap; }
  .meter-val { font-weight: 600; min-width: 34px; text-align: right; }
  .bar { position: relative; background: #2C2F38; border-radius: 6px; height: 7px; }
  .bar .fill { height: 100%; border-radius: 6px; }
  .bar .tick { position: absolute; top: -3px; width: 2px; height: 13px; background: #E7E9EE; border-radius: 1px; opacity: .7; }
  .meter-reset { color: #6C7079; font-size: 11px; margin-top: 5px; }
  .quota-error { font-size: 12px; color: var(--yellow); margin-bottom: 12px; }
  .trend { margin-top: 4px; }
  .trend svg { display: block; width: 100%; height: 36px; }
  .trend-caption { display: flex; justify-content: space-between; font-size: 11px; color: #6C7079; margin-bottom: 4px; }
  .spend-row {
    position: relative; display: flex; justify-content: space-between; font-size: 12px;
    padding: 5px 6px; margin: 0 -6px; border-radius: 6px;
  }
  .spend-row .row-nums { font-weight: 500; }
  .spend-row.has-models { cursor: default; }
  .spend-row.has-models:hover { background: #2C2F38; }
  .spend-row .nodata { color: #6C7079; font-weight: 400; }
  .popover {
    display: none; position: absolute; right: 0; top: calc(100% + 4px); z-index: 10; width: 250px;
    background: #2C2F38; border: 1px solid #3A3D47; border-radius: 10px; padding: 10px 12px;
    box-shadow: 0 8px 24px rgba(0,0,0,.45);
  }
  .spend-row.has-models:hover .popover { display: block; }
  .pm { margin-bottom: 8px; }
  .pm:last-child { margin-bottom: 0; }
  .pm-line { display: flex; justify-content: space-between; gap: 8px; }
  .pm-name { color: #E7E9EE; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pm-sub { color: #9AA0AC; font-size: 11px; margin: 1px 0 3px; }
  .pm .track { height: 3px; }
  .hint { font-size: 11px; color: #6C7079; margin-top: 8px; }
  .plan {
    margin-left: 8px; padding: 1px 7px; border-radius: 6px; background: #2C2F38;
    color: #9AA0AC; font-size: 11px; font-weight: 500;
  }
  .grants { margin-top: 6px; padding-top: 8px; border-top: 1px solid #33363F; }
  .footer { font-size: 11px; color: #6C7079; text-align: center; padding: 2px 0 8px; }
</style>
</head>
<body>
  <div class="stack">
    <section class="card" id="spendCard">
      <div class="card-head">
        <select id="metric" aria-label="指標"></select>
        <span class="info" title="花費是 splitrail 用本機 token 紀錄反推的 API 等價估算，不是官方帳單。Tokens 含 input、output 與 cached。">ⓘ</span>
      </div>
      <div class="tabs" id="tabs"></div>
      <div class="donut-row">
        <div class="donut-wrap">
          <svg width="140" height="140" viewBox="0 0 140 140">
            <circle cx="70" cy="70" r="58" fill="none" stroke="#33363F" stroke-width="15"></circle>
            <g id="arcs"></g>
          </svg>
          <div class="donut-center" id="donutCenter">
            <div class="amount" id="centerAmount"></div>
            <div class="unit" id="centerUnit"></div>
          </div>
        </div>
        <div class="legend" id="legend"></div>
      </div>
      <div class="empty-note" id="emptyNote" style="display:none;">這個區間沒有用量資料</div>
      <button class="caret" data-expand="tokens"><span>Token 明細</span><span class="chev">▾</span></button>
      <div class="expand" data-panel="tokens">
        <div class="sub-title">input / output，灰字為 cached；長條淡色段為 cached</div>
        <div class="token-list" id="tokenList"></div>
      </div>
    </section>
    <div id="providerCards" style="display:contents"></div>
    <div class="footer" id="footer"></div>
  </div>

<script>
const DATA = __DATA_JSON__;
const store = {
  get(k, d) { try { const v = localStorage.getItem("srd." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("srd." + k, JSON.stringify(v)); } catch (e) {} },
};
const PERIODS = [["today", "Today"], ["yesterday", "Yesterday"], ["last30", "30 Days"]];
const METRICS = { cost: "Cost", cpm: "Cost per MTok", tokens: "Tokens" };
let period = store.get("period", "last30");
let metric = store.get("metric", "cost");
if (!PERIODS.some(p => p[0] === period)) period = "last30";
if (!METRICS[metric]) metric = "cost";
const BLUE = "#4C8DF6", YELLOW = "#E5A54B", RED = "#E5484D";
const CIRC = 2 * Math.PI * 58;

function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]); }
function fmtMoney(n) { return n >= 1000 ? "$" + (n / 1000).toFixed(2) + "K" : "$" + n.toFixed(2); }
function fmtMoneyExact(n) { return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function fmtTokens(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
}
function fmtDuration(mins) {
  mins = Math.max(0, Math.round(mins));
  const d = Math.floor(mins / 1440), h = Math.floor(mins % 1440 / 60), m = mins % 60;
  return (d ? d + " 天 " : "") + (d || h ? h + " 小時 " : "") + m + " 分";
}
function fmtAt(t) {
  return new Date(t).toLocaleString("zh-TW", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

// ---------- 收合（記住開關狀態） ----------
function setupCaret(btn, panel, key) {
  const apply = open => { btn.classList.toggle("open", open); panel.classList.toggle("open", open); };
  apply(store.get("open." + key, false));
  btn.onclick = () => { const open = !panel.classList.contains("open"); store.set("open." + key, open); apply(open); };
}

// ---------- Total Spend 卡片 ----------
const metricEl = document.getElementById("metric");
Object.entries(METRICS).forEach(([k, label]) => {
  const o = document.createElement("option"); o.value = k; o.textContent = label; metricEl.appendChild(o);
});
metricEl.value = metric;
metricEl.onchange = () => { metric = metricEl.value; store.set("metric", metric); renderSpend(); };

const tabsEl = document.getElementById("tabs");
PERIODS.forEach(([key, label]) => {
  const el = document.createElement("div");
  el.className = "tab"; el.textContent = label; el.dataset.key = key;
  el.onclick = () => { period = key; store.set("period", period); renderSpend(); };
  tabsEl.appendChild(el);
});

function metricValue(p) {
  const s = p.spend[period];
  if (metric === "cost") return s.cost;
  if (metric === "tokens") return s.tokens;
  return s.cost > 0 && s.tokens > 0 ? s.cost / s.tokens * 1e6 : 0;
}
function fmtMetric(v) {
  if (metric === "cost") return fmtMoneyExact(v);
  if (metric === "tokens") return fmtTokens(v);
  return "$" + v.toFixed(2);
}

function renderSpend() {
  [...tabsEl.children].forEach(el => el.classList.toggle("active", el.dataset.key === period));
  const rows = DATA.providers.map(p => ({ p, v: metricValue(p) })).filter(r => r.v > 0).sort((a, b) => b.v - a.v);
  const total = rows.reduce((s, r) => s + r.v, 0);
  const amountEl = document.getElementById("centerAmount"), unitEl = document.getElementById("centerUnit");
  const centerEl = document.getElementById("donutCenter");
  document.getElementById("emptyNote").style.display = rows.length ? "none" : "block";
  if (!rows.length) {
    amountEl.textContent = "—"; unitEl.textContent = ""; centerEl.title = "";
  } else if (metric === "cost") {
    amountEl.textContent = total >= 1000 ? "$" + (total / 1000).toFixed(1) + "K" : "$" + total.toFixed(0);
    unitEl.textContent = "dollars";
    centerEl.title = fmtMoneyExact(total) + "（splitrail 本機估算）";
  } else if (metric === "tokens") {
    const big = total >= 1e9;
    amountEl.textContent = big ? (total / 1e9).toFixed(2) : (total / 1e6).toFixed(1);
    unitEl.textContent = big ? "billion" : "million";
    centerEl.title = total.toLocaleString("en-US") + " tokens";
  } else {
    const cost = rows.reduce((s, r) => s + r.p.spend[period].cost, 0);
    const tokens = rows.reduce((s, r) => s + r.p.spend[period].tokens, 0);
    const blended = cost / tokens * 1e6;
    amountEl.textContent = "$" + blended.toFixed(2);
    unitEl.textContent = "MTok";
    centerEl.title = "整體平均 $" + blended.toFixed(4) + " / 百萬 tokens（splitrail 本機估算）";
  }

  // 每段依佔比；再小的佔比也保留看得見的細條，最後等比縮放回整圈
  const lens = rows.map(r => Math.max(r.v / total * CIRC, 3));
  const scale = lens.length ? CIRC / lens.reduce((s, l) => s + l, 0) : 1;
  const arcsEl = document.getElementById("arcs"), legendEl = document.getElementById("legend");
  arcsEl.innerHTML = ""; legendEl.innerHTML = "";
  let offset = 0;
  rows.forEach((r, i) => {
    const len = lens[i] * scale;
    const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    Object.entries({ cx: 70, cy: 70, r: 58, fill: "none", stroke: r.p.color, "stroke-width": 15,
      "stroke-dasharray": `${len} ${CIRC - len}`, "stroke-dashoffset": -offset }).forEach(([k, v]) => c.setAttribute(k, v));
    arcsEl.appendChild(c);
    offset += len;
    const item = document.createElement("div");
    item.className = "legend-item";
    item.innerHTML = `<span class="dot" style="background:${r.p.color}"></span>
      <span class="legend-name">${esc(r.p.name)}</span><span class="legend-amount">${fmtMetric(r.v)}</span>`;
    legendEl.appendChild(item);
  });
  renderTokens();
}

function renderTokens() {
  const listEl = document.getElementById("tokenList");
  listEl.innerHTML = "";
  const entries = DATA.providers.map(p => ({ p, ...p.spend[period] })).filter(t => t.tokens > 0);
  if (!entries.length) { listEl.innerHTML = `<div class="empty-note">這個區間沒有 token 紀錄</div>`; return; }
  const max = Math.max(...entries.map(t => t.tokens));
  entries.sort((a, b) => b.tokens - a.tokens).forEach(t => {
    const item = document.createElement("div");
    item.className = "token-item";
    item.innerHTML = `
      <div class="row-head">
        <span class="row-name"><span class="dot" style="background:${t.p.color}"></span>${esc(t.p.name)}</span>
        <span class="row-nums">${fmtTokens(t.in)} / ${fmtTokens(t.out)}<span class="token-cached">cached ${fmtTokens(t.cached)}</span></span>
      </div>
      <div class="track">
        <div class="seg" style="width:${((t.in + t.out) / max * 100).toFixed(2)}%;background:${t.p.color}"></div>
        <div class="seg cached" style="width:${(t.cached / max * 100).toFixed(2)}%;background:${t.p.color}"></div>
      </div>`;
    listEl.appendChild(item);
  });
}

// ---------- 額度條 pace（依 OpenUsage 規則） ----------
// 藍：照目前速度重置時還剩 ≥10%；黃：會落在最後 10% 內但仍有 ≥1% 餘裕；紅：重置前會用完或剛好用完。
// 窗太新（經過 <5%）、尚未開始或沒有重置時間時無從預估，改依用量本身上色：≥80% 黃、≥90% 紅。
function pace(w, now) {
  const u = Math.min(100, Math.max(0, w.pct));
  const reset = w.resets_at ? Math.round(new Date(w.resets_at) / 60000) * 60000 : null;
  // 短窗（5 小時）用量 0% 時，閒置的回應可能是：沒有重置時間、重置時間已過、或重置時間＝現在＋整個窗長
  // （Codex/Antigravity 實測是第三種；Claude 閒置時是哪種尚未觀察到，三種都當成尚未開始）
  const shortWin = w.secs && w.secs < 604800;
  const notStarted = w.not_started || (u === 0 && (!reset ||
    (shortWin && (reset <= now || reset - now >= w.secs * 1000 - 120000))));
  const r = { u, color: BLUE, note: "", noteColor: "", tick: null, hover: "" };
  if (notStarted) r.resetText = "尚未開始（送出第一則訊息後才開始計時）";
  else if (!reset) r.resetText = "重置時間未知";
  else if (reset <= now) r.resetText = `已於 ${fmtAt(reset)} 重置，使用率待下次更新`;
  else r.resetText = `${fmtDuration((reset - now) / 60000)}後重置（${fmtAt(reset)}）`;

  if (u >= 99.5) { Object.assign(r, { color: RED, note: "🔥 已達上限", noteColor: RED, hover: "已達上限" }); return r; }
  const L = (w.secs || 0) * 1000;
  const e = reset && L && reset > now ? 1 - (reset - now) / L : null;
  if (notStarted || u === 0 || e === null || e < 0.05) {
    r.color = u >= 90 ? RED : u >= 80 ? YELLOW : BLUE;
    return r;
  }
  const proj = u / e;
  if (proj <= 90) {
    r.hover = `照目前速度，重置時約剩 ${Math.round(100 - proj)}%`;
  } else if (100 - proj >= 1) {
    Object.assign(r, { color: YELLOW, tick: e * 100, note: `約剩 ${Math.floor(100 - proj)}% 餘裕`, noteColor: YELLOW,
      hover: `照目前速度，重置時約用掉 ${Math.round(proj)}%` });
  } else {
    const start = reset - L;
    const runout = start + (now - start) * 100 / u;
    Object.assign(r, { color: RED, tick: e * 100, noteColor: RED,
      note: runout < reset - 60000 ? `🔥 預計 ${fmtDuration((runout - now) / 60000)}後用完` : "🔥",
      hover: proj > 100.5 ? `照目前速度，重置前會超出上限約 ${Math.round(proj - 100)}%` : "照目前速度，重置時約用掉 100%" });
  }
  return r;
}

function renderMeters(el, p) {
  el.innerHTML = "";
  const q = p.quota;
  if (!q) return;
  if (q.error) {
    const err = document.createElement("div");
    err.className = "quota-error";
    err.textContent = q.error;  // 錯誤訊息可能含 <...>，不能當 HTML
    el.appendChild(err);
    return;
  }
  const now = Date.now();
  q.windows.forEach(w => {
    const r = pace(w, now);
    const m = document.createElement("div");
    m.className = "meter";
    if (r.hover) m.title = r.hover;
    m.innerHTML = `
      <div class="meter-head">
        <span class="meter-label">${esc(w.label)}</span>
        <span class="meter-note" style="color:${r.noteColor}">${esc(r.note)}</span>
        <span class="meter-val">${Math.round(r.u)}%</span>
      </div>
      <div class="bar"><div class="fill" style="width:${r.u}%;background:${r.color}"></div>${r.tick !== null ? `<div class="tick" style="left:calc(${r.tick.toFixed(1)}% - 1px)"></div>` : ""}</div>
      <div class="meter-reset">${esc(r.resetText)}</div>`;
    el.appendChild(m);
  });
}

// ---------- 每家卡片 ----------
function trendHtml(p) {
  const max = Math.max(0, ...p.trend.map(d => d.tokens));
  if (!max) return `<div class="trend"><div class="trend-caption"><span>Usage Trend</span><span>近 30 天沒有用量</span></div></div>`;
  const peak = p.trend.reduce((a, b) => (b.tokens > a.tokens ? b : a));
  const bw = 300 / p.trend.length;
  const bars = p.trend.map((d, i) => {
    const h = d.tokens ? Math.max(2, d.tokens / max * 34) : 1;
    return `<rect x="${(i * bw + 1).toFixed(1)}" y="${(36 - h).toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${h.toFixed(1)}" rx="1.5"
      fill="${p.color}" opacity="${d.tokens ? 0.9 : 0.25}"><title>${d.date.slice(5)}　${d.tokens ? fmtTokens(d.tokens) + " tokens" : "No data"}</title></rect>`;
  }).join("");
  const range = `${p.trend[0].date.slice(5)} – ${p.trend[p.trend.length - 1].date.slice(5)}`;
  return `<div class="trend" title="峰值 ${peak.date.slice(5)}：${fmtTokens(peak.tokens)} tokens（${range}，splitrail 本機紀錄）">
    <div class="trend-caption"><span>Usage Trend</span><span>峰值 ${fmtTokens(peak.tokens)}</span></div>
    <svg viewBox="0 0 300 36" preserveAspectRatio="none">${bars}</svg></div>`;
}

function modelsHtml(s) {
  const byCost = s.cost > 0;
  const total = byCost ? s.cost : s.tokens;
  const models = s.models.filter(m => (byCost ? m.cost : m.tokens) > 0);
  if (!models.length || !total) return "";
  const shown = [], other = { name: "Other", cost: 0, tokens: 0 };
  models.forEach((m, i) => {
    const share = (byCost ? m.cost : m.tokens) / total;
    if (i < 4 && share >= 0.05) shown.push(m); else { other.cost += m.cost; other.tokens += m.tokens; }
  });
  if (other.cost > 0 || other.tokens > 0) shown.push(other);
  return `<div class="popover">` + shown.map(m => {
    const share = (byCost ? m.cost : m.tokens) / total * 100;
    return `<div class="pm">
      <div class="pm-line"><span class="pm-name">${esc(m.name)}</span><span>${fmtMoneyExact(m.cost)}</span></div>
      <div class="pm-sub">${share.toFixed(0)}% · ${fmtTokens(m.tokens)} tokens</div>
      <div class="track"><div class="seg" style="width:${share.toFixed(1)}%;background:#9AA0AC"></div></div></div>`;
  }).join("") + `</div>`;
}

function spendRowsHtml(p) {
  return PERIODS.map(([key, label]) => {
    const s = p.spend[key];
    if (!s.cost && !s.tokens) {
      return `<div class="spend-row"><span class="row-name">${label}</span><span class="row-nums nodata">No data</span></div>`;
    }
    const pop = modelsHtml(s);
    return `<div class="spend-row${pop ? " has-models" : ""}"><span class="row-name">${label}</span>
      <span class="row-nums">${fmtMoneyExact(s.cost)} · ${fmtTokens(s.tokens)} tokens</span>${pop}</div>`;
  }).join("");
}

// 額度重置券：顯示可用張數，滑鼠移上看每張的內容與期限（同 OpenUsage 的 Rate Limit Resets，唯讀）
function grantsHtml(p) {
  const g = p.quota && p.quota.grants;
  if (!g) return "";
  const pop = g.items.length ? `<div class="popover">` + g.items.map(it => `<div class="pm">
      <div class="pm-line"><span class="pm-name">${esc(it.label)}</span><span>×${it.left}</span></div>
      <div class="pm-sub">${it.ends_at ? "期限 " + fmtAt(it.ends_at) : "無期限"}${it.usable_now ? " · 現在可用" : ""}</div></div>`).join("") +
    `<div class="pm-sub">在 Claude Code 執行 /rate-limit-options 使用</div></div>` : "";
  return `<div class="grants"><div class="spend-row${pop ? " has-models" : ""}"><span class="row-name">額度重置券</span>
    <span class="row-nums${g.count ? "" : " nodata"}">${g.count} 張可用</span>${pop}</div></div>`;
}

const meterEls = [];
function renderProviders() {
  const wrap = document.getElementById("providerCards");
  DATA.providers.forEach((p, i) => {
    const card = document.createElement("section");
    card.className = "card";
    const hint = p.name === "Antigravity CLI"
      ? `<div class="hint">Antigravity 的花費受 splitrail 本身的 bug 影響，數字偏低。</div>` : "";
    card.innerHTML = `
      <div class="prov-head"><span class="dot" style="background:${p.color}"></span>${esc(p.name)}${
        p.quota && p.quota.plan ? `<span class="plan">${esc(p.quota.plan)}</span>` : ""}</div>
      <div class="meters"></div>
      ${trendHtml(p)}
      <button class="caret" data-expand="prov${i}"><span>顯示更多</span><span class="chev">▾</span></button>
      <div class="expand" data-panel="prov${i}">
        <div class="sub-title">花費（splitrail 估算，滑鼠移到數字上看分模型明細）</div>
        ${spendRowsHtml(p)}${grantsHtml(p)}${hint}
      </div>`;
    wrap.appendChild(card);
    meterEls.push([card.querySelector(".meters"), p]);
    setupCaret(card.querySelector(".caret"), card.querySelector(".expand"), p.name);
  });
}
function renderAllMeters() { meterEls.forEach(([el, p]) => renderMeters(el, p)); }

setupCaret(document.querySelector('[data-expand="tokens"]'), document.querySelector('[data-panel="tokens"]'), "tokens");
renderSpend();
renderProviders();
renderAllMeters();
setInterval(renderAllMeters, 30000);  // 倒數與 pace 每 30 秒更新；整頁每 5 分鐘重新載入新資料
document.getElementById("footer").textContent = "資料更新於 " + DATA.generated_at + " · 每 5 分鐘自動重新整理";
</script>
</body>
</html>
"""


def build_html(providers, generated_at):
    payload = {"generated_at": generated_at, "providers": providers}
    return HTML_TEMPLATE.replace("__DATA_JSON__", json.dumps(payload, ensure_ascii=False))


def main():
    want_html = "--html" in sys.argv
    out_path = None
    for i, arg in enumerate(sys.argv):
        if arg == "--html" and i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("-"):
            out_path = sys.argv[i + 1]

    data = run_splitrail()
    today = datetime.date.today()
    rows = summarize(data, today)
    print_table(rows, today)

    if want_html:
        providers = build_providers(data, today)
        html = build_html(providers, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        path = out_path or os.path.join(SCRIPT_DIR, "splitrail-dashboard.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\n已產生視覺化頁面：{path}")
        if "--no-open" not in sys.argv:
            webbrowser.open("file:///" + path.replace("\\", "/"))


if __name__ == "__main__":
    main()
