#!/usr/bin/env python3
"""一鍵彙總 Claude Code / Codex CLI / Antigravity CLI 三家用量。

跑 `splitrail.exe stats`，把 JSON 結果聚合成一張總覽表印出來。
費用是用本機 token 紀錄乘上官方 API 單價的估算值（Claude 由 splitrail 計算，
Codex／Antigravity 自行計算），不是官方帳單金額，
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
import glob
import sqlite3
import time
import urllib.error
import urllib.request
import webbrowser

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def find_splitrail_exe():
    """只用腳本同層目錄的 splitrail.exe（不退回用名稱在 PATH／目前資料夾尋找，避免誤執行同名的其他程式）。"""
    return os.path.join(SCRIPT_DIR, "splitrail.exe")


SPLITRAIL_EXE = find_splitrail_exe()

# 跟 openusage 風格對齊的配色，順序 = 圖例排序
COLORS = {
    "Claude Code": "#E8734A",
    "Codex CLI": "#22B573",
    "Antigravity CLI": "#6C63FF",
}
DEFAULT_COLOR = "#8A8F98"


def run_splitrail():
    try:
        result = subprocess.run(
            [SPLITRAIL_EXE, "stats"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError:
        print("找不到 splitrail.exe，請依 README 安裝步驟下載並放進本資料夾", file=sys.stderr)
        sys.exit(1)
    if result.returncode != 0:
        print("splitrail.exe 執行失敗：", result.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print("無法解析 splitrail 輸出的 JSON：", result.stdout[:500], file=sys.stderr)
        sys.exit(1)


def window_label(seconds):
    if seconds == 18000:
        return "5 小時"
    if seconds == 604800:
        return "每週"
    return f"{seconds // 86400} 天" if seconds >= 86400 else f"{seconds // 3600} 小時"


# ---------- 訂閱額度：直接查官方用量 API（Claude token 過期時由本儀表板自己換新） ----------
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code 的 OAuth client（同 OpenUsage）
CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Claude Code 的 User-Agent：重置券只回給這個來源；token 端點前的 Cloudflare 會以 403 (error 1010)
# 擋掉 Python 預設的 User-Agent（2026-09-27 實測）
CLAUDE_CLI_UA = "claude-cli/2.1.283 (external, cli)"


def refresh_claude_token(cred_path):
    """token 過期（或 5 分鐘內到期）時用 refresh token 換新，並寫回 ~/.claude/.credentials.json。

    refresh token 用過即作廢，一定要寫回，否則 Claude Code CLI 下次拿舊的會被要求重新登入。
    **同一時間只能有一個程式負責換新**（不要同時開 Pane／OpenUsage 這類也會換 token 的工具）。防護：
    寫入前備份到 .credentials.json.splitrail-bak、沿用原本的 scopes、只改 token 相關欄位並保留其他欄位、
    先寫暫存檔再整檔替換；寫入前重讀一次，若 CLI 在這期間已經換過就不覆蓋它的結果。
    2026-09-27 實測：換新後 CLI 仍登入、可正常請求，寫回的 refresh token 可再次換新。
    """
    try:
        with open(cred_path, encoding="utf-8") as f:
            original_text = f.read()
        data = json.loads(original_text)
        oauth = data["claudeAiOauth"]
        refresh_token = oauth["refreshToken"]
    except (OSError, ValueError, KeyError, TypeError):
        return {"error": "Claude token 已過期，且讀不到 refresh token，請在終端機執行 claude auth login"}
    scopes = oauth.get("scopes") or ["user:profile", "user:inference", "user:sessions:claude_code",
                                     "user:mcp_servers", "user:file_upload"]
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": refresh_token,
                       "client_id": CLAUDE_OAUTH_CLIENT_ID, "scope": " ".join(scopes)}).encode()
    req = urllib.request.Request(CLAUDE_OAUTH_TOKEN_URL, body,
                                 {"Content-Type": "application/json", "User-Agent": CLAUDE_CLI_UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            got = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            code = json.loads(e.read()).get("error")
        except ValueError:
            code = None
        if code == "invalid_grant":
            return {"error": "Claude 登入已失效，請在終端機執行 claude auth login 重新登入"}
        return {"error": f"Claude token 更新失敗（HTTP {e.code}）"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"error": f"Claude token 更新失敗：{e}"}
    if not got.get("access_token"):
        return {"error": "Claude token 更新失敗：回應沒有新的 access token"}

    now_ms = time.time() * 1000
    new_oauth = dict(oauth)
    new_oauth["accessToken"] = got["access_token"]
    if got.get("refresh_token"):
        new_oauth["refreshToken"] = got["refresh_token"]
    if got.get("expires_in"):
        new_oauth["expiresAt"] = int(now_ms + got["expires_in"] * 1000)
    if got.get("refresh_token_expires_in"):
        new_oauth["refreshTokenExpiresAt"] = int(now_ms + got["refresh_token_expires_in"] * 1000)
    result = {"token": got["access_token"], "oauth": new_oauth}

    try:
        with open(cred_path, encoding="utf-8") as f:
            if f.read() != original_text:
                return result  # CLI 在這期間已經更新過憑證檔，保留它的版本；這次用我們換到的 token 就好
        with open(cred_path + ".splitrail-bak", "w", encoding="utf-8", newline="") as f:
            f.write(original_text)
        data["claudeAiOauth"] = new_oauth
        tmp = cred_path + ".splitrail-tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps(data, separators=(",", ":")))  # 與原檔相同的緊湊格式
        os.replace(tmp, cred_path)
    except OSError as e:
        # 伺服器已經作廢舊的 refresh token，寫不回去 CLI 下次就得重新登入——一定要讓使用者看到
        print(f"⚠ Claude token 已更新但寫回憑證檔失敗：{e}；Claude Code CLI 可能需要重新登入（claude auth login）",
              file=sys.stderr)
    return result


def fetch_claude_quota():
    """查 Claude 訂閱額度（claude /usage 背後的 API）：5 小時窗與每週窗的使用率和重置時間。

    token 取自 Claude Code CLI 的 ~/.claude/.credentials.json；過期時由 refresh_claude_token 換新並寫回
    （桌面 App 不會更新這個檔）。任何失敗都回傳 error，不中斷整頁產生。
    """
    cred_path = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
    try:
        with open(cred_path, encoding="utf-8") as f:
            oauth = json.load(f)["claudeAiOauth"]
        token = oauth["accessToken"]
    except (OSError, ValueError, KeyError):
        return {"error": "找不到 Claude 憑證（~/.claude/.credentials.json）"}
    if oauth.get("expiresAt") and time.time() * 1000 > oauth["expiresAt"] - 5 * 60 * 1000:
        refreshed = refresh_claude_token(cred_path)
        if "error" in refreshed:
            return refreshed
        token, oauth = refreshed["token"], refreshed["oauth"]
    headers = {
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        # 額度重置券（cedar_ember）只回給 Claude Code 這個來源，其他 User-Agent 會得到
        # eligible:false, ineligible_reason:"surface"（2026-09-27 實測；OpenUsage 同樣寫死版本號）
        "User-Agent": CLAUDE_CLI_UA,
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
    plan = data.get("plan_type")
    return {"windows": windows, "plan": plan.capitalize() if isinstance(plan, str) and plan else None}


AGY_PROC_VBS = r'''Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name LIKE 'language_server_%'")
For Each p In procs
  WScript.Echo p.ProcessId & "|" & p.CommandLine
Next
'''


# Windows 內建工具一律用完整路徑呼叫：用名稱呼叫時 Windows 會先找目前資料夾，可能誤執行同名的其他程式
SYSTEM32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")


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
    tasklist = _run_text([os.path.join(SYSTEM32, "tasklist.exe"), "/FI", "IMAGENAME eq agy.exe", "/FO", "CSV", "/NH"])
    for m in re.finditer(r'"agy\.exe","(\d+)"', tasklist):
        pid_token[m[1]] = None
    # 每次用不重複的暫存檔名，執行完就刪除（固定檔名可能在寫入與執行之間被換掉）
    vbs = None
    try:
        fd, vbs = tempfile.mkstemp(prefix="splitrail-lsproc-", suffix=".vbs")
        with os.fdopen(fd, "w", newline="\r\n") as f:
            f.write(AGY_PROC_VBS)
        for line in _run_text([os.path.join(SYSTEM32, "cscript.exe"), "//nologo", vbs]).splitlines():
            pid, _, cmd = line.partition("|")
            tok = re.search(r"--csrf_token[= ]+([0-9a-fA-F-]+)", cmd)
            if pid.strip().isdigit() and tok:
                pid_token[pid.strip()] = tok[1]
    except OSError:
        pass
    finally:
        if vbs:
            try:
                os.remove(vbs)
            except OSError:
                pass
    if not pid_token:
        return []
    netstat = _run_text([os.path.join(SYSTEM32, "netstat.exe"), "-ano", "-p", "TCP"])
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
    # 僅限連本機 127.0.0.1 的 Antigravity language server（自簽憑證）；不得把這個 context 用在任何對外連線。
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


# ---------- Antigravity 花費：自己讀本機對話資料庫（取代 splitrail 的 Antigravity 數字） ----------
# splitrail 3.10.1 把 token 欄位對錯（把 input 當 output、cache 當 input）、只讀 CLI 資料夾，數字不可用。
# 這裡照 OpenUsage／CrossUsage 的讀法：~/.gemini/antigravity*/conversations/**/*.db 的 gen_metadata 逐筆讀，
# data 為 protobuf：field 1 → field 4 = 用量（1 系統提示＋2 input＝計費 input、3 output、5 cache read），
# field 19 模型代號、21 顯示名稱，field 9 → 4 為時間（缺的話用 steps.metadata 的 field 1）。

# 美元／每百萬 token：(input, output, cache read)。來源：Google 官方價目頁
# https://ai.google.dev/gemini-api/docs/pricing（2026-09-28 核對，頁面更新於 2026-09-24）。
# 3.6～3.8 Flash 的優惠價到 2026-12-31，之後恢復原價；3.1 Pro 單次提示超過 20 萬 token 用較高單價。
GEMINI_PRICES = {
    "gemini-3.8-flash": [(datetime.date(2026, 12, 31), (0.75, 3.75, 0.075)), (None, (1.5, 7.5, 0.15))],
    "gemini-3.7-flash": [(datetime.date(2026, 12, 31), (0.75, 3.75, 0.075)), (None, (1.5, 7.5, 0.15))],
    "gemini-3.6-flash": [(datetime.date(2026, 12, 31), (0.75, 3.75, 0.075)), (None, (1.5, 7.5, 0.15))],
    "gemini-3.5-flash": [(None, (1.5, 9.0, 0.15))],
    "gemini-3.1-pro": [(None, (2.0, 12.0, 0.2))],
}
GEMINI_PRO_LONG_PROMPT = (200_000, (4.0, 18.0, 0.4))


def _pb_varint(b, i):
    result = shift = 0
    while True:
        c = b[i]
        i += 1
        result |= (c & 0x7F) << shift
        shift += 7
        if c < 0x80:
            return result, i


def _pb_fields(b):
    """把 protobuf 位元組拆成 [(欄位號, wire type, 值)]；遇到壞資料就回傳已解出的部分。"""
    out, i = [], 0
    try:
        while i < len(b):
            key, i = _pb_varint(b, i)
            num, wt = key >> 3, key & 7
            if wt == 0:
                val, i = _pb_varint(b, i)
            elif wt == 2:
                n, i = _pb_varint(b, i)
                val, i = b[i:i + n], i + n
            elif wt == 1:
                val, i = b[i:i + 8], i + 8
            elif wt == 5:
                val, i = b[i:i + 4], i + 4
            else:
                return out
            out.append((num, wt, val))
    except IndexError:
        pass
    return out


def _pb_get(b, num, wt):
    return next((v for n, w, v in _pb_fields(b) if n == num and w == wt), None) if b else None


def gemini_canonical(model_id, label):
    """內部代號／顯示名稱 → 計價用模型名。優先看顯示名稱（使用者實際選的模型），
    因為同一個代號會對應不同版本，例如 gemini-3-flash-a 的顯示名稱是「Gemini 3.5 Flash」。"""
    m = re.match(r"(?i)^Gemini (3\.[5-8]) Flash\b", label or "")
    if m:
        return f"gemini-{m.group(1)}-flash"
    if re.match(r"(?i)^Gemini 3\.1 Pro\b", label or ""):
        return "gemini-3.1-pro"
    m = re.match(r"^gemini-(3\.[5-8])-flash\b", model_id or "")
    if m:
        return f"gemini-{m.group(1)}-flash"
    if re.match(r"^gemini-(3\.1-pro|pro-(default|agent))\b", model_id or ""):
        return "gemini-3.1-pro"
    return model_id or label or "unknown"


def gemini_cost(model, day, inp, out, cache):
    tiers = GEMINI_PRICES.get(model)
    if not tiers:
        return None  # 沒有單價：token 照算，花費不計，模型名稱會出現在分模型明細裡
    price = next(p for until, p in tiers if until is None or day <= until)
    if model == "gemini-3.1-pro" and inp + cache > GEMINI_PRO_LONG_PROMPT[0]:
        price = GEMINI_PRO_LONG_PROMPT[1]
    return (inp * price[0] + out * price[1] + cache * price[2]) / 1_000_000


def scan_antigravity_daily():
    """回傳 (daily, 對話數)：daily 與 splitrail daily_stats 同形狀 {日期: {"stats", "model_stats"}}；
    每個 .db 是一段對話。讀不了的 db 略過。"""
    daily, conversations = {}, set()
    root = os.path.join(os.path.expanduser("~"), ".gemini")
    for db in glob.glob(os.path.join(root, "antigravity*", "conversations", "**", "*.db"), recursive=True):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cols = [r[1] for r in con.execute("PRAGMA table_info(steps)")]
            step_meta = dict(con.execute("SELECT idx, metadata FROM steps")) if "metadata" in cols else {}
            rows = con.execute("SELECT idx, data FROM gen_metadata WHERE data IS NOT NULL").fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for idx, data in rows:
            event = _pb_get(data, 1, 2)
            usage = _pb_get(event, 4, 2)
            if not usage:
                continue
            u = {n: v for n, w, v in _pb_fields(usage) if w == 0}
            inp, out, cache = u.get(1, 0) + u.get(2, 0), u.get(3, 0), u.get(5, 0)
            if not (u.get(2) or out or cache):
                continue  # 只有系統提示數的簿記紀錄，不是一次生成（同 OpenUsage）
            ts = _pb_get(_pb_get(_pb_get(event, 9, 2), 4, 2), 1, 0) or _pb_get(_pb_get(step_meta.get(idx), 1, 2), 1, 0)
            if not ts:
                continue
            day = datetime.datetime.fromtimestamp(ts).date()
            conversations.add(db)
            model_id = (_pb_get(event, 19, 2) or b"").decode("utf-8", "replace")
            label = (_pb_get(event, 21, 2) or b"").decode("utf-8", "replace")
            model = gemini_canonical(model_id, label)
            cost = gemini_cost(model, day, inp, out, cache) or 0.0
            d = daily.setdefault(day.isoformat(), {"stats": {}, "model_stats": {}})
            for target in (d["stats"], d["model_stats"].setdefault(model, {"model": model})):
                target["inputTokens"] = target.get("inputTokens", 0) + inp
                target["outputTokens"] = target.get("outputTokens", 0) + out
                target["cachedTokens"] = target.get("cachedTokens", 0) + cache
            d["stats"]["costCents"] = d["stats"].get("costCents", 0) + cost * 100
            m = d["model_stats"][model]
            m["cost"] = m.get("cost", 0) + cost
    return daily, len(conversations)


# ---------- Codex 花費：自己讀 ~/.codex/sessions（取代 splitrail 的 Codex 數字） ----------
# splitrail 一律用標準價、不處理 fork／subagent 子 session 重播父 session 的 token 歷史、沒有 272K 長 context 單價。
# 子 session 重播的處理方式也跟 OpenUsage／CrossUsage／Pane 不同，見 parse_codex_session。
# 這裡照 OpenUsage／CrossUsage／Pane 的做法補上。reasoning 不另外加到 output：本機紀錄 5,109 筆 reasoning>0 的回合
# 全部是 total_tokens = input + output，reasoning 已包含在 output 內（2026-09-28 實測）。
# 與 Pane 的 spend_cache 逐模型對照（2026-09-28）：terra、astra 完全一致；sol 差在單價（Pane 用原價）；
# Pane 另把子 session 重播的父 session 歷史算成 gpt-5（重複計算），這裡不會。

# 美元／每百萬 token：(input, cache read, output, 超過 272K 時的 (input, cache, output), fast 倍率)。
# 來源：OpenAI 官方模型頁 developers.openai.com/api/docs/models/<model>（2026-09-28 核對）。
# gpt-5.6-sol 用原價 5/0.5/30（官方頁目前的促銷價 4/0.4/20 未載明開始日期；使用者裁定用原價，與 Pane／OpenUsage 一致）。
# fast（原 priority，2026-07-30 改名）倍率：gpt-6-astra 官方頁寫 2 倍；gpt-5.5 2.5 倍、5.6 系列 2 倍取自
# OpenUsage／CrossUsage 的對照表（5.6 系列官方頁未寫，未查證）。
CODEX_PRICES = {
    "gpt-6-astra": (10.0, 1.0, 50.0, (20.0, 2.0, 75.0), 2.0),
    "gpt-5.6-sol": (5.0, 0.5, 30.0, (10.0, 1.0, 45.0), 2.0),
    "gpt-5.6-terra": (2.0, 0.2, 12.0, (4.0, 0.4, 18.0), 2.0),
    "gpt-5.6-luna": (0.2, 0.02, 1.2, (0.4, 0.04, 1.8), 2.0),
    "gpt-5.5": (5.0, 0.5, 30.0, (10.0, 1.0, 45.0), 2.5),
}
CODEX_LONG_CONTEXT_TOKENS = 272_000
# 紀錄裡沒有實際模型的特殊名稱：auto-review 依 ccusage／Pane 的對照表推定 2026-04-23 起為 gpt-5.5（推定，未查證）；
# gpt-reserve 依 OpenUsage 文件以 gpt-5.6-luna 計價。分模型明細仍顯示原名。
CODEX_PRICING_ALIAS = {"codex-auto-review": "gpt-5.5", "gpt-reserve": "gpt-5.6-luna"}


def codex_pricing_model(model):
    base = CODEX_PRICING_ALIAS.get(model, model)
    base = re.sub(r"-(\d{4}-\d{2}-\d{2}|\d{8})$", "", base)  # 去掉日期版號
    fast = base.endswith("-fast")
    base = base.removesuffix("-fast")
    base = re.sub(r"-(none|minimal|low|medium|high|xhigh|max|ultra)$", "", base)  # 去掉推理強度後綴
    return base, fast


def codex_event_cost(model, inp, cached, out, is_fast):
    base, alias_fast = codex_pricing_model(model)
    price = CODEX_PRICES.get(base)
    if not price:
        return None
    rin, rcache, rout, long_rates, fast_mult = price
    if inp > CODEX_LONG_CONTEXT_TOKENS:  # 單次請求 input（含 cache）超過 272K，整個請求用長 context 單價
        rin, rcache, rout = long_rates
    cost = ((inp - cached) * rin + cached * rcache + out * rout) / 1_000_000
    return cost * (fast_mult if is_fast or alias_fast else 1)


def _iso_to_local_date(text):
    try:
        return datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00")).astimezone().date()
    except (ValueError, AttributeError):
        return None


def _codex_is_child_session(payload):
    """fork 或 subagent 產生的子 session：開頭會重播父 session 的 token 歷史，這段不能重複計費。"""
    source = payload.get("source")
    return bool(payload.get("forked_from_id") or payload.get("parent_thread_id")
                or payload.get("thread_source") == "subagent"
                or (isinstance(source, dict) and source.get("subagent")))


def _codex_usage(u):
    return {k: int(u.get(k) or 0) for k in
            ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")}


def parse_codex_session(path):
    """讀一個 Codex session 檔，回傳 (是否為子 session, [(累計值簽章, 日期, 模型, input, cached, output, reasoning, is_fast)])。

    子 session（fork／subagent）開頭會把父 session 的 token 歷史原封不動重播一遍，而且重播可能出現在子 session
    自己的 task_started 之後（2026-09-28 實測），所以不能靠 task_started 判斷重播結束（Pane／CrossUsage 的做法，
    會把重播算兩次）。改為：累計值簽章交給呼叫端跨檔案去重；子 session 在第一個 turn_context 之前的紀錄沒有模型、
    必定是重播，直接略過（也涵蓋父 session 檔案已刪除的情況）。
    """
    events = []
    prev_totals, current_model, is_fast, is_child, saw_meta = None, None, False, False, False
    markers = ('"turn_context"', '"session_meta"', '"thread_settings_applied"', '"token_count"')
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return False, events
    with f:
        for line in f:
            if not any(m in line for m in markers):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            typ, p = obj.get("type"), obj.get("payload") or {}
            if not isinstance(p, dict):
                continue
            if typ == "turn_context":
                current_model = p.get("model") or p.get("model_name") or current_model
                continue
            if typ == "session_meta":
                if not saw_meta:
                    saw_meta = True
                    is_child = _codex_is_child_session(p)
                continue
            if typ != "event_msg":
                continue
            ptype = p.get("type")
            if ptype == "thread_settings_applied":
                tier = ((p.get("thread_settings") or {}).get("service_tier") or p.get("service_tier") or "").strip()
                if tier:
                    is_fast = tier in ("fast", "priority")
                continue
            if ptype != "token_count":
                continue
            day = _iso_to_local_date(obj.get("timestamp"))
            info = p.get("info") or {}
            totals = _codex_usage(info["total_token_usage"]) if info.get("total_token_usage") else None
            if day is None or (totals and totals == prev_totals):
                continue
            if info.get("last_token_usage"):
                u = _codex_usage(info["last_token_usage"])
            elif totals:
                base = prev_totals or {}
                u = {k: max(v - base.get(k, 0), 0) for k, v in totals.items()}
            else:
                continue
            if totals:
                prev_totals = totals
            if not (u["input_tokens"] or u["cached_input_tokens"] or u["output_tokens"]):
                continue
            model = p.get("model") or info.get("model") or current_model
            if is_child and not model:
                continue  # 子 session 還沒有自己的回合：父 session 的重播
            signature = tuple(sorted(totals.items())) if totals else None
            cached = min(u["cached_input_tokens"], u["input_tokens"])
            events.append((signature, day, model or "unknown", u["input_tokens"], cached, u["output_tokens"],
                           u["reasoning_output_tokens"], is_fast))
    return is_child, events


def codex_events():
    """所有 Codex 回合，跨檔案去重後回傳 [(日期, 模型, input, cached, output, reasoning, is_fast)] 與對話數。

    sessions 與 archived_sessions 以檔名去重；同一個累計值簽章只算第一次出現，先處理一般 session 再處理子 session，
    讓重播的用量歸給父 session（模型資訊也是父 session 的）。
    """
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    files = {}
    for sub in ("sessions", "archived_sessions"):  # sessions 優先
        for path in glob.glob(os.path.join(home, sub, "**", "*.jsonl"), recursive=True):
            files.setdefault(os.path.basename(path), path)
    parsed = sorted((parse_codex_session(path) for path in sorted(files.values())), key=lambda r: r[0])
    seen, out, conversations = set(), [], 0
    for _is_child, events in parsed:
        kept = 0
        for signature, *event in events:
            if signature is not None:
                if signature in seen:
                    continue
                seen.add(signature)
            out.append(tuple(event))
            kept += 1
        conversations += kept > 0
    return out, conversations


def scan_codex_daily():
    """回傳 (daily, 對話數)，daily 與 splitrail daily_stats 同形狀。"""
    events, conversations = codex_events()
    daily = {}
    for day, model, inp, cached, out, reasoning, is_fast in events:
        cost = codex_event_cost(model, inp, cached, out, is_fast) or 0.0
        d = daily.setdefault(day.isoformat(), {"stats": {}, "model_stats": {}})
        for target in (d["stats"], d["model_stats"].setdefault(model, {"model": model})):
            target["inputTokens"] = target.get("inputTokens", 0) + inp - cached  # 與 splitrail 相同：input 不含 cache
            target["cachedTokens"] = target.get("cachedTokens", 0) + cached
            target["outputTokens"] = target.get("outputTokens", 0) + out
            target["reasoningTokens"] = target.get("reasoningTokens", 0) + reasoning
        d["stats"]["costCents"] = d["stats"].get("costCents", 0) + cost * 100
        m = d["model_stats"][model]
        m["cost"] = m.get("cost", 0) + cost
    return daily, conversations


def replace_analyzer_daily(data, name, daily, conversations):
    """用自己算的每日資料取代 splitrail 對某個工具的結果（沒有紀錄時整個移除，不留錯的數字）。"""
    stats = data.get("analyzer_stats", [])
    entry = {"analyzer_name": name, "daily_stats": daily, "num_conversations": conversations}
    pos = next((i for i, a in enumerate(stats) if a.get("analyzer_name") == name), None)
    if pos is None:
        stats.append(entry)
    else:
        stats[pos] = entry  # 原地替換，保持工具的顯示順序
    if not daily:
        stats.remove(entry)
    data["analyzer_stats"] = stats


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
  /* 寬螢幕一頁呈現：左欄總花費，右邊每家工具一欄；窄螢幕維持單欄 */
  /* 卡片撐滿視窗高度、收合內容直接展開，趨勢圖吃掉剩餘高度；內容超過一頁時才捲動 */
  @media (min-width: 1100px) {
    /* 以 Windows 150% 縮放的 1920x1080 筆電為基準：瀏覽器 100% 時可用約 1280x560，要一頁放下 */
    body { padding: 12px 12px 4px; }
    .stack {
      max-width: none; display: grid; gap: 12px; align-items: stretch;
      grid-template-columns: minmax(0, 1.25fr) repeat(3, minmax(0, 1fr));
      grid-template-rows: 1fr auto; min-height: calc(100vh - 16px);
    }
    .footer { grid-column: 1 / -1; padding: 0; }
    .card { display: flex; flex-direction: column; padding: 12px 14px 10px; }
    .card-head, .prov-head { margin-bottom: 8px; }
    .tabs { margin-bottom: 10px; }
    .meter { margin-bottom: 7px; }
    .meter-head { margin-bottom: 3px; }
    .meter-reset { margin-top: 3px; }
    .quota-error { margin-bottom: 8px; }
    .caret { display: none; }
    .expand { display: block; margin-top: 8px; padding-top: 8px; border-top: 1px solid #33363F; }
    .sub-title { margin-bottom: 4px; }
    .spend-row { padding: 3px 6px; }
    .grants { margin-top: 4px; padding-top: 4px; }
    .hint { margin-top: 4px; }
    .token-list { gap: 6px; }
    .trend { flex: 1; display: flex; flex-direction: column; min-height: 44px; max-height: 160px; margin-bottom: 8px; }
    .trend svg { flex: 1; height: auto; min-height: 24px; }
    .card > .expand { margin-top: auto; }  /* 趨勢圖到頂後多出的空間放在花費區上方，各卡花費區貼底對齊 */
    .expand .spend-row { font-size: 14px; padding: 5px 6px; }
    .expand .sub-title { font-size: 12px; }
    #spendCard .donut-row { flex: 1; min-height: 120px; gap: 14px; }
    #spendCard .donut-wrap { width: clamp(110px, 24vh, 190px); height: clamp(110px, 24vh, 190px); }
    #spendCard .donut-wrap svg { width: 100%; height: 100%; }
    #spendCard .donut-center .amount { font-size: 28px; }
    #spendCard .donut-center .unit { font-size: 12px; }
    .popover { top: auto; bottom: calc(100% + 4px); }
  }
</style>
</head>
<body>
  <div class="stack">
    <section class="card" id="spendCard">
      <div class="card-head">
        <select id="metric" aria-label="指標"></select>
        <span class="info" title="花費是用本機紀錄的 token 數乘上官方 API 單價估算，不是官方帳單（Claude 由 splitrail 計算，Codex／Antigravity 由本儀表板自行計算）。Tokens 含 input、output 與 cached。">ⓘ</span>
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
    centerEl.title = fmtMoneyExact(total) + "（本機紀錄估算）";
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
    centerEl.title = "整體平均 $" + blended.toFixed(4) + " / 百萬 tokens（本機紀錄估算）";
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
  if (q.note) {  // 資料來源提示（Pane 資料較舊、改用直讀等）
    const note = document.createElement("div");
    note.className = "meter-reset";
    note.textContent = q.note;
    el.appendChild(note);
  }
}

// ---------- 每家卡片 ----------
function trendHtml(p) {
  const max = Math.max(0, ...p.trend.map(d => d.tokens));
  if (!max) return `<div class="trend"><div class="trend-caption"><span>Usage Trend</span><span>近 30 天沒有用量</span></div></div>`;
  const peak = p.trend.reduce((a, b) => (b.tokens > a.tokens ? b : a));
  const bw = 300 / p.trend.length;
  const bars = p.trend.map((d, i) => {
    // 寬螢幕上 SVG 會被垂直拉伸（preserveAspectRatio=none），所以不加圓角、空白日只畫很薄的底線
    const h = d.tokens ? Math.max(1.2, d.tokens / max * 34) : 0.4;
    return `<rect x="${(i * bw + 1).toFixed(1)}" y="${(36 - h).toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${h.toFixed(1)}"
      fill="${p.color}" opacity="${d.tokens ? 0.9 : 0.25}"><title>${d.date.slice(5)}　${d.tokens ? fmtTokens(d.tokens) + " tokens" : "No data"}</title></rect>`;
  }).join("");
  const range = `${p.trend[0].date.slice(5)} – ${p.trend[p.trend.length - 1].date.slice(5)}`;
  return `<div class="trend" title="峰值 ${peak.date.slice(5)}：${fmtTokens(peak.tokens)} tokens（${range}，本機紀錄）">
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
    card.innerHTML = `
      <div class="prov-head"><span class="dot" style="background:${p.color}"></span>${esc(p.name)}${
        p.quota && p.quota.plan ? `<span class="plan">${esc(p.quota.plan)}</span>` : ""}</div>
      <div class="meters"></div>
      ${trendHtml(p)}
      <button class="caret" data-expand="prov${i}"><span>顯示更多</span><span class="chev">▾</span></button>
      <div class="expand" data-panel="prov${i}">
        <div class="sub-title" title="用本機紀錄的 token 數乘上官方 API 單價估算，不是實際帳單；滑鼠移到數字上看分模型明細">花費（估算，移上看分模型）</div>
        ${spendRowsHtml(p)}${grantsHtml(p)}
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
    return HTML_TEMPLATE.replace("__DATA_JSON__", json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c"))


def main():
    want_html = "--html" in sys.argv
    out_path = None
    for i, arg in enumerate(sys.argv):
        if arg == "--html" and i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("-"):
            out_path = sys.argv[i + 1]

    data = run_splitrail()
    replace_analyzer_daily(data, "Antigravity CLI", *scan_antigravity_daily())
    replace_analyzer_daily(data, "Codex CLI", *scan_codex_daily())
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
