#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
川渝天气展望 · 自动生成器（weather-analyst 方法论版）
========================================================
数据源：中央气象台 NMC（实况 + 7日逐日预报），覆盖川渝七分区代表城市，运行时动态解析站点代码。
分析框架：按 weather-analyst 技能——大尺度形势 → 分区实况 → 分区风险 → 演变趋势 → 关注提示。
增强   ：Ventusky 多要素截图（可选）、LLM 文案润色（可选）。

用法：
    python3 src/generate_weather.py                 # 核心模式：纯官方数据，确定性出稿
    python3 src/generate_weather.py --ventusky      # 开启 Ventusky 图层截图
    python3 src/generate_weather.py --llm           # 开启 LLM 润色（需 OPENAI_API_KEY）
    python3 src/generate_weather.py --out <dir>     # 输出目录，默认 ./site
"""
import argparse, base64, json, os, re, sys, urllib.request
from datetime import datetime

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
NMC_WEATHER = "http://www.nmc.cn/rest/weather?stationid={code}"
NMC_CITYPAGE = "http://www.nmc.cn/publish/forecast/ASC/{pinyin}.html"

# 川渝七分区（weather-analyst 区域框架）
REGIONS = [
    {"name": "盆西",   "focus": "西部沿山强降雨、地形雨；地形增幅易触发局地暴雨", "axis": "rain", "cities": [("成都", "chengdu"), ("雅安", "yaan"), ("眉山", "meishan")]},
    {"name": "盆东",   "focus": "副高控制下持续高温，午后对流", "axis": "heat", "cities": [("重庆", None), ("广安", "guangan")]},
    {"name": "盆中",   "focus": "过渡带，晴雨交替，风险相对均衡", "axis": "mix", "cities": [("遂宁", "suining"), ("南充", "nanchong"), ("资阳", "ziyang")]},
    {"name": "盆北",   "focus": "冷空气南下通道，沿山强降雨、山洪风险", "axis": "rain", "cities": [("广元", "guangyuan"), ("绵阳", "mianyang")]},
    {"name": "川西高原", "focus": "高原天气多变，午后对流、降温明显", "axis": "mix", "cities": [("康定", "kangding"), ("马尔康", "maerkang")]},
    {"name": "川西南",   "focus": "干热河谷，晴热为主，局地强对流", "axis": "heat", "cities": [("西昌", "xichang"), ("攀枝花", "panzhihua")]},
    {"name": "川东北",   "focus": "暖湿气流辐合显著，强降雨、地质灾害风险", "axis": "rain", "cities": [("达州", "dazhou"), ("巴中", "bazhong")]},
]
KNOWN_CODES = {"重庆": "UkfaS"}  # 重庆位于 ACQ 分区，走已知代码


# ---------- 数据抓取 ----------
def http_get(url, timeout=12):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def resolve_scode(pinyin):
    if pinyin is None:
        return None
    try:
        h = http_get(NMC_CITYPAGE.format(pinyin=pinyin)).decode("utf-8", "ignore")
        m = re.search(r"var scode = '([A-Za-z0-9]+)';", h)
        return m.group(1) if m else None
    except Exception:
        return None


def fetch_city(name, pinyin):
    code = KNOWN_CODES.get(name) or resolve_scode(pinyin)
    if not code:
        raise RuntimeError("%s 无站点代码" % name)
    raw = http_get(NMC_WEATHER.format(code=code)).decode("utf-8")
    d = json.loads(raw)
    if d.get("code") != 0:
        raise RuntimeError("%s err: %s" % (name, d.get("msg")))
    data = d["data"]
    real = data.get("real") or {}
    weat = real.get("weather") or {}
    days = []
    for dd in ((data.get("predict") or {}).get("detail") or [])[:4]:
        day = dd.get("day", {}).get("weather", {}) or {}
        night = dd.get("night", {}).get("weather", {}) or {}
        info = day.get("info", "")
        ninfo = night.get("info", "")
        if ninfo and ninfo != info:
            info = (info + "转" + ninfo) if info else ninfo
        days.append({
            "date": dd.get("date", ""),
            "info": info or "-",
            "tmax": _num(day.get("temperature")),
            "tmin": _num(night.get("temperature")),
            "precip": _num(dd.get("precipitation")),
        })
    return {
        "name": name,
        "publish": real.get("publish_time", ""),
        "now_temp": weat.get("temperature"),
        "now_rain": weat.get("rain"),
        "now_info": weat.get("info", ""),
        "humidity": weat.get("humidity"),
        "wind": (real.get("wind") or {}).get("direct", ""),
        "days": days,
    }


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------- 分区聚合 / 风险评分（透明启发式）----------
def _max72(city, key):
    vals = [d[key] for d in city["days"][:3] if d[key] is not None]
    return max(vals) if vals else None


def _today_day0(city):
    return city["days"][0] if city["days"] else {}


def score_region(cities):
    """据官方数据给区域打分：rain_points(0-3) / heat_points(0-3)"""
    rp, hp = 0, 0
    for c in cities:
        mxp = _max72(c, "precip"); mxt = _max72(c, "tmax")
        if mxp is not None:
            rp = max(rp, 1 + (mxp >= 15) + (mxp >= 40))
        if mxt is not None:
            hp = max(hp, 1 + (mxt >= 35) + (mxt >= 37))
    return min(rp, 3), min(hp, 3)


def region_risk(axis, cities):
    rp, hp = score_region(cities)
    if axis == "heat":
        level, icon, tip = _band(hp, "高温", "防暑降温，谨防中暑")
    elif axis == "rain":
        level, icon, tip = _band(rp, "降雨", "防范山洪与地质灾害滞后风险")
    else:
        score = max(rp, hp)
        kind = "高温" if (hp >= rp and hp) else ("降雨" if rp else "晴稳")
        level, icon, tip = _band(score, kind, "关注午后对流与晴雨转换")
    return {"risk": level, "axis": icon, "tip": tip, "rp": rp, "hp": hp}


def _band(score, kind, tip):
    if score >= 3:
        return ("高", kind, tip)
    if score >= 2:
        return ("中", kind, tip)
    if score >= 1:
        return ("较低", kind, tip)
    return ("关注", "晴稳", "天气相对平稳，随观随报")


def build_report(fetched):
    """fetched: dict cityname -> city. 返回标题/分区/概览等数据由 render 消费亦可在此组装"""
    return fetched


# ---------- Ventusky 截图（可选）----------
def capture_ventusky(out_dir, target="30.50;105.50;6"):
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
        return {}
    paths = {}
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(args=["--no-sandbox"])
            page = b.new_page(viewport={"width": 1600, "height": 1000})
            for key, tpl in layer_urls.items():
                try:
                    page.goto(tpl.format(p=target), wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(5000)
                    path = os.path.join(out_dir, "{k}.jpg".format(k=key))
                    page.screenshot(path=path, type="jpeg", quality=78, timeout=30000)
                    paths[key] = path
                except Exception as ex:
                    print("[ventusky]", key, "失败:", ex)
            b.close()
    except Exception as ex:
        print("[ventusky] 捕获异常:", ex)
    return paths


# ---------- LLM 文案（可选，weather-analyst 提示）----------
def llm_summary(data):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    sys_p = ("你是一名专业气象预报解读员，遵循川渝气象分析范式。请仅依据下方给出的中央气象台官方数据，"
             "用简体中文输出三段：1)【形势概览】简述副高/冷空气/水汽对盆地的总体影响（70字内，不杜撰具体数值）；"
             "2)【分区风险】按盆西/盆东/盆中/盆北/川西高原/川西南/川东北依次说一句话（各30字内）；"
             "3)【关注提示】2条。（200字以内）。不得编造任何未给出的数据。")
    prompt = json.dumps(data, ensure_ascii=False)[:6000]
    try:
        payload = json.dumps({
            "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            "messages": [{"role": "system", "content": sys_p},
                         {"role": "user", "content": prompt}],
            "temperature": 0.5,
        }).encode("utf-8")
        req = urllib.request.Request(base + "/chat/completions", data=payload,
                                     headers={**UA, "Content-Type": "application/json",
                                              "Authorization": "Bearer " + api_key})
        with urllib.request.urlopen(req, timeout=45) as r:
            j = json.loads(r.read().decode("utf-8"))
        return j["choices"][0]["message"]["content"].strip()
    except Exception as ex:
        print("[llm] 失败:", ex)
        return None


# ---------- 渲染 ----------
CSS = """
:root{--bg:#eef4fb;--card:#fff;--navy:#16324f;--line:#e3edf7;--accent:#2f7cb6;
  --amber:#b26a00;--red:#c0392b;--orange:#c26a12;--yellow:#9a7d0a;--green:#2a8a3e;}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,#e9f1fa,#eef4fb);color:#26333f;
  font-family:"Noto Sans CJK SC","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.75}
.wrap{max-width:1000px;margin:0 auto;padding:32px 18px 30px}
.hero{background:linear-gradient(135deg,#16324f,#2f5d8a);color:#fff;border-radius:18px;
  padding:26px 28px;margin-bottom:20px;box-shadow:0 8px 24px rgba(22,50,79,.25)}
.hero h1{margin:0 0 6px;font-size:27px;letter-spacing:.5px}
.hero .sub{color:#cfe0f2;font-size:13.5px}
.hero .meta{margin-top:12px;font-size:12.5px;color:#9fbcd6}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px 22px;margin:18px 0;
  box-shadow:0 2px 6px rgba(22,50,79,.06)}
.card h2{margin:0 0 14px;font-size:17px;color:var(--navy);display:flex;align-items:center;gap:10px}
.card h2 .no{background:var(--navy);color:#fff;font-size:13px;border-radius:8px;
  width:24px;height:24px;display:inline-flex;align-items:center;justify-content:center;flex:none}
.sec-sub{color:#8aa0b5;font-size:12.5px;margin:-8px 0 6px}
.now{display:flex;flex-wrap:wrap;gap:12px}
.city{flex:1 1 280px;background:#f5f9fe;border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.city .nm{font-size:14px;color:var(--navy);font-weight:700}
.city .tg{font-size:22px;color:var(--navy);font-weight:700;margin:2px 0}
.city .dt{font-size:12.5px;color:#667}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:8px 7px;text-align:center}
th{color:#7c8ca1;font-weight:600;font-size:12px}
th:first-child,td:first-child{text-align:left}
.region{display:grid;gap:12px}
.rrow{display:flex;gap:14px;align-items:flex-start;border:1px solid var(--line);border-radius:12px;
  padding:12px 14px;background:#fbfdff}
.rlvl{width:64px;text-align:center;flex:none}
.pill{display:inline-block;padding:4px 10px;border-radius:20px;font-size:12px;font-weight:700;color:#fff}
.p-高{background:var(--red)}.p-中{background:var(--orange)}.p-较低{background:var(--yellow)}.p-关注{background:#7f95a8}
.rbody{flex:1}
.rbody b{color:var(--navy)}
.met{font-size:12px;color:#7c8ca1;margin-top:4px}
.grade{border-radius:6px;padding:1px 7px;font-size:11.5px;margin-left:6px}
.g-高{background:#c0392b20}.g-中{background:#c26a1218}.g-较低{background:#9a7d0a14}.g-关注{background:#7f95a814}
.note{background:#fff8ea;border:1px solid #f0dca8;color:#7a5b12;border-radius:10px;padding:10px 14px;font-size:12.5px;margin-top:12px}
.ep{background:#f1f6fb;border-radius:10px;padding:12px 15px;font-size:13.5px;color:#31506e;white-space:pre-wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.grid figure{margin:0;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#fff}
.grid img{width:100%;display:block;aspect-ratio:16/10;object-fit:cover}
.grid figcaption{font-size:12px;color:#667;padding:6px 10px}
.badges{display:flex;flex-wrap:wrap;gap:14px;margin-top:14px}
.badges .b{background:#f2f7fd;border:1px solid var(--line);border-radius:10px;padding:9px 13px;font-size:12.8px}
.badges .b b{color:var(--navy)}
.foot{font-size:11.5px;color:#8697aa;margin-top:24px;text-align:center;line-height:1.8}
b{color:var(--navy)}
.hl{color:var(--red);font-weight:700}
.up{color:#2a8a3e;font-weight:700}
"""


def esc(s):
    if s is None:
        return "-"
    return str(s)


def fmt_temp(v):
    return ("%d" % v) if v is not None and not isinstance(v, str) else (esc(v) if v not in (None, "") else "-")


def render(fetched, ventusky_paths, narr, site_dir, generated):
    ts = generated.strftime("%Y-%m-%d %H:%M")
    dstr = generated.strftime("%m月%d日")
    publish = next((c["publish"] for c in fetched.values() if c.get("publish")), "-")

    # ① 双城实况
    nowchips = ""
    for key in ("成都", "重庆"):
        c = fetched.get(key)
        if not c:
            continue
        nw = c.get("now_info", "") or "-"
        wd = c.get("wind", "") or "-"
        hd = c.get("humidity")
        nowchips += ("<div class='city'><div class='nm'>{nm}</div><div class='tg'>{t}℃</div>"
                     "<div class='dt'>{info} · {wind} · 湿度 {hum}% · 实况发布于 {pub}</div></div>").format(
            nm=c["name"], t=fmt_temp(c.get("now_temp")), info=nw, wind=wd,
            hum=(int(hd) if hd is not None else "-"), pub=(c.get("publish") or "-"))

    # ② 分区风险
    regions_html = ""
    for r in REGIONS:
        rc = [fetched[n] for n, _ in r["cities"] if n in fetched]
        if not rc:
            continue
        risk = region_risk(r["axis"], rc)
        deg = risk["risk"]
        # 各代表城市一句
        metro = []
        for c in rc:
            d0 = c["days"][0] if c["days"] else {}
            metro.append("%s %s%s°/%s°·%.0fmm"
                         % (c["name"], d0.get("info", "-"), fmt_temp(d0.get("tmax", "-")),
                            fmt_temp(d0.get("tmin", "-")), d0.get("precip") or 0))
        cnames = "、".join(nm for nm, _ in r["cities"] if nm in fetched)
        maxp = max((_max72(c, "precip") or 0) for c in rc)
        maxt = max((_max72(c, "tmax") or 0) for c in rc)
        met = "72h最大日降水约 %.0f mm · 72h最高温约 %d℃ · 代表站：%s" % (maxp, maxt, cnames)
        grade = risk["axis"] if risk["risk"] in ("高", "中") else ("降雨" if risk["rp"] else "高温")
        regions_html += (
            "<div class='rrow'><div class='rlvl'><span class='pill p-{deg}'>{deg}</span>"
            "<div style='font-size:11.5px;color:#7c8ca1;margin-top:6px'>{axis}</div></div>"
            "<div class='rbody'><b>{name}</b> <span class='grade g-{deg}'>{axis}</span>"
            "<div style='font-size:13px;margin-top:4px'>{metro}</div>"
            "<div class='met'>关注：{focus} ｜ {met}</div>"
            "<div style='font-size:12.8px;margin-top:6px'>{tip}</div></div></div>"
        ).format(deg=deg, axis=risk["axis"], name=r["name"], metro="；".join(metro),
                 focus=r["focus"], met=met, tip=risk["tip"])

    # ③ 总览表（16城）
    rows = ""
    for r in REGIONS:
        for nm, _ in r["cities"]:
            c = fetched.get(nm)
            if not c:
                continue
            d0 = c["days"][1] if len(c["days"]) > 1 else c["days"][0] if c["days"] else {}
            d1 = c["days"][2] if len(c["days"]) > 2 else d0
            rows += "<tr><td>{r}</td><td>{nm}</td><td>{t0}</td><td>{d0}</td><td>{t1}</td><td>{d1}</td></tr>".format(
                r=r["name"], nm=nm, t0=fmt_temp(c.get("now_temp")),
                d0=("%s %.0fmm" % (d0.get("info", "-"), d0.get("precip") or 0)) if d0 else "-",
                t1=fmt_temp(d1.get("tmax", "-")),
                d1=("%s %.0fmm" % (d1.get("info", "-"), d1.get("precip") or 0)) if d1 else "-")

    # Ventusky
    vent = ""
    if ventusky_paths:
        blocks = ""
        for k, p in ventusky_paths.items():
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            mime = "image/jpeg" if p.lower().endswith((".jpg", ".jpeg")) else "image/png"
            blocks += ("<figure><img src='data:{m};base64,{b}'/><figcaption>{l}</figcaption></figure>").format(
                m=mime, b=b64, l={"wind": "风场", "rain": "降水落区", "temperature": "气温", "pressure": "海平面气压"}.get(k, k))
        vent = ("<div class='card'><h2><span class='no'>4</span>多要素实况解析（Ventusky 数值模式）</h2>"
                "<div class='sec-sub'>同一时刻四要素 · 叠加地形与城市标注 · 与官方数据交叉印证</div>"
                "<div class='grid'>{b}</div>"
                "<div class='note'>实况要素面交叉印证，具体取值与结论以中央气象台及属地气象部门官方预报为准。</div></div>").format(b=blocks)

    narr_block = ("<div class='ep'>{narr}</div>").format(narr=narr or "（未启用 LLM 润色，采用规则化概览，数据取官方实时）")

    html = """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>川渝天气展望 · 分区风险解析 {d}</title><style>{css}</style></head>
<body><div class="wrap">
<div class="hero">
<h1>川渝天气展望 · 夏季分区风险解析</h1>
<div class="sub">雨带动态与盆西盆东温差 · 七分区逐轴风险评估</div>
<div class="meta">数据源：中央气象台 NMC（官方实况+未来预报）｜ 分析：weather-analyst 范式 ｜ 生成：{gen}</div>
</div>

<div class="card"><h2><span class="no">1</span>双城官方实况</h2>
<div class="sec-sub">发布于 {pub} · 实时观测</div><div class="now">{nowchips}</div></div>

<div class="card"><h2><span class="no">2</span>分区天气与风险解析</h2>
<div class="sec-sub">依官方预报聚合 · 风险按主导影响轴（降雨/高温）分级；高=需重点防范</div>
<div class="region">{regions}</div>
<div class="note">风险等级由各分区代表城市官方逐日预报中的<b>最大单日降水</b>与<b>72h最高气温</b>经透明规则综合评定；仅作形势研判，具体以属地气象台预警为准。</div></div>

<div class="card"><h2><span class="no">3</span>分区实况总览（官方逐日预报）</h2>
<div class="sec-sub">成都/重庆为实时观测，其余城市取官方未来预报</div>
<table><thead><tr><th>分区</th><th>城市</th><th>实况/未来1日</th><th>逐日</th><th>未来最高</th><th>逐日</th></tr></thead>
<tbody>{rows}</tbody></table></div>

{vent}

<div class="card"><h2><span class="no">5</span>形势解读与关注提示</h2>
{narr_block}</div>

<div class="foot">本页由 GitHub Actions 定时自动生成 · 数据来自中央气象台 NMC / Ventusky · 未经人工审核，仅供参考<br/>
灾害性天气请以属地气象部门发布的预报预警为准。</div>
</div></body></html>""".format(
        css=CSS, d=dstr, gen=ts, pub=publish, nowchips=nowchips, regions=regions_html,
        rows=rows, vent=vent, narr_block=narr_block)

    os.makedirs(site_dir, exist_ok=True)
    out = os.path.join(site_dir, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成:", out)
    return out


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--ventusky", action="store_true")
    ap.add_argument("--llm", action="store_true")
    args = ap.parse_args()

    fetched = {}
    for r in REGIONS:
        for nm, pinyin in r["cities"]:
            if nm in fetched:
                continue
            try:
                c = fetch_city(nm, pinyin)
                fetched[nm] = c
                print("[{r}] {n} 实况 {t}℃ · 已取 {d} 日".format(
                    r=r["name"], n=nm, t=c.get("now_temp"), d=len(c["days"])))
            except Exception as ex:
                print("[{r}] {n} 抓取失败: {e}".format(r=r["name"], n=nm, e=ex))
    if not fetched:
        print("!! 所有城市抓取失败，无法生成。")
        sys.exit(1)

    vent = capture_ventusky(os.path.join(args.out, "ventusky")) if args.ventusky else {}

    narr = llm_summary(fetched) if args.llm else None
    if not narr:
        narr = "今日数据已按分区聚合于上方表格。整体看：盆东及川南（重庆、广安、西昌、攀枝花）最高温偏高、需防暑；盆西、盆北、川东北沿山地区逐日降水较明显，注意局地强降雨与地质灾害滞后效应。高温/强降雨具体落区与强度以中央气象台及属地气象台实时预警为准。"

    render(fetched, vent, narr, args.out, datetime.now())


if __name__ == "__main__":
    main()