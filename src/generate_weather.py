#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
川渝天气展望 · 分析报告版自动生成器（weather-analyst 方法论）
=============================================================
数据源：中央气象台 NMC（实况 + 7日逐日预报），覆盖川渝七分区代表城市。
分析框架：严格按 weather-analyst 技能 ——
    ① 大尺度形势概览（副高/冷空气/水汽对盆地影响）
    ② 双城实况
    ③ 分区×逐日×多维风险评估（降雨/高温/强对流，含72h演变趋势）
    ④ 重点天气过程
    ⑤ 分区逐日总览
    ⑥ 关注与提示

用法：
    python3 src/generate_weather.py                 # 核心模式：纯官方数据，确定性出稿
    python3 src/generate_weather.py --ventusky      # 开启 Ventusky 多要素截图
    python3 src/generate_weather.py --llm           # 开启 LLM 润色（需 OPENAI_API_KEY）
    python3 src/generate_weather.py --out <dir>     # 输出目录，默认 ./site
"""
import argparse, base64, json, os, re, sys, urllib.request
from datetime import datetime

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
NMC_WEATHER = "http://www.nmc.cn/rest/weather?stationid={code}"
NMC_CITYPAGE = "http://www.nmc.cn/publish/forecast/ASC/{pinyin}.html"

# 川渝七分区（weather-analyst 区域框架；axis 为"主导影响轴"，用于默认配色与排序）
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

# 代表城市经纬度（WGS84，用于地图标点）
CITY_COORDS = {
    "成都": (30.5728, 104.0668), "雅安": (29.9791, 103.0131), "眉山": (30.0489, 103.8317),
    "重庆": (29.5630, 106.5516), "广安": (30.4560, 106.6332),
    "遂宁": (30.5321, 105.5717), "南充": (30.8371, 106.1106), "资阳": (30.1289, 104.6359),
    "广元": (32.4354, 105.8434), "绵阳": (31.4731, 104.6798), "巴中": (31.8661, 106.7437),
    "康定": (29.9985, 101.9640), "马尔康": (31.9057, 102.2213),
    "西昌": (27.8945, 102.2585), "攀枝花": (26.5804, 101.7183),
    "达州": (31.2096, 107.4676),
}


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
        info = _cond(day.get("info"))
        ninfo = _cond(night.get("info"))
        if ninfo and ninfo != info:
            info = (info + "转" + ninfo) if info else ninfo
        days.append({
            "date": dd.get("date", ""),
            "info": info or "-",
            "tmax": _temp(day.get("temperature")),
            "tmin": _temp(night.get("temperature")),
            "precip": _precip(dd.get("precipitation")),
        })
    return {
        "name": name,
        "publish": real.get("publish_time", ""),
        "now_temp": _temp(weat.get("temperature")),
        "now_rain": weat.get("rain"),
        "now_info": _cond(weat.get("info", "")),
        "humidity": _hum(weat.get("humidity")),
        "wind": (real.get("wind") or {}).get("direct", ""),
        "days": days,
    }


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _temp(v):
    """气温清洗：丢弃 NMC 缺测哨兵值(9999)与越界数值，返回合理气温(±60℃)或 None。"""
    v = _num(v)
    if v is None:
        return None
    return v if -60 <= v <= 60 else None


def _precip(v):
    """降水清洗：丢弃哨兵值(9999)与超出合理范围的毫米数，返回合理值或 None。"""
    v = _num(v)
    if v is None or v < 0 or v > 800 or v == 9999:
        return None
    return v


def _hum(v):
    """湿度清洗：返回 0-100 的合理值，否则 None。"""
    v = _num(v)
    if v is None or v < 0 or v > 100:
        return None
    return v


def _cond(s):
    """天气现象清洗：NMC 对缺测天气编码同样返回 9999，视为空串。"""
    s = (s or "").strip()
    return "" if s in ("9999", "999", "0", "-") else s


# ---------- 分区聚合 / 多维风险引擎（透明白箱启发式）----------
CONVEC_WORDS = ("雷", "阵雨", "中雨", "大雨", "暴雨", "强对流")


def _day_dim(city, i):
    """单城单日三维风险：rp 降雨(0-3) / hp 高温(0-3) / cp 强对流(0-2)"""
    d = city["days"][i] if i < len(city["days"]) else {}
    p, t, info = d.get("precip"), d.get("tmax"), (d.get("info", "") or "")
    rp = 0
    if p is not None:
        rp = 1 + (p >= 15) + (p >= 40)
    hp = 0
    if t is not None:
        hp = 1 + (t >= 35) + (t >= 37)
    # 强对流：强雷雨显式词 / 高温下的大降水(午后热对流) / 大降水+降雨词
    cp = 2 if "雷" in info else (1 if any(w in info for w in ("阵雨", "中雨", "大雨", "暴雨", "强对流")) else 0)
    if p is not None and p >= 15 and t is not None and t >= 30:
        cp = max(cp, 1)
    if p is not None and p >= 40 and any(w in info for w in CONVEC_WORDS):
        cp = max(cp, 2)
    return {"rp": min(rp, 3), "hp": min(hp, 3), "cp": min(cp, 2),
            "info": info or "-", "precip": p, "tmax": t,
            "tmin": d.get("tmin"), "date": d.get("date", "")}


def region_analysis(cities):
    """分区×逐日(0-2)聚合 + 72h峰值 + 演变趋势"""
    days = []
    for i in range(3):
        r = h = c = 0
        for city in cities:
            dm = _day_dim(city, i)
            r = max(r, dm["rp"]); h = max(h, dm["hp"]); c = max(c, dm["cp"])
        days.append([r, h, c])
    r72 = max(d[0] for d in days); h72 = max(d[1] for d in days); c72 = max(d[2] for d in days)

    def _s(d):
        return max(d[0], d[1])
    # 趋势：粗细用 dot 表示，箭头方向表示增强/减弱
    s0, s1, s2 = (_s(days[0]), _s(days[1]), _s(days[2]))
    if s1 > s0:
        trend, trend_dir = "增强（明日风险抬升）", "↑"
    elif s1 < s0:
        trend, trend_dir = "减弱（今日为峰值）", "↓"
    else:
        trend_dir = "→"
        trend = "平稳（维持相同量级）"
    rpeaks = [i for i in range(3) if days[i][0] == r72 and r72]
    tpeaks = [i for i in range(3) if days[i][1] == h72 and h72]
    daynames = {0: "今", 1: "明", 2: "后"}
    r_peak = ("，降雨峰值在" + "、".join(daynames[i] for i in rpeaks)) if r72 else ""
    t_peak = ("，高温峰值在" + "、".join(daynames[i] for i in tpeaks)) if h72 else ""
    return {"days": days, "rp": r72, "hp": h72, "cp": c72,
            "s0": s0, "s1": s1, "s2": s2, "trend": trend, "trend_dir": trend_dir,
            "r_peak": r_peak, "t_peak": t_peak}


def region_risk(axis, analysis):
    """由三维风险合成区域定级。返回 dict：level / axis_label / dims / tip"""
    rp, hp, cp = analysis["rp"], analysis["hp"], analysis["cp"]
    dims = []
    if rp: dims.append(("降雨", rp, _lvl(rp)))
    if hp: dims.append(("高温", hp, _lvl(hp)))
    if cp: dims.append(("强对流", cp, _lvl(cp)))
    if not dims:
        dims = [("晴稳", 0, "关注")]
    # 主导轴定级：用"该轴分数"决定色彩，其余维度作补充
    if axis == "heat":
        score = hp
    elif axis == "rain":
        score = rp
    else:
        score = max(rp, hp)
    score = max(score, cp if cp >= 2 else 0)
    if score >= 3:
        level = "高"
    elif score >= 2:
        level = "中"
    elif score >= 1:
        level = "较低"
    else:
        level = "关注"
    tip = {3: "重点关注：防范强降雨/高温叠加影响，减少非必要户外活动",
           2: "需防范局地强降雨或高温，留意趋势变化",
           1: "量级较低，随观随报，关注午后对流"} .get(max(score, 0), "天气相对平稳，随观随报")
    if cp >= 2:
        tip = "警惕午后局地强对流（雷暴大风/短时强降水）与地质灾害滞后效应"
    return {"level": level, "score": score, "dims": dims, "tip": tip,
            "rp": rp, "hp": hp, "cp": cp}


def _lvl(score):
    return {3: "高", 2: "中", 1: "较低", 0: "关注"}.get(score, "关注")


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


# ---------- NMC 官方预报图 ----------
NMC_PRECIP_PAGE = "http://www.nmc.cn/publish/precipitation/1-day.html"
NMC_SAT_PAGE = "http://www.nmc.cn/publish/satellite.html"
# 全国降水量预报：SEVP_NMC_STFC_SFER_ER24_ACHN_L88_P9_{basetime12}{lead5}.JPG
_NMC_PRECIP_RE = re.compile(
    r"image\.nmc\.cn/product/\d{4}/\d{2}/\d{2}/STFC/medium/"
    r"SEVP_NMC_STFC_SFER_ER24_ACHN_L88_P9_(\d{12})(\d{5})\.JPG", re.I)
# FY4B 卫星云图（亮度温度,中国区域）
_NMC_SAT_RE = re.compile(
    r"image\.nmc\.cn/product/\d{4}/\d{2}/\d{2}/WXBL/medium/"
    r"SEVP_NSMC_WXBL_FY4B_ETCC_ACHN_LNO_PY_(\d{14})\.JPG", re.I)


def _newest_by_lead(page_html, regex, leads):
    """从产品页解析各时效(lead)最新基线下的预报图 URL 全量"""
    best = {}
    for m in regex.finditer(page_html):
        base, lead = m.group(1), m.group(2)
        if lead not in leads:
            continue
        cur = best.get(lead)
        if cur is None or base > cur[0]:
            best[lead] = "https://" + m.group(0)
    return best


def _dl_chart(url, path, max_w=860, q=68):
    """下载官方图，并用 PIL 降采样压缩以便内嵌；无 PIL 则原样保存"""
    data = http_get(url, timeout=30)
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = im.size
        if w > max_w:
            im = im.resize((max_w, int(h * max_w / w)), Image.LANCZOS)
        im.save(path, "JPEG", quality=q, optimize=True)
        return path
    except Exception:
        with open(path, "wb") as f:
            f.write(data)
        return path


def fetch_nmc_charts(out_dir):
    """抓取 NMC 官方预报图：降水量预报(24h/48h/72h) + FY4B 卫星云图。
    返回 {label: path}，全部失败返回 {}；异常均吞掉以便降级。"""
    os.makedirs(out_dir, exist_ok=True)
    order = [("02400", "24小时降水预报"), ("04800", "48小时降水预报"), ("07200", "72小时降水预报")]
    charts = {}
    try:
        precip_html = http_get(NMC_PRECIP_PAGE).decode("utf-8", "ignore")
        best = _newest_by_lead(precip_html, _NMC_PRECIP_RE, {l for l, _ in order})
        for lead, label in order:
            url = best.get(lead)
            if not url:
                continue
            p = _dl_chart(url, os.path.join(out_dir, "precip_%s.jpg" % lead))
            charts["降水 · %s" % label] = p
        if not charts:
            print("[nmc-img] 降水预报图抓取为空")
    except Exception as ex:
        print("[nmc-img] 降水预报图抓取失败:", ex)

    # 卫星云图只保留近 3 天内（避免产品页陈旧图）
    try:
        sat_html = http_get(NMC_SAT_PAGE).decode("utf-8", "ignore")
        m = _NMC_SAT_RE.search(sat_html)
        if m:
            base = m.group(1)  # YYYYMMDDHHMM
            ts = datetime.strptime(base, "%Y%m%d%H%M")
            if (datetime.now() - ts).days <= 3:
                p = _dl_chart("https://" + m.group(0), os.path.join(out_dir, "cloud.jpg"))
                charts["FY4B 卫星云图"] = p
    except Exception as ex:
        print("[nmc-img] 卫星云图抓取失败:", ex)
    return charts


# ---------- 官方预警信号（weather.cma.cn 中国气象局全国预警） ----------
ALARM_API = "https://weather.cma.cn/api/map/alarm?adcode={code}"
ALARM_ADCODES = [("51", "四川"), ("50", "重庆")]
ALARM_LEVELS = ["红色", "橙色", "黄色", "蓝色"]               # 级别由高到低
_ALARM_CATS = ["地质灾害气象", "道路结冰", "低温雨雪", "森林草原火险", "森林火险",
               "强对流云团", "雷雨大风", "沙尘暴", "台风", "暴雨大风", "暴雨", "暴雪",
               "寒潮", "大风", "冰雹", "雷电", "大雾", "高温", "干旱", "霜冻", "山洪",
               "强对流", "低温", "霾"]
ALARM_REGION_MAP = [   # 预警发布地区字符串 → 我们的分区（子串匹配）
    ("盆西", ["成都", "雅安", "眉山"]),
    ("盆东", ["重庆", "广安"]),
    ("盆中", ["遂宁", "南充", "资阳"]),
    ("盆北", ["广元", "绵阳", "巴中"]),
    ("川西高原", ["甘孜", "阿坝", "康定", "马尔康"]),
    ("川西南", ["凉山", "西昌", "攀枝花"]),
    ("川东北", ["达州", "巴中"]),
]


def _alarm_parse(item):
    text = (item.get("headline") or "") + " " + (item.get("title") or "")
    level = next((l for l in ALARM_LEVELS if l in text), "")
    cat = next((c for c in _ALARM_CATS if c in text), "其他")
    area = ((item.get("title") or "").split("发布")[0].strip()) or "-"
    return {"cat": cat, "level": level, "area": area,
            "desc": item.get("description") or "",
            "time": (item.get("effective") or "").replace("/", "-")}


def fetch_alarms():
    """抓取四川/重庆现行官方预警。
    返回 (uniq_alarms, 各省条数 dict)；任一失败均降级。"""
    alarms, cnt = [], {}
    for code, prov in ALARM_ADCODES:
        try:
            raw = json.loads(http_get(ALARM_API.format(code=code), timeout=15).decode("utf-8"))
            items = (raw or {}).get("data") or []
        except Exception as ex:
            print("[alarm] %s 抓取失败: %s" % (prov, ex))
            continue
        cnt[prov] = len(items)
        for it in items:
            a = _alarm_parse(it)
            a["prov"] = prov
            a["regions"] = [rg for rg, keys in ALARM_REGION_MAP
                            if any(k in a["area"] for k in keys) or
                            (rg == "盆东" and prov == "重庆")]
            alarms.append(a)
    # 去重：同类同级同地区只保留一条
    seen, uniq = set(), []
    for a in alarms:
        k = (a["cat"], a["level"], a["area"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(a)
    order = {l: i for i, l in enumerate(ALARM_LEVELS)}
    uniq.sort(key=lambda a: (order.get(a["level"], 9), a["prov"]))
    return uniq, cnt


def build_alarm_card(alarms, cnt, top=22):
    """把官方预警渲染成一张卡片 HTML；无预警或全失败返回空串。"""
    if not alarms:
        return ""
    n = len(alarms)
    sc = sum(1 for a in alarms if a["prov"] == "四川")
    cq = sum(1 for a in alarms if a["prov"] == "重庆")
    counts = {l: sum(1 for a in alarms if a["level"] == l) for l in ALARM_LEVELS}
    order = {l: i for i, l in enumerate(ALARM_LEVELS)}
    hi = next((l for l in ALARM_LEVELS if counts.get(l)), "")
    BZ = {"红色": "严重", "橙色": "较重", "黄色": "注意", "蓝色": "一般"}

    # 顶部横幅：突出"涉及本报告分区"的红/橙预警
    critical = [a for a in alarms
                if a["level"] in ("红色", "橙色") and a["regions"]]
    if critical:
        rgs = "、".join(sorted({r for a in critical for r in a["regions"]}))
        cats = "、".join(sorted({a["cat"] for a in critical}))
        banner = ("<span class='ab-icon'>⚠</span>"
                  "<div><b>本报告分区 {rgs} 现有多条{lv}预警</b>"
                  "<span>类型：{cats}｜请以属地气象台最新发布为准，红色/橙色预警区域避免高风险活动。</span></div>").format(
            rgs=rgs or "川渝", lv=next((l for l in ("红色", "橙色") if counts.get(l)), ""),
            cats=cats or "-")
    else:
        banner = ("<div><b>当前川渝最高预警级别：{lv}（{bz}）</b>"
                  "<span>生效预警共 {n} 条，红橙黄蓝按官方分级，请关注与您所在/前往地区相关条目。</span></div>").format(
            lv=hi or "无", bz=BZ.get(hi, "-"), n=n)

    # 分级别统计条
    stat_chips = "".join(
        "<span class='s-chip c-{l}'>{l} {c}</span>".format(l=l, c=counts[l])
        for l in ALARM_LEVELS if counts.get(l))
    stats = ("<span class='s-total'>四川 {sc} 条 · 重庆 {cq} 条 · 共 {n} 条</span>{chips}").format(
        sc=sc, cq=cq, n=n, chips=stat_chips)

    # 列表
    rows = ""
    shown = 0
    for a in alarms[:top]:
        shown += 1
        tags = "".join("<span class='areg'>{r}</span>".format(r=r) for r in a["regions"])
        rows += ("<div class='alarm-row'><span class='alv lv-{l}'>{l}</span>"
                 "<span class='acat'>{cat}</span><span class='aarea'>{area}</span>"
                 "<span class='atime'>{time}</span>{tags}</div>").format(
            l=a["level"] or "其他", cat=a["cat"], area=a["area"],
            time=(a["time"] or "-")[5:] or "-", tags=tags)
    rest = n - shown
    more = ("<div class='alarm-more'>另有 {rest} 条其余预警未列出，完整清单见属地气象台或" 
            "<a href='http://www.nmc.cn/'>中央气象台预警</a>。</div>").format(rest=rest) if rest > 0 else ""

    return ("""<div class='card'><h2><span class='no'>4</span>官方预警信号（实时）</h2>
<div class='sec-sub'>数据源：中国气象局 weather.cma.cn 国家预警信息发布中心 · 覆盖四川、重庆 · 按级别/分区聚合</div>
<div class='alarm-banner'>{banner}</div>
<div class='alarm-stats'>{stats}</div>
<div class='alarms'>{rows}</div>
{more}
<div class='note'>预警为官方实时发布，反映当下风险等级；本报告仅做聚合与解读，具体防御请严格遵循属地气象台预警与当地应急指令。</div></div>""").format(
        banner=banner, stats=stats, rows=rows, more=more)


# ---------- 今日最高温分布 · 地图（Leaflet + 多底图回退） ----------
MAP_CARD = """<div class="card"><h2><span class="no">6</span>今日最高温分布 · 地图</h2>
<div class="sec-sub">川渝代表城市 当日最高温示意（预报最高缺测时以当前实况近似） · 悬浮查分区与实况</div>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
#heatmax{height:470px;width:100%;border-radius:12px;overflow:hidden;border:1px solid var(--line);background:#e9edf2}
.heat-legend{display:flex;flex-wrap:wrap;gap:4px;margin-top:10px;font-size:11.5px;color:#41556a;align-items:center}
.heat-legend b{margin-right:4px;color:var(--navy)}
.heat-legend .hl{display:inline-flex;align-items:center;gap:5px;margin:0 8px 4px 0}
.heat-legend .hl i{width:11px;height:11px;border-radius:50%;display:inline-block;border:1px solid rgba(0,0,0,.12)}
</style>
<div id="heatmax"></div>
<div class="heat-legend"><b>分级（今日最高温，℃）</b>
<span class="hl"><i style="background:#c23b2e"></i>≥35</span>
<span class="hl"><i style="background:#ff7a2f"></i>33–34.9</span>
<span class="hl"><i style="background:#f2c23b"></i>30–32.9</span>
<span class="hl"><i style="background:#58b3e0"></i>27–29.9</span>
<span class="hl"><i style="background:#3a79c2"></i>&lt;27</span>
<span class="hl" style="margin-left:6px;color:#8aa0b5">圆点半径随气温增大</span></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function(){
  var DATA=__DATA__;
  function colorOf(t){if(t==null)return '#8aa0b5';if(t>=35)return '#c23b2e';if(t>=33)return '#ff7a2f';if(t>=30)return '#f2c23b';if(t>=27)return '#58b3e0';return '#3a79c2';}
  function radOf(t){if(t==null)return 6;if(t>=37)return 15;if(t>=35)return 13;if(t>=33)return 11;if(t>=30)return 9;return 7;}
  var map=L.map('heatmax',{scrollWheelZoom:false}).setView([30.6,105.6],6);
  var provs=['https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
             'https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png',
             'https://cartodb-basemaps-a.global.ssl.fastly.net/light_all/{z}/{x}/{y}.png'];
  var pi=0, base=L.tileLayer(provs[0],{subdomains:'abc',maxZoom:10,attribution:'© OpenStreetMap（多底图回退）'}).addTo(map);
  base.on('tileerror',function(){if(pi+1<provs.length){pi++;base.setUrl(provs[pi]);}});
  DATA.forEach(function(p){
    L.circleMarker([p.lat,p.lng],{radius:radOf(p.tmax),color:'#fff',weight:2,fillColor:colorOf(p.tmax),fillOpacity:0.9})
      .addTo(map)
      .bindPopup('<b>'+p.city+'</b>（'+p.region+'）<br/>当日最高 <b style="color:#c23b2e">'+(p.tmax!=null?p.tmax+'℃':'—')+'</b>'+(p.hasFc?'':'（以当前实况近似）')+'<br/>当前实况 '+(p.now!=null?p.now+'℃':'—'));
  });
})();
</script>
</div>"""


def build_map_card(fetched):
    pts = []
    for r in REGIONS:
        for nm, _py in r["cities"]:
            c = fetched.get(nm)
            if not c or nm not in CITY_COORDS:
                continue
            d0 = (c["days"] or [{}])[0]
            fc_max = d0.get("tmax")          # 今日预报最高（可能缺测=9999已清为None）
            now = c.get("now_temp")          # 当前实况
            # 截止目前的当日最高：预报最高有效则用之，否则以当前实况近似
            val = fc_max if fc_max is not None else now
            pts.append({"city": nm, "lat": CITY_COORDS[nm][0], "lng": CITY_COORDS[nm][1],
                        "tmax": val, "now": now, "region": r["name"], "hasFc": fc_max is not None})
    if not pts:
        return ""
    return MAP_CARD.replace("__DATA__", json.dumps(pts, ensure_ascii=False))


# 前端实时刷新脚本：页面每次打开即向 NMC 拉取成都/重庆实况，更新双城卡片与预报发布时间。
# 纯字符串（不经 .format），花括号为 JS 字面量；CORS 已开放(nmc.cn)，失败时保留静态兜底值。
LIVE_JS = r"""<script>
(function(){
  /* 前端实时刷新 + 自动轮询：打开页面即拉取成都/重庆 NMC 实况，之后每 2 分钟再次自动拉取，
     使发布时刻/气温等高时效字段持续追随官方最新；失败时静默保留上一次值。 */
  var CODES={"成都":"yGYHR","重庆":"UkfaS"};
  function pad(n){return (n<10?"0":"")+n;}
  function rt(x){return (x==null||isNaN(x))?"-":Math.round(x);}
  function cleanPub(p){var m=String(p||"").match(/(\d{1,2}):(\d{2})/);return m?pad(+m[1])+":"+m[2]:"-";}
  function refresh(){
    var cards=[].slice.call(document.querySelectorAll(".city"));
    if(!cards.length) return;
    var remaining=cards.length, pubs=[];
    cards.forEach(function(card){
      var code=CODES[card.getAttribute("data-city")];
      if(!code){remaining--;return;}
      fetch("https://www.nmc.cn/rest/weather?stationid="+code,{headers:{"X-Requested-With":"fetch"}})
        .then(function(r){return r.json();})
        .then(function(d){
          var real=(d&&d.data&&d.data.real)||{}, wea=real.weather||{}, wind=real.wind||{};
          var tv=card.querySelector(".tval"), T=wea.temperature;
          if(tv && T!=null && Math.abs(T)<=60) tv.textContent=rt(T);
          if(real.publish_time){
            var flag=card.querySelector(".top .flag");
            if(flag) flag.textContent="实况 "+cleanPub(real.publish_time);
            pubs.push(real.publish_time);
          }
          var dt=card.querySelector(".dt");
          if(dt){
            var today=dt.getAttribute("data-today")||"";
            var sp=wind.speed, h=wea.humidity;
            var ws=(wind.dir!=null ? wind.dir+(sp!=null&&sp>=0&&sp<100?" "+sp:"") : "-");
            var hum=(h!=null&&h>=0&&h<=100)?Math.round(h)+"%":"-";
            dt.innerHTML=(ws+" · 湿度 "+hum+" · 今日 "+today);
          }
        }).catch(function(){})
        .then(function(){ remaining--; if(remaining<=0&&pubs.length){pubs.sort();var pv=document.querySelector(".pub-val");if(pv)pv.textContent=cleanPub(pubs[pubs.length-1]);} });
    });
  }
  refresh();
  setInterval(refresh, 120000);
})();
</script>"""


# ---------- 渲染 ----------
CSS = """
:root{--bg:#eef3f9;--card:#fff;--navy:#14324f;--navy2:#2a5b8c;--ink:#24313d;
  --line:#e3ecf5;--accent:#2f7cb6;--muted:#7b8ca0;
  --red:#c23b2e;--orange:#c26a12;--amber:#9a7d0a;--green:#2a8a3e;--soft:#5c93c2}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"Noto Sans CJK SC","PingFang SC","Microsoft YaHei",sans-serif;
  line-height:1.75;-webkit-font-smoothing:antialiased}
.wrap{max-width:1020px;margin:0 auto;padding:34px 18px 32px}
a{color:var(--accent);text-decoration:none}

/* Hero */
.hero{position:relative;overflow:hidden;border-radius:20px;padding:30px 32px;margin-bottom:22px;
  background:radial-gradient(120% 160% at 8% 0%,#2b6b9d 0%,#1c4a75 45%,#14324f 100%);
  color:#fff;box-shadow:0 12px 34px rgba(20,50,79,.28)}
.hero::after{content:"";position:absolute;right:-60px;top:-60px;width:240px;height:240px;
  border-radius:50%;background:radial-gradient(circle,rgba(255,255,255,.14),transparent 65%)}
.hero .kicker{display:inline-block;font-size:12px;letter-spacing:2px;color:#bcd7ef;
  text-transform:uppercase;margin-bottom:8px;background:rgba(255,255,255,.08);
  padding:3px 10px;border-radius:20px}
.hero h1{margin:0 0 8px;font-size:29px;letter-spacing:.5px;line-height:1.3}
.hero .sub{color:#cde0f2;font-size:14px;max-width:640px}
.hero .meta{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:16px;font-size:12.3px;color:#9dbcd9}
.hero .meta b{color:#dcebf8;font-weight:600}

/* Section / card */
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;
  padding:22px 24px;margin:18px 0;box-shadow:0 2px 8px rgba(20,50,79,.05)}
.card h2{display:flex;align-items:center;gap:11px;margin:0 0 6px;font-size:18px;color:var(--navy)}
.card h2 .no{flex:none;width:28px;height:28px;border-radius:9px;background:linear-gradient(135deg,var(--navy),var(--navy2));
  color:#fff;font-size:14px;display:inline-flex;align-items:center;justify-content:center;font-weight:700}
.sec-sub{color:var(--muted);font-size:12.6px;margin:-2px 0 14px}

/* Overview narrative */
.syn{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.syn .chunk{background:linear-gradient(180deg,#f5f9fe,#f9fcfd);border:1px solid var(--line);
  border-radius:12px;padding:14px 16px}
.syn .chunk b{display:block;color:var(--navy);margin-bottom:4px;font-size:14px}
.syn .chunk p{margin:0;font-size:13.4px;color:#41556a}
.chip-tag{display:inline-block;background:var(--accent);color:#fff;font-size:10.5px;font-weight:700;
  border-radius:6px;padding:1px 7px;vertical-align:2px;margin-left:6px}

/* now */
.now{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.city{background:linear-gradient(180deg,#f5f9fe,#fbfdfe);border:1px solid var(--line);
  border-radius:13px;padding:13px 15px}
.city .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:2px}
.city .nm{font-size:14.5px;color:var(--navy);font-weight:700}
.city .flag{font-size:10.5px;color:var(--muted);border:1px solid var(--line);border-radius:6px;padding:0 6px}
.city .tg{font-size:27px;color:var(--navy);font-weight:800;line-height:1.15}
.city .tg small{font-size:13px;color:var(--muted);font-weight:600}
.city .dt{font-size:12.3px;color:#60758a;margin-top:5px;line-height:1.6}
.city .dt b{color:var(--navy)}

/* Region risk */
.regions{display:flex;flex-direction:column;gap:14px}
.reg{border:1px solid var(--line);border-radius:14px;overflow:hidden;background:#fbfdff}
.reg-head{display:flex;align-items:center;gap:14px;padding:12px 16px;flex-wrap:wrap;
  background:linear-gradient(180deg,#f4f8fc,#f8fbfd);border-bottom:1px solid var(--line)}
.reg-head .rname{font-size:16px;font-weight:800;color:var(--navy);letter-spacing:.3px}
.reg-head .rweek{font-size:11.5px;color:var(--muted)}
.pill{display:inline-flex;align-items:center;gap:5px;border-radius:20px;padding:4px 12px;font-size:12.6px;
  font-weight:800;color:#fff;box-shadow:0 2px 6px rgba(20,50,79,.12)}
.p-高{background:linear-gradient(135deg,#d64537,#c23b2e)}
.p-中{background:linear-gradient(135deg,#e2853a,#c26a12)}
.p-较低{background:linear-gradient(135deg,#cdb04a,#9a7d0a)}
.p-关注{background:linear-gradient(135deg,#9db2c4,#7b8ca0)}
.gauge{flex:1;min-width:150px}
.gauge .cap{display:flex;justify-content:space-between;font-size:10.8px;color:var(--muted);
  letter-spacing:.5px;margin-bottom:3px}
.track{height:7px;border-radius:4px;background:#e6eef6;overflow:hidden}
.fill{height:100%;border-radius:4px;background:linear-gradient(90deg,#5c93c2,#2f7cb6)}
.reg-body{padding:12px 16px 14px}
.dims{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}
.dim-node{display:flex;align-items:center;gap:6px;font-size:12.2px;border:1px solid var(--line);
  border-radius:20px;padding:3px 10px;background:#fff}
.dim-node .dot{width:8px;height:8px;border-radius:50%}
.d-dot-0{background:#c8d4e0}.d-dot-1{background:#cdb04a}.d-dot-2{background:#e2853a}.d-dot-3{background:#d64537}
.trendline{display:flex;align-items:center;gap:6px;font-size:12px;color:#41556a;margin-bottom:12px}
.trendline .tlab{color:var(--muted)}
.spark{display:inline-flex}
.spark i{width:11px;height:11px;border-radius:3px;background:#c8d4e0;display:inline-block}
.spark i.on{background:#c23b2e}.spark i.part{background:#e2853a}
.spark i+.arrow{color:#9db2c4;margin:0 4px;font-weight:700}
.cities{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}
.crow{border:1px solid var(--line);border-radius:11px;padding:9px 12px;background:#fff}
.crow .cn{font-size:13px;font-weight:700;color:var(--navy);display:flex;justify-content:space-between;align-items:center}
.crow .cn .cb{font-size:10.5px;font-weight:800;border-radius:6px;padding:1px 6px}
.crow .today{font-size:12.2px;color:#41556a;margin-top:4px}
.crow .today b{color:var(--navy)}
.crow .pbar{height:5px;border-radius:3px;background:#e6eef6;margin-top:6px;overflow:hidden}
.crow .pbar i{display:block;height:100%;background:linear-gradient(90deg,#7ac1e8,#2f7cb6)}
.reg-tip{margin-top:12px;font-size:12.6px;color:#6a3f12;background:#fff8ea;border:1px dashed #e6cf96;
  border-radius:10px;padding:8px 12px}

/* Weather processes */
.proc{list-style:none;margin:0;padding:0}
.proc li{position:relative;padding:9px 0 9px 26px;border-bottom:1px dashed var(--line);font-size:13.4px}
.proc li:last-child{border-bottom:0}
.proc li::before{content:"";position:absolute;left:6px;top:16px;width:8px;height:8px;border-radius:50%;
  background:var(--accent)}
.proc li.warn::before{background:var(--red)}
.proc li b{color:var(--navy)}

/* table */
table{width:100%;border-collapse:collapse;font-size:12.8px}
th,td{border-bottom:1px solid var(--line);padding:8px 6px;text-align:center}
th{color:var(--muted);font-weight:600;font-size:11.6px;letter-spacing:.3px}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
.wbar{display:inline-block;min-width:44px;height:6px;border-radius:3px;background:#e6eef6;position:relative;vertical-align:middle;margin-left:4px}
.wbar i{position:absolute;left:0;top:0;bottom:0;border-radius:3px;background:linear-gradient(90deg,#7ac1e8,#2f7cb6)}

/* ventusky grid */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.grid figure{margin:0;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#fff}
.grid img{width:100%;display:block;aspect-ratio:16/10;object-fit:cover}
.grid figcaption{font-size:12px;color:#5c6b7c;padding:7px 11px}

/* notes & focus */
.foc{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px}
.foc .f{background:#f2f7fb;border:1px solid var(--line);border-radius:11px;padding:10px 13px;
  font-size:12.8px;color:#3b4f62}
.foc .f b{color:var(--navy);display:block;margin-bottom:2px}
.foc .f.red{background:#fdf1ef;border-color:#f0c6be;color:#6d2f26}
.foc .f.red b{color:#a63a2b}
.ep{background:linear-gradient(180deg,#f4f8fc,#fafcfe);border:1px solid var(--line);border-radius:12px;
  padding:12px 15px;font-size:13.6px;color:#31506e;white-space:pre-wrap}
/* 形势解读（专家视角） */
.expert{display:flex;flex-direction:column;gap:11px;margin-top:4px}
.expert-title{font-size:15.5px;font-weight:800;color:#0f2c47;letter-spacing:.2px;
  padding:11px 14px;border-left:4px solid var(--accent);background:linear-gradient(180deg,#f2f7fb,#f8fbfe);
  border-radius:0 9px 9px 0;margin-bottom:2px}
.echunk{background:#fbfdfe;border:1px solid var(--line);border-radius:10px;padding:9px 13px;font-size:13.2px;
  line-height:1.8;color:#37506a}
.echunk b{color:#0f2c47;margin-right:4px;font-weight:700}
.echunk .hl{color:var(--red);font-weight:700}
.echunk .ok{color:var(--green);font-weight:700}

/* 官方预警信号 */
.alarm-banner{display:flex;align-items:flex-start;gap:11px;padding:12px 14px;margin-bottom:12px;
  border-radius:10px;background:linear-gradient(180deg,#fff5f4,#fdeeed);
  border:1px solid #f2c4be;font-size:13.2px;color:#5a3030;line-height:1.7}
.alarm-banner b{color:#b32c1f;font-size:14px}
.alarm-banner .ab-icon{font-size:17px;line-height:1.4}
.alarm-stats{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:12px;font-size:12.3px}
.alarm-stats .s-total{color:var(--muted);margin-right:2px}
.s-chip{display:inline-block;padding:2px 9px;border-radius:14px;font-size:11.6px;font-weight:800;color:#fff}
.c-红色{background:#c23b2e}.c-橙色{background:#e2853a}.c-黄色{background:#c9a227;color:#3b2f00}
.c-蓝色{background:#3a79c2}
.alarms{display:flex;flex-direction:column;gap:6px;margin-bottom:6px}
.alarm-row{display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding:6px 10px;border:1px solid var(--line);
  border-radius:9px;background:#fbfcfe;font-size:12.8px}
.alv{flex:none;color:#fff;font-size:11.2px;font-weight:800;padding:2px 8px;border-radius:7px;min-width:34px;text-align:center}
.lv-红色{background:#c23b2e}.lv-橙色{background:#e2853a}.lv-黄色{background:#c9a227;color:#3b2f00}
.lv-蓝色{background:#3a79c2}.lv-其他{background:#8aa0b5}
.acat{flex:none;font-weight:700;color:var(--navy)}
.aarea{flex:1 1 220px;color:#41556a;min-width:130px}
.atime{flex:none;color:var(--muted);font-size:11.4px}
.areg{flex:none;font-size:10.4px;color:#fff;background:var(--accent);border-radius:6px;padding:1px 6px;font-weight:700}
.alarm-more{font-size:12px;color:var(--muted);margin:8px 2px 2px}
.alarm-more a{color:var(--accent)}
.note{background:#fff8ea;border:1px solid #f0dca8;color:#7a5b12;border-radius:10px;padding:10px 14px;
  font-size:12.3px;margin-top:14px}
.foot{font-size:11.6px;color:#8697aa;margin-top:26px;text-align:center;line-height:1.8}
b.strong{color:var(--navy)}
.warn-t{color:var(--red);font-weight:700}
"""


def esc(s):
    return "-" if s is None else str(s)


def fmt_temp(v):
    if v is None:
        return "-"
    if isinstance(v, str):
        return v or "-"
    try:
        return "%d" % int(round(v))
    except Exception:
        return str(v)


def _lvl_css(level):
    return {"高": 3, "中": 2, "较低": 1, "关注": 0}.get(level, 0)


def _clean_cond(v):
    """把 NMC 的占位符 '-' 或空值归一化为空串"""
    if v is None or str(v).strip() in ("", "-"):
        return ""
    return str(v).strip()


# ---------- 形势解读（专家视角，数据驱动自动生成）----------
def build_expert(fetched):
    """按 weather-analyst 范式，从官方逐站数据自动生成『形势→实况→演变→
    区域→趋势→提示』的专家叙事。返回 (title, [(tag, text), ...])"""
    profiles = []
    obs_hot, obs_cool = None, None
    for nm, c in fetched.items():
        t = c.get("now_temp")
        if t is None:
            continue
        if obs_hot is None or t > obs_hot[1]:
            obs_hot = (nm, t)
        if obs_cool is None or (t < obs_cool[1] and nm in ("广元", "绵阳", "康定")):
            obs_cool = (nm, t)
    for r in REGIONS:
        rc = [fetched[n] for n, _ in r["cities"] if n in fetched]
        if not rc:
            continue
        an = region_analysis(rc)
        risk = region_risk(r["axis"], an)
        dim0 = [_day_dim(c, 0) for c in rc]
        dim1 = [_day_dim(c, 1) for c in rc]
        t0 = max((d["tmax"] or 0) for d in dim0)
        t1 = max((d["tmax"] or 0) for d in dim1)
        p0 = max((d["precip"] or 0) for d in dim0)
        p1 = max((d["precip"] or 0) for d in dim1)
        profiles.append({"name": r["name"], "an": an, "risk": risk,
                         "t0": t0, "t1": t1, "p0": p0, "p1": p1,
                         "maxt": max(t0, t1), "maxp": max(p0, p1),
                         "hot_now": t0 >= 35,
                         "conv": any(d["cp"] >= 2 for d in dim0),
                         "rain_now": p0 >= 25 or an["rp"] >= 2})
    hot = [p for p in profiles if p["hot_now"]]
    rain = [p for p in profiles if p["rain_now"] or p["maxp"] >= 25]
    conv = [p for p in profiles if p["conv"]]

    def _names(ps):
        seen = set()
        out = []
        for p in ps:
            if p["name"] not in seen:
                seen.add(p["name"])
                out.append(p["name"])
        return "、".join(out) or "盆地大部"

    # ---- ① 形势背景
    back = []
    if hot and rain:
        back.append("副热带高压与大陆高压对盆地大部仍具控场，为%s提供晴热背景" % _names(hot))
        back.append("与此同时，高原低槽东移、切变线西侧有冷空气渗透，暖湿气流沿副高边缘输入，在%s形成辐合抬升，水汽充沛、层结不稳定" % _names(rain))
    elif hot:
        back.append("副热带高压与大陆高压呈打通态势，控场%s，下沉增温、逆温抑制对流，晴热少云延续" % _names(hot))
        back.append("盆地其余地区处于高压边缘，午后热力抬升，仍存在分散性对流的触发机会")
    elif rain:
        back.append("高原低槽东移并加深，低涡/切变线维持在%s一带，北方冷空气与偏南暖湿气流辐合，触发强降雨的水汽与不稳定能量均已到位" % _names(rain))
        if conv:
            back.append("层结不稳定，利于触发雷雨大风、短时强降水等强对流")
    else:
        back.append("本轮盆地无明显强天气系统控场，以多云与分散性阵雨为主，天气相对平稳")
    syn_bg = "".join("（%d）%s。" % (i + 1, t) for i, t in enumerate(back)) if back else "（1）本轮天气相对平稳。"

    # ---- ② 实况呈现
    hot_s = ("<span class='hl'>%s</span>" % obs_hot[0]) if obs_hot else "盆地"
    syn_now = ("午后%s以%s℃领跑闷热榜单" % (hot_s, obs_hot[1])) if obs_hot else "午后多站气温在28—36℃之间"
    if obs_cool:
        cool_nm = obs_cool[0]
        cool_ctx = "盆北沿山" if cool_nm in ("广元", "绵阳") else "川西高原" if cool_nm == "康定" else "沿山"
        syn_now += "；受冷空气先导渗透影响，%s（%s）仅%s℃上下，盆地呈'东西偏热、沿山偏凉'的双轨格局" % (
            cool_ctx, cool_nm, obs_cool[1])
    syn_now += "。多数站湿度中等偏上，体感较气温偏高1—3℃。"

    # ---- ③ 系统演变（未来3日）
    peak_today = [p for p in profiles if p["p0"] >= p["p1"] and p["p0"] >= 20]
    peak_tomorrow = [p for p in profiles if p["p1"] > p["p0"] and p["p1"] >= 20]
    peak_s = ""
    if peak_today or peak_tomorrow:
        if peak_today:
            peak_s += "今晚到明天白天%s雨势最盛（单日累计可达%dmm量级）" % (
                _names(peak_today), max(p["p0"] for p in peak_today))
        if peak_tomorrow:
            peak_s += ("；明天主雨带东移，%s仍有一定降雨（%dmm）" % (_names(peak_tomorrow), max(p["p1"] for p in peak_tomorrow)))
        peak_s += "，后天雨带整体减弱、盆地逐步转多云。"
    else:
        peak_s = "未来3日盆地降水整体不强，以分散性阵雨为主，后天大部转多云或晴。"

    # ---- ④ 区域差异
    diff = ("盆西：西部沿山叠加地形增幅，是强降雨首选落区；盆东/川东北：白天晴热加傍晚对流，" +
            "需防'高温暴雨同现'的强对流；盆中：晴雨交替的过渡带；盆北：冷空气通道顺流而下，先凉多雨；" +
            "川西高原/川西南：以高原性午后对流与阵雨为主。")
    if not hot and not rain:
        diff = "各分区差异不大，均以多云或阵雨为主；高原与山地午后对流略强，盆西沿山夜雨相对明显。"

    # ---- ⑤ 趋势判断
    future_hot = [p for p in profiles if p["t1"] >= 35]
    colding = hot and not future_hot
    if colding:
        trend = ("<span class='ok'>高温缓解</span>——%s今日最高<span class='hl'>%s℃</span>仍达高温线，但明日冷空气与降雨压制，" +
                 "盆地最高温将回落至30℃以下，本轮晴热趋于结束。") % (_names(hot), str(max(p["t0"] for p in hot)))
    elif future_hot:
        trend = ("高温延续——%s明日最高温仍可能触及35℃高温线，缓解需等待更系统的冷空气或降雨过程。"
                 % (_names(future_hot)))
    else:
        trend = ("天气平稳——未来3日盆地最高温总体在30℃上下区间，无极端高温，也无系统性强降雨，维持'晴雨交替'的节奏。")

    # ---- ⑥ 风险提示
    hi = [p for p in profiles if p["risk"]["level"] in ("高", "中")]
    tip = []
    if rain:
        tip.append("重点关注<strong>%s</strong>强降雨可能引发的山洪与地质灾害（具滞后性）" % _names(rain))
    if conv:
        tip.append("警惕<strong>%s</strong>午后雷暴大风、短时强降水等强对流" % _names(conv))
    if hot:
        tip.append("<strong>%s</strong>白天晴热，午后减少长时间户外活动，谨防中暑" % _names(hot))
    if not tip:
        tip.append("本轮无明显灾害性天气，正常出行，注意山区午后对流与沿山夜雨")

    # ---- 标题（点题）
    if hot and rain:
        title = "此消彼长：%s余热未消，%s强降雨阻高温抬头" % (_names(hot), _names(rain))
    elif hot:
        title = "陆高/副高控场：%s晴热延续，午后警惕局地强对流" % _names(hot)
    elif rain:
        title = "低涡切变活跃：%s强降雨，须防山洪与地质灾害" % _names(rain)
    else:
        title = "天气相对平稳，晴雨交替，关注午后对流"

    blocks = [("形势背景", syn_bg), ("实况呈现", syn_now), ("系统演变", peak_s),
              ("区域差异", diff), ("趋势判断", trend), ("风险提示", "；".join(tip) + "。")]
    return title, blocks


# ---------- 渲染主函数 ----------
def render(fetched, ventusky_paths, nmc_charts, narr, site_dir, generated, alarms=None, alarm_cnt=None):
    ts = generated.strftime("%Y-%m-%d %H:%M")
    dstr = generated.strftime("%m月%d日")
    publish = next((c["publish"] for c in fetched.values() if c.get("publish")), "-")

    # 官方预警卡片（插入为卡片 4）
    alarm_html = build_alarm_card(alarms or [], alarm_cnt or {})
    # 今日最高温分布 · 地图（插入为卡片 6）
    map_html = build_map_card(fetched)

    # ② 官方预报图（NMC）
    nmc_html = ""
    if nmc_charts:
        blocks = ""
        for label, p in nmc_charts.items():
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            blocks += ("<figure><img src='data:image/jpeg;base64,{b}'/><figcaption>{l}</figcaption></figure>").format(
                b=b64, l=label)
        nmc_html = ("<div class='card'><h2><span class='no'>2</span>官方预报图（中央气象台 NMC）</h2>"
                    "<div class='sec-sub'>全国降水量预报 · 逐时效官方发布 · 叠加本轮分区风险交叉印证</div>"
                    "<div class='grid'>{b}</div>"
                    "<div class='note'>预报图由国家气象中心（中央气象台）公开产品发布，时效以图中标注为准；"
                    "图幅为全国范围，盆地落区请结合上方分区风险判断。</div></div>").format(b=blocks)

    # ③ 形势解读（专家视角，数据驱动自动生成）
    _etitle, _eblocks = build_expert(fetched)
    echunks = "".join("<div class='echunk'><b>【%s】</b>%s</div>" % (tag, txt) for tag, txt in _eblocks)
    expert_html = ("<div class='card'><h2><span class='no'>3</span>形势解读（专家视角）</h2>"
                   "<div class='sec-sub'>依 weather-analyst 范式由每日官方数据自动生成 · 形势→实况→演变→区域→趋势→提示</div>"
                   "<div class='expert-title'>%s</div><div class='expert'>%s</div>"
                   "<div class='note'>大尺度环流表述为基于官方数据的专家解读，仅供参考；灾害性天气以属地气象台发布的预报预警为准。</div></div>") % (
        _etitle, echunks)

    # ① 双城实况
    now_cards = ""
    for key in ("成都", "重庆"):
        c = fetched.get(key)
        if not c:
            continue
        now_cards += ("<div class='city' data-city='{nm}'><div class='top'><span class='nm'>{nm}</span>"
                      "<span class='flag'>实况 {pub}</span></div>"
                      "<div class='tg'><span class='tval'>{t}</span>℃{tag}</div>"
                      "<div class='dt' data-today=\"{today}\">{wind} · 湿度 {hum} · 今日 {today}</div></div>").format(
            nm=c["name"], t=fmt_temp(c.get("now_temp")),
            tag=("　<small>{info}</small>" % {"info": _clean_cond(c.get("now_info"))}) if _clean_cond(c.get("now_info")) else "",
            pub=((c.get("publish") or "")[-5:] or "-"),
            wind=(c.get("wind") or "-"), hum=((c.get("humidity") and "%d%%" % int(c["humidity"])) or "-"),
            today=(c["days"][0]["info"] if c["days"] else "-"))

    # 形势总览：解析各分区强度用于文案
    hot_names, rain_names = [], []
    for r in REGIONS:
        rc = [fetched[n] for n, _ in r["cities"] if n in fetched]
        if not rc:
            continue
        an = region_analysis(rc)
        if an["hp"] >= 2 and an["hp"] >= an["rp"]:
            hot_names.append(r["name"])
        if an["rp"] >= 2:
            rain_names.append(r["name"])
    syn_head = "本轮（未来3日）形势："
    if hot_names and rain_names:
        syn = ("盆地呈显著分区差异——%s以晴热高温为主，%s一带降雨相对活跃，需分区看待。"
               "副高控制强度直接决定盆东至川南高温是否延续，低涡切变与地形抬升则构成西部沿山强降雨的背景。"
               % ("、".join(hot_names), "、".join(rain_names)))
    elif hot_names:
        syn = "盆地及川南以副高控制下的晴热为主，%s为高温持续区，盆西气温相对温和。" % "、".join(hot_names)
    elif rain_names:
        syn = "区域内降雨系统活跃，%s为多雨重点区，其余地区天气相对平稳。" % "、".join(rain_names)
    else:
        syn = "本轮区域天气相对平稳，降水与极端高温均不显著，整体适宜户外活动与出行。"

    # ② 分区风险
    regions_html = ""
    processes = []   # ③ 重点天气过程
    focus = []       # 关注与提示
    for r in REGIONS:
        rc = [fetched[n] for n, _ in r["cities"] if n in fetched]
        if not rc:
            continue
        an = region_analysis(rc)
        risk = region_risk(r["axis"], an)
        lvl = risk["level"]
        # 逐日 spark
        spark = ""
        for i, dd in enumerate(an["days"]):
            s = max(dd[0], dd[1])
            cls = "on" if s >= 2 else ("part" if s == 1 else "")
            lab = ("今:" if i == 0 else "明:" if i == 1 else "后:")
            arrow = "→" if i == 0 else "→"
            spark += ("<span class='spark'>{lab}<i class='{cls}'></i></span>").format(lab=lab, cls=cls)
            if i < 2:
                spark += "<span class='arrow'>→</span>"
        # 维度节点
        dimnodes = ""
        for name, sc, dlab in risk["dims"]:
            dimnodes += ("<span class='dim-node'><span class='dot d-dot-{s}'></span>{n} {l}</span>").format(
                s=_lvl_css(dlab) if False else sc, n=name, l=dlab)
        # 每城一日
        crows = ""
        for c in rc:
            d0 = c["days"][0] if c["days"] else {}
            dm = _day_dim(c, 0)
            pb = min(100, max(3, round(((d0.get("precip") or 0) / 60.0) * 100)))
            cb, cbl = "cb", "关注"
            if dm["rp"] >= 3 or dm["cp"] >= 2:
                cb, cbl = "cb", "高"; ccls = "background:#c23b2e;color:#fff"
            elif dm["rp"] >= 2 or dm["hp"] >= 2:
                ccls = "background:#e2853a;color:#fff"; cbl = "中"
            else:
                ccls = "background:#c8d4e0;color:#3b4f62"
            crows += ("<div class='crow'><div class='cn'>{nm}<span class='cb' style='{cls}'>{bl}</span></div>"
                      "<div class='today'>{info} <b>{tmax}°</b>/<b>{tmin}°</b></div>"
                      "<div class='pbar'><i style='width:{pb}%'></i></div></div>").format(
                nm=c["name"], info=(d0.get("info") or "-"),
                tmax=fmt_temp(d0.get("tmax")), tmin=fmt_temp(d0.get("tmin")),
                pb=pb, cls=ccls, bl=cbl)
        cnames = "、".join(nm for nm, _ in r["cities"] if nm in fetched)
        # peak texts
        maxt = max((_day_dim(c, i)["tmax"] or 0) for c in rc for i in range(3))
        maxp = max((_day_dim(c, i)["precip"] or 0) for c in rc for i in range(3))
        met = "72h最高温约 %d℃ · 72h最大单日降水约 %dmm · 代表站 %s" % (maxt, maxp, cnames)
        regions_html += ("""
<div class='reg'>
  <div class='reg-head'>
    <span class='rname'>{name}</span>
    <span class='pill p-{lvl}'>{lvl}风险</span>
    <div class='gauge'><div class='cap'><span>风险指数 {scr}/3</span><span>未来3日</span></div>
      <div class='track'><div class='fill' style='width:{pct}%'></div></div></div>
  </div>
  <div class='reg-body'>
    <div class='dims'>{dims}</div>
    <div class='trendline'><span class='tlab'>演变</span>{spark}
      <span class='arrow'>{dir}</span><span>{trend}</span></div>
    <div class='cities'>{crows}</div>
    <div class='met' style='color:{mc};font-size:12px;margin-top:10px'>{met}</div>
    <div class='reg-tip'>关注：{focus}｜{tip}</div>
  </div>
</div>""").format(
            name=r["name"], lvl=lvl, scr=risk["score"],
            pct=min(100, 25 + risk["score"] * 25), dims=dimnodes,
            spark=spark, dir=an["trend_dir"], trend=an["trend"], crows=crows,
            focus=r["focus"], tip=risk["tip"], met=met, mc="#7b8ca0")

        # ③ 重点天气过程 + 关注
        if lvl in ("高", "中"):
            if risk["hp"] >= 2 and risk["hp"] >= risk["rp"]:
                processes.append(("高", "%s：未来72小时最高温约 %d℃，持续晴热，注意防暑降温与午后对流" % (r["name"], maxt)))
            elif risk["rp"] >= 2:
                processes.append(("高", "%s：预报最大单日降水约 %dmm，防范局地强降雨、山洪与地质灾害滞后影响" % (r["name"], maxp)))
            elif risk["cp"] >= 2:
                processes.append(("中", "%s：午后局地强对流（雷阵雨/短时大风）活跃，注意出行安全" % r["name"]))
            else:
                processes.append(("中", "%s：天气有波动，晴雨转换频繁，关注午后阵性降水" % r["name"]))
            if risk["hp"] >= 2:
                focus.append(("red", "高温应对", "%s 最高温偏高，午后减少长时间户外活动，谨防中暑" % r["name"]))
            if risk["rp"] >= 2:
                focus.append(("red", "降雨与次生灾害", "%s 降雨明显，山区避免前往，留意山洪/滑坡滞后风险" % r["name"]))
            if risk["cp"] >= 2:
                focus.append(("norm", "强对流", "%s 午后对流活跃，雷雨时段减少露天活动" % r["name"]))
    if not processes:
        processes.append(("norm", "本轮区域天气整体平稳，无明显灾害性天气，随观随报，关注后续预报更新"))
    proc_html = "".join("<li class='{w}'>{t}</li>".format(w=("warn" if w == "高" else ""), t=t)
                        for w, t in processes)

    # 关注与提示卡片
    if not focus:
        focus.append(("norm", "天气平稳", "区域无显著灾害性天气，正常生活与出行即可"))
    # 官方预警回响：把涉及本报告分区的红/橙预警置顶
    if alarms:
        crit = [a for a in alarms if a["level"] in ("红色", "橙色") and a["regions"]]
        if crit:
            rgs = "、".join(sorted({r for a in crit for r in a["regions"]}))
            cats = "、".join(sorted({a["cat"] for a in crit}))
            focus.insert(0, ("red", "官方预警", "%s 现有多条红/橙预警（%s），请以属地气象台最新发布为准，尽量避免高风险户外活动"
                             % (rgs or "川渝", cats or "-")))
    foc_html = "".join("<div class='f {cl}'><b>{t}</b>{d}</div>".format(cl=cl, t=t, d=d) for cl, t, d in focus)

    # ④ 分区逐日总览(16城)：今日 + 明日
    rows = ""
    for r in REGIONS:
        for nm, _ in r["cities"]:
            c = fetched.get(nm)
            if not c or not c["days"]:
                continue
            d0 = c["days"][0] if len(c["days"]) > 0 else {}
            d1 = c["days"][1] if len(c["days"]) > 1 else d0
            p0 = (d0.get("precip") or 0)
            pb = min(100, round((p0 / 60.0) * 100))
            rows += ("<tr><td>{r}</td><td><b>{nm}</b></td>"
                     "<td>{t0}</td><td>{w0}<span class='wbar'><i style='width:{pb}%'></i></span></td>"
                     "<td>{t1}</td><td>{w1}</td></tr>").format(
                r=r["name"], nm=nm, t0=fmt_temp(d0.get("tmax")),
                w0=(d0.get("info") or "-"), pb=pb,
                t1=fmt_temp(d1.get("tmax")), w1=(d1.get("info") or "-"))

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
        vent = ("<div class='card'><h2><span class='no'>10</span>多要素实况解析（Ventusky 数值模式）</h2>"
                "<div class='sec-sub'>同一时刻四要素 · 叠加地形与城市标注 · 与官方数据交叉印证</div>"
                "<div class='grid'>{b}</div>"
                "<div class='note'>实况要素面交叉印证，具体取值与结论以中央气象台及属地气象部门官方预报为准。</div></div>").format(b=blocks)

    narr_block = ("<div class='ep'>{narr}</div>").format(narr=narr or "（未启用 LLM 润色，采用规则化概览，数据取官方实时）")

    html = """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>川渝天气展望 · 分区风险分析报告 {d}</title><style>{css}</style></head>
<body><div class="wrap">
<div class="hero">
  <span class="kicker">Sichuan &amp; Chongqing Weather Outlook</span>
  <h1>川渝天气展望 · 分区风险分析报告</h1>
  <div class="sub">基于中央气象台官方预报 · 七分区 × 降雨/高温/强对流三维风险 · 未来3日演变趋势</div>
  <div class="meta"><span><b>报告日期</b> {d}</span><span><b>预报发布</b> <span class="pub-val">{pub}</span></span>
    <span><b>数据源</b> 中央气象台 NMC</span><span><b>分析范式</b> weather-analyst</span>
    <span><b>生成时间</b> {gen}</span></div>
</div>

<div class="card"><h2><span class="no">1</span>形势概览</h2>
<div class="sec-sub">自大尺度环流切入，先判"盆西水深 / 盆东火热"式的区域格局</div>
<div class="syn"><div class="chunk"><b>环流判断</b><p>{syn}</p></div>
<div class="chunk"><b>解读口径</b><p>本报告不生产新预报，仅对官方逐日预报做分区聚合与透明分级，
  强对流/高温/强降雨的具体落区以属地气象台预警为准。</p></div></div></div>

{nmc_html}
{expert_html}
{alarm_html}

<div class="card"><h2><span class="no">5</span>双城官方实况</h2>
<div class="sec-sub">成都 / 重庆 实时观测 · 实时刷新于 {pub}</div><div class="now">{now_cards}</div></div>

{map_html}

<div class="card"><h2><span class="no">7</span>分区天气与三维风险评估</h2>
<div class="sec-sub">按分区 × 逐日聚合 · 风险合成降雨(0-3)/高温(0-3)/强对流(0-2) 三轴；高/中需重点防范</div>
{regions_html}
<div class="note">风险由各分区代表城市官方逐日预报中的最大单日降水、72h最高气温与雷雨对流信号经透明白箱规则综合评定；
  仅作形势研判，灾害性天气请以属地气象台预警为准。</div></div>

<div class="card"><h2><span class="no">8</span>重点天气过程</h2>
<div class="sec-sub">由分区风险自动提炼的过程清单</div>
<ul class="proc">{proc_html}</ul></div>

<div class="card"><h2><span class="no">9</span>分区逐日预报总览（官方）</h2>
<div class="sec-sub">今明两日逐日天气与最高温 · 柱条反映今日相对降水强度</div>
<table><thead><tr><th>分区</th><th>城市</th><th>今日最高</th><th>今日天气</th><th>明日最高</th><th>明日天气</th></tr></thead>
<tbody>{rows}</tbody></table></div>

{vent}

<div class="card"><h2><span class="no">11</span>关注与提示</h2>
<div class="sec-sub">按分区风险生成，红标为首要关注</div>
<div class="foc">{foc_html}</div>
{narr_block}</div>

<footer>
  <div class="foot">本页由 GitHub Actions 定时自动生成 · 数据来自中央气象台 NMC（含官方预报图）{vent_note} · 未经人工审核，仅供参考<br/>
  灾害性天气请以属地气象部门发布的预报预警为准。双城实况与预报发布在页面加载时实时向 NMC 刷新。</div>
 </footer>
 <script>{live_js}</script>
 </body></html>""".format(
        css=CSS, d=dstr, pub=publish, gen=ts, syn=syn, now_cards=now_cards,
        nmc_html=nmc_html, expert_html=expert_html, alarm_html=alarm_html,
        map_html=map_html, regions_html=regions_html,
        rows=rows, proc_html=proc_html, foc_html=foc_html,
        vent=vent, narr_block=narr_block, live_js=LIVE_JS,
        vent_note=(" / Ventusky" if ventusky_paths else ""))

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

    nmc_charts = fetch_nmc_charts(os.path.join(args.out, "nmc"))

    alarms, alarm_cnt = fetch_alarms()
    if alarms:
        print("[alarm] 已取官方预警 {n} 条 (去重后)".format(n=len(alarms)))

    narr = llm_summary(fetched) if args.llm else None
    if not narr:
        narr = "（未启用 LLM 润色，采用上方规则化形势概览与分区风险，数据取官方实时。）"

    render(fetched, vent, nmc_charts, narr, args.out, datetime.now(), alarms, alarm_cnt)


if __name__ == "__main__":
    main()