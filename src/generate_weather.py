#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
川渝天气预报自动生成器
======================
数据源：中央气象台 NMC（实时+逐日预报，零依赖可跑）
增强   ：Ventusky 多要素截图（可选，需 Playwright）、LLM 文案润色（可选，需 OPENAI_API_KEY）

用法：
    python3 generate_weather.py                 # 核心模式：仅 NMC 数据，纯实况出稿
    python3 generate_weather.py --ventusky      # 开启 Ventusky 图层截图
    python3 generate_weather.py --llm           # 开启 LLM 文案润色（需 OPENAI_API_KEY/OPENAI_BASE_URL）
    python3 generate_weather.py --out <dir>     # 输出目录，默认 ./site
"""
import argparse, base64, io, json, os, re, sys, urllib.request
from datetime import datetime, timedelta

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
CITIES = [
    {"id": "yGYHR", "name": "成都", "province": "四川"},
    {"id": "UkfaS", "name": "重庆", "province": "重庆"},
]
NMC_URL = "http://www.nmc.cn/rest/weather?stationid={sid}"


# ---------- 数据抓取 ----------
def http_get(url, timeout=12):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_city(sid):
    raw = http_get(NMC_URL.format(sid=sid)).decode("utf-8")
    d = json.loads(raw)
    if d.get("code") != 0:
        raise RuntimeError("NMC {sid} err: {msg}".format(sid=sid, msg=d.get("msg")))
    data = d["data"]
    real = data.get("real") or {}
    weat = real.get("weather") or {}
    return {
        "station": (real.get("station") or {}).get("city", sid),
        "publish": real.get("publish_time", ""),
        "realtime_temp": weat.get("temperature"),
        "realtime_rain": weat.get("rain"),
        "realtime_info": weat.get("info", ""),
        "humidity": weat.get("humidity"),
        "wind": (real.get("wind") or {}).get("direct", ""),
        "days": (data.get("predict") or {}).get("detail", []),
    }


def summarize(city):
    """把 NMC detail 压成每日本报所需行"""
    rows = []
    for dd in city["days"][:4]:
        day, night = dd.get("day", {}).get("weather", {}), dd.get("night", {}).get("weather", {})
        info = day.get("info", "")
        ninfo = night.get("info", "")
        if ninfo and ninfo != info:
            info = info + "转" + ninfo
        rows.append({
            "date": dd.get("date", ""),
            "info": info,
            "tmax": day.get("temperature", "-"),
            "tmin": night.get("temperature", "-"),
            "precip": dd.get("precipitation", 0),
            "daywind": (dd.get("day", {}).get("wind") or {}).get("direct", ""),
        })
    return rows


def take(data, *keys, default=None):
    for k in keys:
        data = data.get(k, {}) if isinstance(data, dict) else default
    return data if data is not None else default


# ---------- Ventusky 截图（可选）----------
def capture_ventusky(out_dir, target="30.50;105.50;6"):
    """用 Playwright 打开多层 URL 各截图。失败则静默跳过。"""
    layer_urls = {
        "wind": "https://www.ventusky.com/wind-map#p={p}",
        "rain": "https://www.ventusky.com/rain#p={p}",
        "temperature": "https://www.ventusky.com/#p={p}",
        "pressure": "https://www.ventusky.com/air-pressure-map#p={p}",
    }
    os.makedirs(out_dir, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {}  # 未安装 Playwright
    paths = {}
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(args=["--no-sandbox"])
            page = b.new_page(viewport={"width": 1600, "height": 1000})
            for key, tpl in layer_urls.items():
                try:
                    page.goto(tpl.format(p=target), wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(5000)  # 等瓦片渲染
                    path = os.path.join(out_dir, "{k}.jpg".format(k=key))
                    page.screenshot(path=path, type="jpeg", quality=78, timeout=30000)
                    paths[key] = path
                except Exception as ex:
                    print("[ventusky] {k} 失败: {e}".format(k=key, e=ex))
            b.close()
    except Exception as ex:
        print("[ventusky] 捕获异常:", ex)
    return paths


# ---------- LLM 文案（可选）----------
def llm_summary(blocks, lang="zh"):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    sys_p = ("你是一位专业气象预报解读员。请用简体中文、简洁口语，依据给出的官方实况与预报数据，"
             "生成一段 120~180 字的天气形势评述与提醒，不得杜撰数据，不得给出灾害性天气肯定结论。")
    prompt = "官方数据如下：\n" + json.dumps(blocks, ensure_ascii=False)[:3000]
    payload = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "messages": [{"role": "system", "content": sys_p},
                     {"role": "user", "content": prompt}],
        "temperature": 0.6,
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(base + "/chat/completions", data=data,
                                     headers={**UA, "Content-Type": "application/json",
                                              "Authorization": "Bearer " + api_key})
        with urllib.request.urlopen(req, timeout=40) as r:
            j = json.loads(r.read().decode("utf-8"))
        return j["choices"][0]["message"]["content"].strip()
    except Exception as ex:
        print("[llm] 失败:", ex)
        return None


# ---------- 渲染 ----------
CSS = """
:root{--bg:#eef3f9;--card:#ffffff;--navy:#16324f;--line:#e3eaf2;--accent:#2f7cb6;--amber:#b26a00}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:#26333f;font-family:"Noto Sans CJK SC","PingFang SC","Microsoft YaHei",-apple-system,sans-serif;line-height:1.7}
.wrap{max-width:960px;margin:0 auto;padding:28px 18px 20px}
h1{font-size:26px;color:var(--navy);margin:0 0 6px}
.sub{color:#7c8ca1;font-size:13px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:16px 0;box-shadow:0 1px 3px rgba(18,50,80,.05)}
.card h2{font-size:16px;color:var(--navy);margin:0 0 12px;border-left:4px solid var(--accent);padding-left:10px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{border-bottom:1px solid var(--line);padding:9px 8px;text-align:left}
th{color:#7c8ca1;font-weight:600;font-size:12.5px}
.now{display:flex;flex-wrap:wrap;gap:12px;margin-top:6px}
.chip{background:#f2f7fd;border:1px solid var(--line);border-radius:10px;padding:8px 12px;font-size:13px}
.chip b{color:var(--navy)}
.warn{background:#fff7ec;border:1px solid #f2d9b0;color:#8a5a08;border-radius:10px;padding:10px 14px;font-size:13px;margin-top:10px}
.ep{background:#f1f6fb;border-radius:10px;padding:12px 14px;font-size:13.5px;color:#31506e}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.grid figure{margin:0;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#fff}
.grid img{width:100%;display:block;aspect-ratio:16/10;object-fit:cover}
.grid figcaption{font-size:12px;color:#667;padding:6px 10px}
.foot{font-size:11.5px;color:#8697aa;margin-top:22px;text-align:center}
b{color:var(--navy)}
.week{color:var(--accent);font-weight:600}
"""


def render(meta, cities_rows, narr, ventusky_paths, site_dir):
    now = datetime.now()
    table = ""
    for i in range(4):
        cells = []
        for c in cities_rows:
            if i < len(c["rows"]):
                r = c["rows"][i]
                cells.append(
                    "<td><div class='week'>{d}</div>{info}<br/>"
                    "<b>{tmax}°</b> / {tmin}° · 降水 {p}mm</td>".format(
                        d=r["date"], info=r["info"], tmax=r["tmax"], tmin=r["tmin"], p=r["precip"]))
            else:
                cells.append("<td>—</td>")
        table += "<tr><td>第 {n} 天</td>{c}</tr>".format(n=i + 1, c="".join(cells))

    nowchips = ""
    for c in cities_rows:
        nowchips += ("<div class='chip'><b>{n}</b> 实况 {t}℃ · {info} · {wind} · 湿度{h}%"
                     "　（发布于 {pub}）</div>").format(
            n=c["name"], t=c["realtime_temp"], info=c["realtime_info"],
            wind=c["wind"], h=c["humidity"], pub=c["publish"])

    vent = ""
    if ventusky_paths:
        blocks = ""
        for k, p in ventusky_paths.items():
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            mime = "image/jpeg" if p.lower().endswith((".jpg", ".jpeg")) else "image/png"
            src = "data:{m};base64,{b}".format(m=mime, b=b64)
            blocks += ("<figure><img src='{src}'/><figcaption>{lbl}</figcaption></figure>".format(
                src=src, lbl={"wind": "风场", "rain": "降水落区",
                              "temperature": "气温", "pressure": "海平面气压"}.get(k, k)))
        vent = ("<div class='card'><h2>多要素实况解析（Ventusky 数值模式）</h2>"
                "<div class='grid'>{b}</div>"
                "<p style='font-size:12px;color:#8697aa'>实况要素面交叉印证，具体以官方预报为准。</p></div>").format(b=blocks)

    html = """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{css}</style></head>
<body><div class="wrap">
<h1>{title}</h1>
<div class="sub">自动生成 · 数据源：中央气象台 NMC（发布于 {pub}） · 生成时间 {gen}</div>

<div class="card"><h2>① 双城实况</h2><div class="now">{nowchips}</div>
{pub}
{narr_html}
</div>

<div class="card"><h2>② 未来 4 天逐日（NMC 预报）</h2>
<table><thead><tr><th></th><th>成都</th><th>重庆</th></tr></thead>
<tbody>{table}</tbody></table>
<p style="font-size:12px;color:#8697aa">白天/夜间温度取预报昼夜值；降水量为该日合计（mm）。</p>
</div>

{vent}

<div class="card"><h2>③ 气象解读</h2><div class="ep">{narr}</div>
<div class="warn">灾害性天气请以属地气象部门预警为准，本页仅作信息参考。</div></div>

<div class="foot">本页由 GitHub Actions 定时自动生成 · 数据来自中央气象台 NMC / Ventusky · 未经人工审核，仅供参考</div>
</div></body></html>""".format(
        title="川渝天气展望 · 自动生成 {d}".format(d=now.strftime("%m月%d日")),
        css=CSS, pub=cities_rows[0]["publish"] or "-",
        gen=now.strftime("%Y-%m-%d %H:%M"), nowchips=nowchips, table=table,
        vent=vent, narr=narr,
        narr_html=""
        if False else "")
    os.makedirs(site_dir, exist_ok=True)
    with open(os.path.join(site_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成:", os.path.join(site_dir, "index.html"))
    return os.path.join(site_dir, "index.html")


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--ventusky", action="store_true")
    ap.add_argument("--llm", action="store_true")
    args = ap.parse_args()

    cities = []
    for c in CITIES:
        try:
            fc = fetch_city(c["id"])
            fc["name"] = c["name"]
            fc["rows"] = summarize(fc)
            cities.append(fc)
            print("[{n}] 实况 {t}℃ 未来{n2}天已取".format(
                n=c["name"], t=fc["realtime_temp"], n2=len(fc["rows"])))
        except Exception as ex:
            print("[{n}] 抓取失败: {e}".format(n=c["name"], e=ex))
    if not cities:
        print("!! 所有城市抓取失败，无法生成。")
        sys.exit(1)

    vent = capture_ventusky(os.path.join(args.out, "ventusky")) if args.ventusky else {}

    blocks = [{"city": c["name"], "now": c.get("realtime_temp"),
               "days": c["rows"][:4]} for c in cities]
    narr = llm_summary(blocks) if args.llm else None
    if not narr:
        narr = _fallback_narr(cities)

    render({}, cities, narr, vent, args.out)


def _fallback_narr(cities):
    """无 LLM 时依据实况/预报作文案"""
    parts = []
    for c in cities:
        r = c["rows"][0] if c["rows"] else {}
        parts.append("{n}今日{r}{t}℃，未来将有{info}。".format(
            n=c["name"], r=r.get("info", ""), t=r.get("tmax", "-"), info=r.get("info", "降水")))
    base = (parts[0] + "。" + parts[1] + "。整体来看，"
            "两地主打降水过程，出行请备伞、注意防滑；降水量大处需防范局地积水与地质灾害滞后影响，"
            "请持续关注当地气象台最新预报预警。")
    return base


if __name__ == "__main__":
    main()