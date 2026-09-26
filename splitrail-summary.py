#!/usr/bin/env python3
"""一鍵彙總 Claude Code / Codex CLI / Antigravity CLI 三家用量。

跑 `splitrail.exe stats`，把 JSON 結果聚合成一張總覽表印出來。
費用是 splitrail 用本機 token 記錄反推的估算值，不是官方帳單金額，
只拿來看相對趨勢，對帳請以官方帳單為準。
"""
import json
import os
import subprocess
import sys
import datetime
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


def summarize(data, today):
    yesterday = today - datetime.timedelta(days=1)
    last7 = today - datetime.timedelta(days=7)
    last30 = today - datetime.timedelta(days=30)
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


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<title>AI CLI 用量 Cost 總覽</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0; padding: 24px; min-height: 100vh; box-sizing: border-box;
    background: #1B1D23; color: #E7E9EE;
    font-family: -apple-system, "Segoe UI", "Microsoft JhengHei", sans-serif;
    display: flex; align-items: center; justify-content: center;
  }
  .card {
    width: 360px; background: #22242B; border-radius: 20px; padding: 20px 22px 16px;
    box-shadow: 0 8px 28px rgba(0,0,0,.35);
  }
  .card h1 { font-size: 17px; margin: 0 0 14px; font-weight: 600; }
  .tabs { display: flex; background: #2C2F38; border-radius: 12px; padding: 3px; margin-bottom: 18px; }
  .tab {
    flex: 1; text-align: center; padding: 7px 0; border-radius: 10px; font-size: 13px;
    color: #9AA0AC; cursor: pointer; user-select: none; transition: background .15s, color .15s;
  }
  .tab.active { background: #E7E9EE; color: #1B1D23; font-weight: 600; }
  .donut-row { display: flex; align-items: center; gap: 22px; margin-bottom: 6px; }
  .donut-wrap { position: relative; width: 150px; height: 150px; flex: none; }
  .donut-wrap svg { transform: rotate(-90deg); }
  .donut-center {
    position: absolute; inset: 0; display: flex; flex-direction: column;
    align-items: center; justify-content: center; text-align: center;
  }
  .donut-center .amount { font-size: 22px; font-weight: 700; }
  .donut-center .unit { font-size: 11px; color: #9AA0AC; margin-top: 2px; }
  .legend { flex: 1; display: flex; flex-direction: column; gap: 10px; }
  .legend-item { display: flex; align-items: center; font-size: 13px; }
  .legend-dot { width: 9px; height: 9px; border-radius: 50%; margin-right: 8px; flex: none; }
  .legend-name { flex: 1; color: #C7CBD4; }
  .legend-amount { font-weight: 600; }
  .empty-note { font-size: 12px; color: #9AA0AC; text-align: center; padding: 20px 0; }
  .section-title {
    font-size: 12px; color: #9AA0AC; margin: 18px 0 10px; padding-top: 14px;
    border-top: 1px solid #33363F;
  }
  .token-list { display: flex; flex-direction: column; gap: 10px; }
  .token-item { font-size: 12px; }
  .token-head { display: flex; justify-content: space-between; margin-bottom: 4px; }
  .token-name { display: flex; align-items: center; color: #C7CBD4; }
  .token-nums { color: #E7E9EE; font-weight: 600; }
  .token-cached { color: #6C7079; font-weight: 400; margin-left: 6px; }
  .token-bar-track { background: #2C2F38; border-radius: 6px; height: 6px; overflow: hidden; }
  .token-bar-fill { height: 100%; border-radius: 6px; }
  .footer {
    margin-top: 14px; padding-top: 10px; border-top: 1px solid #33363F;
    display: flex; justify-content: space-between; font-size: 11px; color: #6C7079;
  }
</style>
</head>
<body>
  <div class="card">
    <h1>Cost（splitrail 估算，非官方帳單）</h1>
    <div class="tabs" id="tabs"></div>
    <div class="donut-row">
      <div class="donut-wrap">
        <svg width="150" height="150" viewBox="0 0 150 150">
          <circle cx="75" cy="75" r="62" fill="none" stroke="#33363F" stroke-width="16"></circle>
          <g id="arcs"></g>
        </svg>
        <div class="donut-center">
          <div class="amount" id="centerAmount">$0</div>
          <div class="unit">dollars</div>
        </div>
      </div>
      <div class="legend" id="legend"></div>
    </div>
    <div class="empty-note" id="emptyNote" style="display:none;">這個區間沒有用量資料</div>
    <div class="section-title">Token 用量（input / output，灰字為 cached）</div>
    <div class="token-list" id="tokenList"></div>
    <div class="footer">
      <span id="generatedAt"></span>
      <span>splitrail-summary --html</span>
    </div>
  </div>

<script>
const DATA = __DATA_JSON__;
const PERIODS = [
  { key: "today", label: "Today" },
  { key: "yesterday", label: "Yesterday" },
  { key: "last30", label: "30 Days" },
];
let current = "last30";

const tabsEl = document.getElementById("tabs");
PERIODS.forEach(p => {
  const el = document.createElement("div");
  el.className = "tab" + (p.key === current ? " active" : "");
  el.textContent = p.label;
  el.dataset.key = p.key;
  el.onclick = () => { current = p.key; render(); };
  tabsEl.appendChild(el);
});

const CIRC = 2 * Math.PI * 62;

function fmt(n) {
  return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtTokens(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return n.toLocaleString("en-US");
}

function render() {
  [...tabsEl.children].forEach(el => el.classList.toggle("active", el.dataset.key === current));

  const rows = DATA.tools
    .map(t => ({ ...t, value: t[current] }))
    .filter(t => t.value > 0)
    .sort((a, b) => b.value - a.value);

  const total = rows.reduce((s, t) => s + t.value, 0);
  document.getElementById("centerAmount").textContent =
    total >= 1000 ? "$" + (total / 1000).toFixed(1) + "K" : fmt(total);

  const arcsEl = document.getElementById("arcs");
  arcsEl.innerHTML = "";
  const legendEl = document.getElementById("legend");
  legendEl.innerHTML = "";
  document.getElementById("emptyNote").style.display = rows.length ? "none" : "block";

  let offset = 0;
  rows.forEach(t => {
    const frac = total > 0 ? t.value / total : 0;
    const len = frac * CIRC;
    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("cx", "75");
    circle.setAttribute("cy", "75");
    circle.setAttribute("r", "62");
    circle.setAttribute("fill", "none");
    circle.setAttribute("stroke", t.color);
    circle.setAttribute("stroke-width", "16");
    circle.setAttribute("stroke-dasharray", `${len} ${CIRC - len}`);
    circle.setAttribute("stroke-dashoffset", (-offset).toString());
    arcsEl.appendChild(circle);
    offset += len;

    const item = document.createElement("div");
    item.className = "legend-item";
    item.innerHTML = `<span class="legend-dot" style="background:${t.color}"></span>
      <span class="legend-name">${t.name}</span>
      <span class="legend-amount">${fmt(t.value)}</span>`;
    legendEl.appendChild(item);
  });

  renderTokens();
}

function renderTokens() {
  const tokenListEl = document.getElementById("tokenList");
  tokenListEl.innerHTML = "";

  const entries = DATA.tools.map(t => {
    const [inTok, outTok, cachedTok] = t.tokens[current];
    return { ...t, inTok, outTok, cachedTok, total: inTok + outTok };
  }).filter(t => t.total > 0 || t.cachedTok > 0);

  const maxTotal = Math.max(1, ...entries.map(t => t.total));

  entries.sort((a, b) => b.total - a.total).forEach(t => {
    const frac = t.total / maxTotal;
    const item = document.createElement("div");
    item.className = "token-item";
    item.innerHTML = `
      <div class="token-head">
        <span class="token-name"><span class="legend-dot" style="background:${t.color}"></span>${t.name}</span>
        <span class="token-nums">${fmtTokens(t.inTok)} / ${fmtTokens(t.outTok)}<span class="token-cached">cached ${fmtTokens(t.cachedTok)}</span></span>
      </div>
      <div class="token-bar-track"><div class="token-bar-fill" style="width:${(frac * 100).toFixed(1)}%;background:${t.color}"></div></div>
    `;
    tokenListEl.appendChild(item);
  });
}

document.getElementById("generatedAt").textContent = "更新於 " + DATA.generated_at;
render();
</script>
</body>
</html>
"""


def build_html(rows, today):
    tools = [{
        "name": r["name"],
        "color": r["color"],
        "today": round(r["today_cost"], 2),
        "yesterday": round(r["yesterday_cost"], 2),
        "last30": round(r["last30_cost"], 2),
        "tokens": {
            "today": r["period_tokens"]["today"],
            "yesterday": r["period_tokens"]["yesterday"],
            "last30": r["period_tokens"]["last30"],
        },
    } for r in rows]
    payload = {
        "generated_at": today.isoformat(),
        "tools": tools,
    }
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
        html = build_html(rows, today)
        path = out_path or os.path.join(SCRIPT_DIR, "splitrail-dashboard.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\n已產生視覺化頁面：{path}")
        if "--no-open" not in sys.argv:
            webbrowser.open("file:///" + path.replace("\\", "/"))


if __name__ == "__main__":
    main()
