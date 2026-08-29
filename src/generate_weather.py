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
import argparse, base64, json, os, re, sys, urllib.request, urllib.parse, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import math

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


# ---- WGS84 → GCJ-02（高德坐标系）----
def _out_china(lat, lng):
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def wgs84_to_gcj02(lat, lng):
    """标准算法（参考点 105°E/35°N）将 WGS84 转为高德 GCJ-02（境外坐标原样返回）。"""
    if _out_china(lat, lng):
        return float(lat), float(lng)
    a = 6378245.0
    ee = 0.00669342162296594323
    lat, lng = float(lat), float(lng)
    x, y = lng - 105.0, lat - 35.0

    def _tl(x, y):
        ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y / 30.0 * math.pi)) * 2.0 / 3.0
        return ret

    def _lg(x, y):
        ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
        return ret

    dlat = _tl(x, y)
    dlng = _lg(x, y)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lng + dlng


# ---------- 川渝全站点数据集（缓存为 JSON，运行期仅按需解析坐标） ----------
PROVINCE_URL = "https://www.nmc.cn/rest/province/{code}"
DATAV_BOUND = "https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json"
DATAV_PROV = ["510000", "500000"]  # 四川、重庆（DataV 区县坐标已为 GCJ-02，高德直用）
COUNTY_PATCH = {  # DataV 缺失 centroid 的区县，按其行政边界中心近似
    "蓬溪县": (30.6526, 105.7168),
    "叙州区": (28.7860, 104.3897),
}
_COUNTY_TAIL = re.compile(
    r"(土家族苗族自治县|苗族土家族自治县|彝族羌族自治县|藏羌族自治县|"
    r"彝族自治县|羌族自治县|藏族自治县|苗族侗族自治县|自治州|自治县|县|区|市)$")
_COUNTY_ALIAS = {
    "重庆": "渝中区", "凉山": "凉山彝族自治州", "两江新区": "渝北区",
    "万盛": "綦江区", "会理城区": "会理市", "九龙": "九龙县",
}


def _county_core(s):
    t = s or ""
    while True:
        m = _COUNTY_TAIL.search(t)
        if not m:
            break
        t = t[:m.start()]
    return t


def _geo_county(name, cc):
    if not name:
        return None
    if name in cc:
        return cc[name]["lat"], cc[name]["lng"]
    alias = _COUNTY_ALIAS.get(name)
    if alias and alias in cc:
        return cc[alias]["lat"], cc[alias]["lng"]
    core = _county_core(name)
    for k in cc:
        if k == name or _county_core(k) == core:
            return cc[k]["lat"], cc[k]["lng"]
    pre = [k for k in cc if _county_core(k).startswith(core) and _county_core(k) != core]
    if pre:
        pre.sort(key=lambda k: (len(_county_core(k)), len(k)))
        return cc[pre[0]]["lat"], cc[pre[0]]["lng"]
    con = [k for k in cc if core and core.startswith(_county_core(k))]
    if con:
        con.sort(key=lambda k: -len(_county_core(k)))
        return cc[con[0]]["lat"], cc[con[0]]["lng"]
    return None


def _datav_get_json(url):
    return json.loads(http_get(url, timeout=20).decode("utf-8", "ignore"))


def build_county_coords(site_dir):
    """构建或复用川渝区县坐标表（阿里云 DataV / GCJ-02），写入 site/county_coords.json。"""
    path = os.path.join(site_dir, "county_coords.json")
    try:
        if os.path.exists(path):
            cc = json.load(open(path, encoding="utf-8"))
            if cc:
                return cc
    except Exception:
        pass
    cc = {}

    def add(feats):
        for f in feats:
            p = f.get("properties") or {}
            if p.get("name") and p.get("centroid"):
                cc.setdefault(p["name"], {"adcode": p.get("adcode"),
                                          "lng": p["centroid"][0], "lat": p["centroid"][1]})

    pref = []
    for adcode in DATAV_PROV:
        j = _datav_get_json(DATAV_BOUND.format(adcode=adcode))
        add(j["features"])
        for f in j["features"]:
            ad = (f.get("properties") or {}).get("adcode")
            if adcode == "510000" and ad and str(ad)[-2:] == "00" and str(ad) != "510000":
                pref.append(ad)
    for ad in pref:
        try:
            add(_datav_get_json(DATAV_BOUND.format(adcode=ad))["features"])
            time.sleep(0.15)
        except Exception:
            pass
    for nm, (lat, lng) in COUNTY_PATCH.items():
        cc.setdefault(nm, {"lat": lat, "lng": lng, "patch": True})
    os.makedirs(site_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cc, f, ensure_ascii=False, indent=1)
    return cc


def build_stations(site_dir):
    """拉取川渝全量站点（NMC：四川 ASC + 重庆 ACQ）并配坐标（DataV GCJ-02），
    结果缓存到 site/stations.json。坐标仅在出现新站点时解析，日常运行只复用缓存并拉取对应实况。"""
    path = os.path.join(site_dir, "stations.json")
    cache = {}
    try:
        if os.path.exists(path):
            for s in json.load(open(path, encoding="utf-8")):
                if s.get("code") and s.get("lat") is not None:
                    cache[s["code"]] = s
    except Exception:
        pass
    cc = build_county_coords(site_dir)
    raw = []
    for code in ("ASC", "ACQ"):
        try:
            raw += _datav_get_json(PROVINCE_URL.format(code=code))
        except Exception as e:
            print("[stations] {c} 站点列表失败: {e}".format(c=code, e=e))
    out, resolved = [], 0
    seen = set()
    for st in raw:
        code, city = st.get("code"), st.get("city")
        if not code or code in seen:
            continue
        seen.add(code)
        rec = {"code": code, "city": city or "", "province": st.get("province", ""),
               "lat": None, "lng": None}
        old = cache.get(code)
        g = (old["lat"], old["lng"]) if (old and old.get("lat") is not None) else _geo_county(city, cc)
        if g and g[0] is not None:
            rec["lat"], rec["lng"] = g[0], g[1]
            resolved += 1
        else:
            print("[stations] 无坐标: {c}/{n}".format(c=code, n=city))
        out.append(rec)
    os.makedirs(site_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("[stations] 共 {n} 站点 · 有坐标 {r}".format(n=len(out), r=resolved))
    return out


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
    for i, dd in enumerate((data.get("predict") or {}).get("detail") or []):
        if i >= 4:
            break
        day = dd.get("day", {}).get("weather", {}) or {}
        night = dd.get("night", {}).get("weather", {}) or {}
        info = _cond(day.get("info"))
        ninfo = _cond(night.get("info"))
        if ninfo and ninfo != info:
            info = (info + "转" + ninfo) if info else ninfo
        tmax = _temp(day.get("temperature"))
        # NMC 对"当日"预报气温常返回缺测哨兵 9999，此时以实况气温近似当日最高温，
        # 避免"今日最高/今日区卡"整列显示为 "-"。
        if i == 0 and tmax is None:
            tmax = _temp(weat.get("temperature"))
        days.append({
            "date": dd.get("date", ""),
            "info": info or "-",
            "tmax": tmax,
            "tmin": _temp(night.get("temperature")),
            "precip": _precip(dd.get("precipitation")),
        })
    return {
        "name": name,
        "code": code,
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


def _visual_review_charts(b64_charts):
    """用视觉LLM读取中央气象台官方【实时】实况天气图，综合识别天气系统；无key或失败返回None。
    b64_charts: [(图名, base64字符串)]（base64不含data:前缀）"""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key or not b64_charts:
        return None
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    sys_p = ("你是专业天气形势分析员，遵循川渝气象分析范式。下面给出中央气象台官方【实时】实况分析天气图"
             "（图上已标注等值线、槽线、切变线、锋线、高/低压中心等官方分析）。请综合这些实时天气图，"
             "用简体中文输出一段【实时天气图综合研判】(约180字)：指出当前影响川渝的主要天气系统及其位置"
             "（副热带/大陆高压、5880线/脊位、槽脊、低涡、切变线、锋面、冷空气路径等）、系统间的搭配，"
             "并推断其对四川盆地高温或降水的总体影响。只依据图上实际可见信息，一字不得编造；看不清的部分不要臆测，"
             "直接说“图面未提供/模糊”。")
    content = [{"type": "text",
                "text": "以下为中央气象台官方实时实况分析天气图，请综合研判影响川渝的天气系统及对盆地高温/降水的影响；看不清的不要臆测。"}]
    for lab, b64 in b64_charts:
        content.append({"type": "text", "text": "[%s]" % lab})
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,%s" % b64}})
    msg = [{"role": "system", "content": sys_p}, {"role": "user", "content": content}]
    payload = json.dumps({"model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                          "messages": msg, "temperature": 0.3, "max_tokens": 600}).encode("utf-8")
    try:
        req = urllib.request.Request(base + "/chat/completions", data=payload,
                                     headers={**UA, "Content-Type": "application/json",
                                              "Authorization": "Bearer " + api_key})
        with urllib.request.urlopen(req, timeout=75) as r:
            j = json.loads(r.read().decode("utf-8"))
        txt = j["choices"][0]["message"]["content"].strip()
        return txt or None
    except Exception as ex:
        print("[visual] 失败:", ex)
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


def _compress_jpg(data, max_w=900, q=72):
    """内存中降采样+压缩 JPEG（用于内嵌n多张小图时减小HTML体积）；无PIL则原样返回"""
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = im.size
        if w > max_w:
            im = im.resize((max_w, int(h * max_w / w)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q, optimize=True)
        return buf.getvalue()
    except Exception:
        return data


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


# ---------- 川渝全站点实况分布 · 地图（高德直连 + 全站点实时） ----------
MAP_CARD = """<div class="card"><h2><span class="no">6</span>川渝全站点实况分布 · 地图</h2>
<div class="sec-sub">叠加川渝全量气象站点（四川 __SC_S__ + 重庆 __SC_C__ 个）· 高德底图直连（无需代理）· 圆点按各站点实时实况着色</div>
<link rel="stylesheet" href="https://cdn.staticfile.org/leaflet/1.9.4/leaflet.min.css"/>
<style>
#heatmax{height:520px;width:100%;border-radius:12px;overflow:hidden;border:1px solid var(--line);background:#e9edf2}
.heat-legend{display:flex;flex-wrap:wrap;gap:4px;margin-top:10px;font-size:11.5px;color:#41556a;align-items:center}
.heat-legend b{margin-right:4px;color:var(--navy)}
.heat-legend .hl{display:inline-flex;align-items:center;gap:5px;margin:0 8px 4px 0}
.heat-legend .hl i{width:11px;height:11px;border-radius:50%;display:inline-block;border:1px solid rgba(0,0,0,.12)}
.layer-note{font-size:11.5px;color:#7b8ca0;margin-top:6px}
.live-stat{color:var(--accent);font-weight:600}
</style>
<div id="heatmax"></div>
<div class="heat-legend"><b>实况气温分级（℃）</b>
<span class="hl"><i style="background:#c23b2e"></i>≥35</span>
<span class="hl"><i style="background:#ff7a2f"></i>33–34.9</span>
<span class="hl"><i style="background:#f2c23b"></i>30–32.9</span>
<span class="hl"><i style="background:#58b3e0"></i>25–29.9</span>
<span class="hl"><i style="background:#3a79c2"></i>&lt;25</span>
<span class="hl"><i style="background:#8aa0b5"></i>缺测</span></div>
<div class="layer-note">底图：高德（无需代理）· 小圆点=川渝各站点实时实况（生成时服务端抓取·浏览器追更）· 大圆=七分区代表城区今日预报最高温。 <span class="live-stat" id="liveStat">实况更新中…</span> · <span class="live-stat" id="liveTs"></span></div>
<script src="https://cdn.staticfile.org/leaflet/1.9.4/leaflet.min.js"></script>
<script>
(function(){
  var STATIONS=__STATIONS__;
  var DATA=__DATA__;
  function colorOf(t){if(t==null)return '#8aa0b5';if(t>=35)return '#c23b2e';if(t>=33)return '#ff7a2f';if(t>=30)return '#f2c23b';if(t>=25)return '#58b3e0';return '#3a79c2';}
  function radOf(t){if(t==null)return 5;if(t>=35)return 11;if(t>=33)return 10;if(t>=30)return 9;return 7;}
  function radFix(t){if(t==null)return 9;if(t>=35)return 16;if(t>=33)return 14;if(t>=30)return 12;if(t>=27)return 10;return 8;}

  var map=L.map('heatmax',{scrollWheelZoom:false}).setView([30.6,105.6],6);

  /* 高德底图（国内直连，免代理） */
  var gdRoad='https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}';
  var gdSat='https://webst0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=6&x={x}&y={y}&z={z}';
  var gdSatL='https://webst0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}';
  var road=L.tileLayer(gdRoad,{subdomains:'1234',maxZoom:18,attribution:'© 高德地图（直连）'}).addTo(map);
  var sat=L.layerGroup([
    L.tileLayer(gdSat,{subdomains:'1234',maxZoom:18,attribution:'© 高德'}),
    L.tileLayer(gdSatL,{subdomains:'1234',maxZoom:18,transparent:true,attribution:'© 高德'})
  ]);

  /* 图层1：代表城区今日预报最高温 */
  var rep=[], REPMAP={};
  DATA.forEach(function(p){
    var mk=L.circleMarker([p.lat,p.lng],{radius:radFix(p.tmax),color:'#222',weight:2,fillColor:colorOf(p.tmax),fillOpacity:0.88,riseOnHover:true});
    mk.city=p.city; mk.region=p.region; mk._tmax=p.tmax; mk._now=p.now;
    mk.bindPopup('<b>'+p.city+'</b>（'+p.region+'）<br/>今日预报最高 <b style="color:#c23b2e">'+(p.tmax!=null?p.tmax+'℃':'—')+'</b>'+(p.hasFc?'':'（以实况近似）')+'<br/>当前实况 '+(p.now!=null?p.now+'℃':'—'));
    rep.push(mk); REPMAP[p.city]=mk;
  });
  var repLayer=L.layerGroup(rep).addTo(map);
  window.__WEATHER_REP=REPMAP;

  /* 图层2：川渝全站点实时实况 */
  var live=[];
  var LIVE=__LIVE__;
  STATIONS.forEach(function(s,i){
    var o=LIVE&&LIVE[i], T=o?o[0]:null;
    var mk=L.circleMarker([s.lat,s.lng],{radius:(T!=null?radOf(T):5),color:'#fff',weight:1,
      fillColor:(T!=null?colorOf(T):'#8aa0b5'),fillOpacity:0.9,riseOnHover:true});
    mk.bindPopup(o&&T!=null
      ?('<b>'+s.city+'</b>（'+s.province+'）<br/>实况 <b style="color:#c23b2e">'+Math.round(T)+'℃</b>'+(o[1]?' · '+o[1]:'')+'<br/>发布 '+(o[2]||'—'))
      :('<b>'+s.city+'</b>（'+s.province+'）<br/>实况——'));
    live.push(mk);
  });
  var liveLayer=L.layerGroup(live).addTo(map);
  window.__WEATHER_MAP=map; window.__WEATHER_STATIONS=STATIONS; window.__WEATHER_LIVE=LIVE;
  window.__WEATHER_DATA=DATA; window.__WEATHER_COLOR=colorOf; window.__WEATHER_RAD=radOf; window.__WEATHER_RADFIX=radFix;

  function stamp(txt){var e=document.getElementById('liveTs'); if(e)e.textContent=txt;}
  stamp('实况由生成时服务端一次抓取（'+__LIVE_TS__+'），浏览器每10分钟轻量追更');

  L.control.layers({'高德路网':road,'高德卫星':sat},
    {'代表城区(今日预报最高)':repLayer,'川渝全站点(实时实况)':liveLayer}, {collapsed:false}).addTo(map);

  /* 实时实况：NMC 逐站拉取，并发受控，逐个着色（简单 GET 免预检跨域） */
  function pad(n){return (n<10?'0':'')+n;}
  var statEl=document.getElementById('liveStat');
  function refresh(){
    var qi=0, done=0, tasks=STATIONS.slice(), ok=0;
    function pump(){
      if(qi>=tasks.length){ if(done>=tasks.length && statEl) statEl.textContent='已加载实况 '+ok+'/'+tasks.length; return; }
      var i=qi; qi++;
      (function(i){
        var s=tasks[i], mk=live[i];
        fetch('https://www.nmc.cn/rest/weather?stationid='+s.code)
          .then(function(r){return r.json();})
          .then(function(d){
            var real=(d&&d.data&&d.data.real)||{}, wea=real.weather||{};
            var T=wea.temperature;
            if(mk && T!=null && !isNaN(T) && Math.abs(T)<=60){
              mk.setStyle({fillColor:colorOf(T),radius:radOf(T)}); ok++;
              var pt=(real.publish_time||'').slice(11,16);
              mk.setPopupContent('<b>'+s.city+'</b>（'+s.province+'）<br/>实况 <b style="color:#c23b2e">'+Math.round(T)+'℃</b>'+(wea.info?' · '+wea.info:'')+'<br/>发布 '+(pt||'—'));
            }else if(mk){
              mk.setPopupContent('<b>'+s.city+'</b>（'+s.province+'）<br/>实况暂缺（数据缺测）');
            }
          }).catch(function(){})
          .then(function(){ done++; if(statEl) statEl.textContent='实况 '+(done<tasks.length?'更新中 '+done+'/'+tasks.length:'加载完成 '+ok+'/'+tasks.length); if(done>=tasks.length) stamp('最后刷新 '+new Date().toTimeString().slice(0,8)); pump(); });
      })(i);
    }
    for(var k=0;k<6;k++){
      if(qi>=tasks.length) break;
      pump();
    }
  }
  refresh();
  setInterval(refresh, 600000);
})();
</script>
<style>
.share-zone{display:flex;justify-content:flex-end;margin-top:8px}
.share-btn{border:1px solid #c7d5e2;background:#fff;color:#12395b;font-size:12.5px;font-weight:600;border-radius:20px;
  padding:6px 14px;cursor:pointer;transition:.15s;box-shadow:0 1px 3px rgba(18,45,80,.08)}
.share-btn:hover{background:#ecf3fb;border-color:#9db9d4}
#shareOverlay{position:fixed;top:0;left:0;right:0;bottom:0;z-index:99999;display:none;
  align-items:center;justify-content:center;padding:20px;background:rgba(15,28,46,.55);
  box-sizing:border-box;overflow:auto}
.share-box{box-sizing:border-box;background:#fff;border-radius:14px;box-shadow:0 18px 60px rgba(0,0,0,.35);
  max-width:min(92vw,1200px);max-height:calc(92vh - 30px);display:flex;flex-direction:column;
  overflow:hidden;margin:auto}
.share-head{display:flex;align-items:center;justify-content:space-between;padding:11px 16px;border-bottom:1px solid #e8eef4;flex:0 0 auto}
.share-body{flex:1;min-height:0;overflow:auto;display:flex;background:#eef2f6}
.share-body img{display:block;max-width:100%;max-height:calc(86vh - 150px);height:auto;object-fit:contain;margin:auto}
.share-foot{display:flex;gap:10px;justify-content:center;padding:12px 16px;border-top:1px solid #e8eef4}
.share-foot button{border:none;border-radius:20px;padding:8px 22px;font-size:14px;font-weight:700;cursor:pointer}
.share-foot .dl{background:#12395b;color:#fff}
.share-foot .cancel{background:#eef2f6;color:#41556a}
.share-foot .copy{background:#e8773c;color:#fff}
.share-foot .share{background:#1fa463;color:#fff}
</style>
<div class="share-zone"><button class="share-btn" onclick="shareMapCard6()">📷 分享地图（带实况 · 图片）</button></div>
<div id="shareOverlay"><div class="share-box">
  <div class="share-head"><b>川渝全站点实况分布 · 分享图片</b><button onclick="closeShare()">✕</button></div>
  <div class="share-body"><img id="shareImg" alt="分享地图"/></div>
  <div class="share-foot"><button class="copy" onclick="copyShare()">📋 复制图片</button><button class="share" id="nativeShareBtn" style="display:none" onclick="nativeShare()">📤 系统分享</button><button class="dl" onclick="downloadShare()">⬇ 下载 PNG</button><button class="cancel" onclick="closeShare()">关闭</button></div>
</div></div>
<script>
(function(){
  var o=document.getElementById('shareOverlay'); if(o&&o.parentNode&&o.parentNode!==document.body) document.body.appendChild(o);
  window.__shareCanvas=null;
  function toast(t){ var e=document.getElementById('shareToast'); if(e){ e.textContent=t; e.style.opacity='1';
    clearTimeout(e._t); e._t=setTimeout(function(){e.style.opacity='0';},1800); } }
  /* 画布重绘分享图：瓦片与点位共用同一 Leaflet 投影，保证严格对齐（规避 html2canvas 对瓦片 transform 的偏移缺陷） */
  function renderSharePicture(){
    return new Promise(function(res){
      var map=window.__WEATHER_MAP; if(!map){ res(null); return; }
      var EW=1280, EH=620;
      var z=Math.round(map.getZoom())||6;
      var center=map.getCenter();
      var cpx=map.project(center,z);
      var tlx=cpx.x-EW/2, tly=cpx.y-EH/2;
      var scale=Math.max(1,map.getSize().x?EW/map.getSize().x:1);
      var mcv=document.createElement('canvas'); mcv.width=EW; mcv.height=EH;
      var mc=mcv.getContext('2d');
      mc.fillStyle='#e9edf2'; mc.fillRect(0,0,EW,EH);
      function circ(ptx,pty,r,fill){ mc.beginPath(); mc.arc(ptx,pty,r,0,Math.PI*2);
        mc.fillStyle=fill; mc.fill(); mc.lineWidth=Math.max(1,r/6); mc.strokeStyle='rgba(255,255,255,.92)'; mc.stroke(); }
      function drawData(){
        (window.__WEATHER_DATA||[]).forEach(function(p){ if(p.lat==null)return;
          var pt=map.project(L.latLng(p.lat,p.lng),z);
          var R=(window.__WEATHER_RADFIX?window.__WEATHER_RADFIX(p.tmax):10)*scale;
          circ(pt.x-tlx,pt.y-tly,R,window.__WEATHER_COLOR?window.__WEATHER_COLOR(p.tmax):'#8aa0b5'); });
        var sts=window.__WEATHER_STATIONS||[], LIVE=window.__WEATHER_LIVE||[];
        sts.forEach(function(s,i){ if(s.lat==null)return;
          var oo=LIVE&&LIVE[i],T=oo?oo[0]:null;
          var pt=map.project(L.latLng(s.lat,s.lng),z);
          var R=(T!=null?(window.__WEATHER_RAD?window.__WEATHER_RAD(T):8):5)*scale;
          circ(pt.x-tlx,pt.y-tly,R,T!=null?(window.__WEATHER_COLOR?window.__WEATHER_COLOR(T):'#8aa0b5'):'#8aa0b5'); });
        compose(mcv);
      }
      /* 加载可见瓦片 */
      var turl='https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}';
      var mnx=Math.floor(tlx/256), mxx=Math.ceil((tlx+EW)/256);
      var mny=Math.floor(tly/256), mxy=Math.ceil((tly+EH)/256);
      var todo=0, done=0;
      function fin(){ done++; if(done>=todo) drawData(); }
      for(var ty=mny;ty<=mxy;ty++)for(var tx=mnx;tx<=mxx;tx++){
        (function(tx,ty){ todo++;
          var img=new Image(); img.crossOrigin='anonymous';
          img.onload=function(){ mc.drawImage(img,Math.round(tx*256-tlx),Math.round(ty*256-tly)); fin(); };
          img.onerror=fin;
          img.src=turl.replace('{x}',tx).replace('{y}',ty).replace('{z}',z);
        })(tx,ty);
      }
      if(todo===0) drawData();
      function compose(mcv2){
        var pad=18,gap=16,tbar=46;
        var W=mcv2.width+pad*2, H=pad+tbar+mcv2.height+gap+34+pad;
        var cv=document.createElement('canvas'); cv.width=W; cv.height=H;
        var x=cv.getContext('2d');
        x.fillStyle='#ffffff'; x.fillRect(0,0,W,H);
        x.fillStyle='#12395b'; x.font='700 27px "PingFang SC","Microsoft YaHei",sans-serif'; x.textAlign='center';
        x.fillText('川渝全站点实况分布 · 实时地图', W/2, pad+31);
        x.drawImage(mcv2, pad, pad+tbar);
        var items=[['#c23b2e','≥35'],['#ff7a2f','33–34.9'],['#f2c23b','30–32.9'],['#58b3e0','25–29.9'],['#3a79c2','<25'],['#8aa0b5','缺测']];
        var ly=pad+tbar+mcv2.height+gap+20, lx=pad;
        x.textAlign='left';
        x.fillStyle='#12395b'; x.font='700 17px "PingFang SC","Microsoft YaHei",sans-serif'; x.fillText('实况气温分级（℃）',lx,ly);
        lx+= x.measureText('实况气温分级（℃）').width+16;
        x.font='15px "PingFang SC","Microsoft YaHei",sans-serif';
        items.forEach(function(it){
          x.beginPath(); x.arc(lx+8,ly-5,8,0,Math.PI*2); x.fillStyle=it[0]; x.fill();
          x.lineWidth=1; x.strokeStyle='rgba(0,0,0,.16)'; x.stroke();
          lx+=24; x.fillStyle='#41556a'; x.fillText(it[1],lx,ly);
          lx+= x.measureText(it[1]).width+26;
        });
        window.__shareCanvas=cv; res(cv);
      }
    });
  }
  window.shareMapCard6=function(){
    renderSharePicture().then(function(cv){
      if(!cv){ toast('分享渲染失败，请稍后重试'); return; }
      var img=document.getElementById('shareImg'); if(img) img.src=cv.toDataURL('image/png');
      var ov=document.getElementById('shareOverlay'); if(ov) ov.style.display='flex';
      toast('已生成带实况的地图分享图');
    });
  };
  window.downloadShare=function(){ var cv=window.__shareCanvas; if(!cv)return toast('请先点击分享生成图片');
    var a=document.createElement('a'); a.href=cv.toDataURL('image/png'); a.download='川渝实况地图.png'; a.click(); };
  window.copyShare=function(){ var cv=window.__shareCanvas; if(!cv)return toast('请先点击分享生成图片');
    cv.toBlob(function(blob){
      if(!blob) return toast('生成失败，请使用下载');
      if(navigator.clipboard && window.ClipboardItem){
        navigator.clipboard.write([new ClipboardItem({'image/png':blob})])
          .then(function(){ toast('✓ 图片已复制，可直接粘贴到微信/备忘录'); })
          .catch(function(){ toast('复制被浏览器拦截，请使用下载'); });
      } else { toast('当前浏览器不支持复制图片，请使用下载'); }
    },'image/png');
  };
  window.nativeShare=function(){ var cv=window.__shareCanvas; if(!cv)return toast('请先点击分享生成图片');
    cv.toBlob(function(blob){ var f=new File([blob],'川渝实况地图.png',{type:'image/png'});
      navigator.share({title:'川渝全站点实况分布',text:'实时地图',files:[f]}).catch(function(){});
    },'image/png');
  };
  window.closeShare=function(){ var ov=document.getElementById('shareOverlay'); if(ov)ov.style.display='none'; };
  (function(){ if(navigator.share && navigator.canShare){
    var t=null; try{ var f=new File(['a'],'x.png',{type:'image/png'}); t=navigator.canShare({files:[f]}); }catch(e){ t=false; }
    var b=document.getElementById('nativeShareBtn'); if(b&&t) b.style.display='';
  } })();
})();
</script>
<div id="shareToast" style="position:fixed;left:50%;bottom:42px;transform:translateX(-50%);z-index:100000;
  background:rgba(20,32,48,.92);color:#fff;font-size:13px;padding:9px 16px;border-radius:8px;
  opacity:0;transition:opacity .25s;pointer-events:none;max-width:80vw;text-align:center"></div>
</div>
"""


def fetch_station_live(sts):
    """生成时由服务端并发抓取全部站点 NMC 实时实况并烘焙进页面。
    浏览器端因 NMC 按客户端 IP 限流(约45个后)无法逐站拉全 201 站，
    改为服务端抓全后浏览器仅做轻量追更，规避限流。并发抓取以缩短构建时间。"""
    def get(u):
        r = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        return json.load(urllib.request.urlopen(r, timeout=6))
    def one(s):
        try:
            d = get("https://www.nmc.cn/rest/weather?stationid=" + s["code"])
            real = (d.get("data") or {}).get("real") or {}
            wea = real.get("weather") or {}
            T = wea.get("temperature")
            if T in (None, "", "9999", "999"):
                return None
            info = _cond(wea.get("info", "")) or ""
            pt = str(real.get("publish_time") or "")[-5:]
            return [round(float(T), 1), info, pt]
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(one, sts))


def build_map_card(fetched, stations):
    pts = []
    for r in REGIONS:
        for nm, _py in r["cities"]:
            c = fetched.get(nm)
            if not c or nm not in CITY_COORDS:
                continue
            d0 = (c["days"] or [{}])[0]
            fc_max = d0.get("tmax")
            now = c.get("now_temp")
            val = fc_max if fc_max is not None else now
            glat, glng = wgs84_to_gcj02(*CITY_COORDS[nm])
            pts.append({"city": nm, "lat": glat, "lng": glng,
                        "tmax": val, "now": now, "region": r["name"], "hasFc": fc_max is not None})
    sts = [{"code": s["code"], "city": s["city"], "province": s.get("province", ""),
            "lat": s["lat"], "lng": s["lng"]}
           for s in stations if s.get("lat") is not None]
    if not pts or not sts:
        return ""
    s_cnt = sum(1 for s in sts if "四川" in s["province"])
    c_cnt = sum(1 for s in sts if "重庆" in s["province"])
    live_obs = fetch_station_live(sts)
    live_ts = datetime.now().strftime("%H:%M")
    return (MAP_CARD
            .replace("__STATIONS__", json.dumps(sts, ensure_ascii=False))
            .replace("__DATA__", json.dumps(pts, ensure_ascii=False))
            .replace("__LIVE__", json.dumps(live_obs, ensure_ascii=False))
            .replace("__LIVE_TS__", json.dumps(live_ts, ensure_ascii=False))
            .replace("__SC_S__", str(s_cnt)).replace("__SC_C__", str(c_cnt)))


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
      fetch("https://www.nmc.cn/rest/weather?stationid="+code)
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


# ---------- 前端实时预报刷新（随 NMC 官方预报自动追更：今/明预报表·分区今日·双城今日·地图代表层） ----------
# 纯字符串（不经 .format，花括号为 JS 字面量）；__LIVE_CODES__ 在 render 时替换为 JSON。
LIVE_FORECAST_JS = r"""<script>
(function(){
  /* 与 NMC 官方预报实时对齐：逐城拉取 predict，刷新卡片9分区逐日预报总览表、卡片7分区卡片"今日"、
     卡片5双城"今日"预报、以及地图"代表城区今日预报最高"层。失败自动保留原值。 */
  var CODES=__LIVE_CODES__;
  function rt(v){v=Number(v);return(!isNaN(v)&&Math.abs(v)<=60)?Math.round(v):null;}
  function precOf(v){v=Number(v);return(!isNaN(v)&&v>=0&&v<=800&&v!==9999)?v:0;}
  function cleanInfo(s){s=String(s||'').trim();return(s==='9999'||s==='999'||s==='0'||s==='-'||s==='')?'':s;}
  function dayOf(dd,fbT){
    var d=(dd&&dd.day&&dd.day.weather)||{}, n=(dd&&dd.night&&dd.night.weather)||{};
    var info=cleanInfo(d.info), ninfo=cleanInfo(n.info);
    if(ninfo&&ninfo!==info){info=info?info+'转'+ninfo:ninfo;}
    var tmax=rt(d.temperature);
    /* 当日预报气温常为缺测哨兵(9999)，以实况气温近似当日最高温，避免整列 "-" */
    if(tmax==null&&fbT!=null)tmax=fbT;
    return{info:info||'-',tmax:tmax,tmin:rt(n.temperature),prec:precOf(dd&&dd.precipitation)};
  }
  function fmtT(v){return(v==null)?'-':v+'°';}
  function barW(p){return Math.min(100,Math.max(3,Math.round((p||0)/60*100)));}
  function colorOf(t){if(t==null)return'#8aa0b5';if(t>=35)return'#c23b2e';if(t>=33)return'#ff7a2f';if(t>=30)return'#f2c23b';if(t>=25)return'#58b3e0';return'#3a79c2';}
  function radFix(t){if(t==null)return 9;if(t>=35)return 16;if(t>=33)return 14;if(t>=30)return 12;if(t>=27)return 10;return 8;}

  function applyCity(nm,d0,d1){
    /* 卡片9 分区逐日预报总览表 */
    var tr=document.querySelector('tr[data-city="'+nm+'"]');
    if(tr&&tr.cells){
      if(tr.cells[2])tr.cells[2].textContent=fmtT(d0.tmax);
      if(tr.cells[4])tr.cells[4].textContent=fmtT(d1.tmax);
      if(tr.cells[3])tr.cells[3].innerHTML=d0.info+'<span class="wbar"><i style="width:'+barW(d0.prec)+'%"></i></span>';
      if(tr.cells[5])tr.cells[5].textContent=d1.info;
    }
    /* 卡片7 分区卡片·今日 */
    var crow=document.querySelector('.crow[data-city="'+nm+'"]');
    if(crow){
      var td=crow.querySelector('.today'); if(td)td.innerHTML=d0.info+' <b>'+fmtT(d0.tmax)+'</b>/'+fmtT(d0.tmin);
      var cb=crow.querySelector('.pbar i'); if(cb)cb.style.width=barW(d0.prec)+'%';
    }
    /* 卡片5 双城今日预报 */
    var city=document.querySelector('.city[data-city="'+nm+'"]');
    if(city){var dt=city.querySelector('.dt'); if(dt)dt.setAttribute('data-today',d0.info);}
    /* 地图代表城区（今日预报最高） */
    if(window.__WEATHER_REP&&window.__WEATHER_REP[nm]&&d0.tmax!=null){
      var mk=window.__WEATHER_REP[nm], nowt=(mk._now!=null?mk._now+'℃':'—');
      mk.setStyle({fillColor:colorOf(d0.tmax),radius:radFix(d0.tmax)});
      mk.setPopupContent('<b>'+nm+'</b>（'+mk.region+'）<br/>今日预报最高 <b style="color:#c23b2e">'+d0.tmax+'℃</b><br/>当前实况 '+nowt);
    }
  }
  function refresh(){
    var cities=Object.keys(CODES), i=0, done=0, news={};
    (function next(){
      if(i>=cities.length){if(done>=cities.length){for(var k in news)applyCity(k,news[k].d0,news[k].d1);} return;}
      var j=i++, nm=cities[j];
      fetch('https://www.nmc.cn/rest/weather?stationid='+CODES[nm])
        .then(function(r){return r.json();})
        .then(function(d){
          var det=((d&&d.data&&d.data.predict)||{}).detail||[];
          var fb=rt((((d&&d.data&&d.data.real)||{}).weather||{}).temperature);
          if(det[0]&&det[0].day)news[nm]={d0:dayOf(det[0],fb),d1:dayOf(det[1]||det[0],fb)};
        }).catch(function(){})
        .then(function(){done++;next();});
    })();
  }
  refresh();
  setInterval(refresh,120000);
})();
</script>"""


# ---------- 前端官方预警实时刷新（weather.cma.cn 国家预警信息发布中心，CORS 已开放） ----------
# 周期重拉川/渝两省现行预警，就地重建卡片4横幅/统计/清单/分区标签；失败保留原内容。
LIVE_ALARM_JS = r"""<script>
(function(){
  var LEVELS=['红色','橙色','黄色','蓝色'];
  var CATS=["地质灾害气象","道路结冰","低温雨雪","森林草原火险","森林火险","强对流云团","雷雨大风","沙尘暴","台风","暴雨大风","暴雨","暴雪","寒潮","大风","冰雹","雷电","大雾","高温","干旱","霜冻","山洪","强对流","低温","霾"];
  var RMAP=[["盆西",["成都","雅安","眉山"]],["盆东",["重庆","广安"]],["盆中",["遂宁","南充","资阳"]],["盆北",["广元","绵阳","巴中"]],["川西高原",["甘孜","阿坝","康定","马尔康"]],["川西南",["凉山","西昌","攀枝花"]],["川东北",["达州","巴中"]]];
  function regions(area,prov){
    var out=[];
    for(var i=0;i<RMAP.length;i++){var r=RMAP[i];for(var k=0;k<r[1].length;k++){if(area.indexOf(r[1][k])>=0&&out.indexOf(r[0])<0)out.push(r[0]);}}
    if(prov==='重庆'&&out.indexOf('盆东')<0)out.push('盆东');
    return out;
  }
  function parse(item,prov){
    var text=(item.title||'')+'';
    var level='', cat='其他', area='-';
    for(var i=0;i<LEVELS.length;i++){if(text.indexOf(LEVELS[i])>=0){level=LEVELS[i];break;}}
    for(var i=0;i<CATS.length;i++){if(text.indexOf(CATS[i])>=0){cat=CATS[i];break;}}
    var idx=text.indexOf('气象台发布');
    var area0=(idx>=0?text.slice(0,idx):text).trim();
    area=(area0.replace(/^(四川省|重庆市|重庆)/,'').trim()||area0||'-');
    return{level:level,cat:cat,area:area,prov:prov,regions:regions(text,prov),time:((item.issuetime||'').replace(/\//g,'-'))};
  }
  function fetchAlarm(prov,label){
    return fetch('https://www.nmc.cn/rest/findAlarm?pageNo=1&pageSize=60&signaltype=&signallevel=&province='+encodeURIComponent(prov))
      .then(function(r){return r.json();})
      .then(function(d){var L=((d&&d.data&&d.data.page&&d.data.page.list)||[]);return L.map(function(it){return parse(it,label);});})
      .catch(function(){return null;});
  }
  function refresh(){
    Promise.all([fetchAlarm('四川省','四川'),fetchAlarm('重庆市','重庆')]).then(function(rows){
      var all=[],have=false;
      rows.forEach(function(r){if(r){have=true;all=all.concat(r);}});
      if(!have)return;
      var seen={},uniq=[];
      all.forEach(function(a){var k=a.cat+'|'+a.level+'|'+a.area;if(seen[k])return;seen[k]=1;uniq.push(a);});
      var ord={'红色':0,'橙色':1,'黄色':2,'蓝色':3,'其他':9};
      uniq.sort(function(a,b){return ord[a.level]-ord[b.level];});
      render(uniq);
    }).catch(function(){});
  }
  function render(list){
    var card=document.querySelector('.alarm-banner'); if(!card)card=document.querySelector('.alarm-stats'); if(!card)return;
    card=card.closest('.card'); var sc=0,cq=0,counts={};
    list.forEach(function(a){if(a.prov==='四川')sc++;if(a.prov==='重庆')cq++;counts[a.level]=(counts[a.level]||0)+1;});
    var n=list.length, hi=null;LEVELS.forEach(function(l){if(!hi&&counts[l])hi=l;});
    var bz={'红色':'严重','橙色':'较重','黄色':'注意','蓝色':'一般'};
    var bannerEl=card.querySelector('.alarm-banner');
    if(bannerEl){
      var crit=list.filter(function(a){return(a.level==='红色'||a.level==='橙色')&&a.regions.length;}), hin;
      if(crit.length){
        var rgs={},cats={};crit.forEach(function(a){a.regions.forEach(function(r){rgs[r]=1;});cats[a.cat]=1;});
        hin='<span class="ab-icon">⚠</span><div><b>本报告分区 '+Object.keys(rgs).sort().join('、')+' 现有多条'+(hi||'')+'预警</b><span>类型：'+Object.keys(cats).sort().join('、')+'｜请以属地气象台最新发布为准，红色/橙色预警区域避免高风险活动。</span></div>';
      }else{
        hin='<div><b>当前川渝最高预警级别：'+(hi||'无')+'（'+(hi?bz[hi]:'-')+'）</b><span>生效预警共 '+n+' 条，红橙黄蓝按官方分级，请关注与您所在/前往地区相关条目。</span></div>';
      }
      bannerEl.innerHTML=hin;
    }
    var statsEl=card.querySelector('.alarm-stats');
    if(statsEl){var chips='';LEVELS.forEach(function(l){if(counts[l])chips+='<span class="s-chip c-'+l+'">'+l+' '+counts[l]+'</span>';});
      statsEl.innerHTML='<span class="s-total">四川 '+sc+' 条 · 重庆 '+cq+' 条 · 共 '+n+' 条</span>'+chips;}
    var listEl=card.querySelector('.alarms');
    if(listEl){var rows='';
      list.slice(0,22).forEach(function(a){
        var tags=a.regions.map(function(r){return'<span class="areg">'+r+'</span>';}).join('');
        rows+='<div class="alarm-row"><span class="alv lv-'+(a.level||'其他')+'">'+(a.level||'其他')+'</span><span class="acat">'+a.cat+'</span><span class="aarea">'+a.area+'</span><span class="atime">'+(((a.time||'').slice(5))||'-')+'</span>'+tags+'</div>';
      });
      listEl.innerHTML=rows;}
  }
  refresh();
  setInterval(refresh,300000);
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

/* ===== 吸顶目录导航 & 回到顶部 ===== */
.card{scroll-margin-top:78px}
.sidenav{position:sticky;top:0;z-index:400;background:rgba(16,38,60,.97);
  backdrop-filter:blur(8px);border-bottom:1px solid rgba(255,255,255,.10);
  box-shadow:0 4px 18px rgba(10,28,46,.18)}
.sidenav-inner{max-width:1020px;margin:0 auto;padding:10px 18px;display:flex;gap:8px;
  overflow-x:auto;scrollbar-width:none;white-space:nowrap}
.sidenav-inner::-webkit-scrollbar{display:none}
.snav-item{flex:none;display:inline-flex;align-items:center;gap:7px;color:#cfe2f4;font-size:12.8px;
  padding:6px 13px;border-radius:20px;border:1px solid rgba(255,255,255,.16);cursor:pointer;
  transition:all .18s ease;user-select:none}
.snav-item .n{font-weight:800;color:#7fb6e2;font-size:11.6px;font-variant-numeric:tabular-nums}
.snav-item:hover{background:rgba(255,255,255,.12);color:#fff}
.snav-item.active{background:linear-gradient(135deg,#3d8bc0,#2f7cb6);border-color:transparent;color:#fff;
  box-shadow:0 3px 10px rgba(47,124,182,.35)}
.snav-item.active .n{color:#eaf5ff}
.totop{position:fixed;right:22px;bottom:26px;z-index:700;width:46px;height:46px;border-radius:50%;border:0;
  background:linear-gradient(135deg,var(--navy),var(--navy2));color:#fff;font-size:19px;cursor:pointer;
  box-shadow:0 8px 22px rgba(20,50,79,.38);opacity:0;pointer-events:none;transform:translateY(10px);
  transition:all .26s ease;line-height:1}
.totop.show{opacity:1;pointer-events:auto;transform:none}
.totop:hover{background:var(--accent)}
.snav-more{margin-left:auto;flex:none;display:inline-flex;align-items:center;gap:6px;color:#fff;
  background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.22);font-size:12.8px;
  padding:6px 14px;border-radius:20px;cursor:pointer;transition:all .18s ease;white-space:nowrap}
.snav-more:hover{background:rgba(255,255,255,.24)}
.snav-more .caret{transition:transform .22s ease;font-size:11px}
.snav-more.open .caret{transform:rotate(180deg)}
.drawer-bd{position:fixed;inset:0;background:rgba(10,22,34,.42);opacity:0;pointer-events:none;
  z-index:505;transition:opacity .22s ease}
.drawer-bd.open{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;height:100vh;width:300px;max-width:84vw;z-index:515;
  background:linear-gradient(180deg,#f4f8fc,#fff);box-shadow:-14px 0 44px rgba(10,28,46,.24);
  transform:translateX(103%);transition:transform .28s cubic-bezier(.22,.8,.3,1);
  overflow-y:auto;padding:16px 16px 26px}
.drawer.open{transform:none}
.drawer-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.drawer-head h4{margin:0;font-size:13.5px;color:var(--navy);letter-spacing:1px}
.dclose{width:32px;height:32px;flex:none;border-radius:9px;border:1px solid var(--line);background:#fff;
  color:var(--muted);font-size:18px;line-height:1;cursor:pointer;transition:all .16s ease}
.dclose:hover{background:#eaf2fa;color:var(--navy)}
.d-item{display:flex;align-items:center;gap:10px;padding:11px 12px;margin-bottom:6px;border-radius:11px;
  color:#2c4358;font-size:13.6px;cursor:pointer;border:1px solid transparent;transition:all .16s ease}
.d-item:hover{background:#eaf2fa;border-color:var(--line)}
.d-item .n{width:24px;height:24px;flex:none;border-radius:8px;
  background:linear-gradient(135deg,var(--navy),var(--navy2));color:#fff;font-size:12px;font-weight:700;
  display:inline-flex;align-items:center;justify-content:center}
.d-item.active{background:#fff;border-color:#bfd7ea;color:var(--navy);font-weight:700}
.d-item.active .n{background:linear-gradient(135deg,#3d8bc0,#2f7cb6)}
"""

NAV_JS = r"""<script>
(function(){
  var inner=document.getElementById('snav-inner');
  var cards=[].slice.call(document.querySelectorAll('div.card'));
  if(!inner||!cards.length)return;
  cards.forEach(function(c,i){if(!c.id)c.id='sec'+(i+1);});
  var items=[];
  cards.forEach(function(c){
    var h=c.querySelector('h2');if(!h)return;
    var no=h.querySelector('.no');var n=no?no.textContent.replace(/[^0-9]/g,''):'';
    var t=h.textContent.replace(n,'').trim()||('板块'+n);
    items.push({el:c,n:n,t:t});
  });
  var PRIMARY=6;
  function mkTop(it){
    var a=document.createElement('a');a.className='snav-item';a.href='#'+it.el.id;
    a.innerHTML=(it.n?'<span class="n">'+it.n+'</span>':'')+'<span>'+it.t+'</span>';
    a.addEventListener('click',function(e){e.preventDefault();go(it.el);setActive(it.el);});
    return a;
  }
  function mkDraw(it){
    var d=document.createElement('a');d.className='d-item';d.href='#'+it.el.id;
    d.innerHTML='<span class="n">'+it.n+'</span>'+it.t;
    d.addEventListener('click',function(e){e.preventDefault();go(it.el);setActive(it.el);closeDrawer();});
    return d;
  }
  // 顶部：前 6 个主板块
  items.slice(0,PRIMARY).forEach(function(it){
    it.a=mkTop(it);inner.appendChild(it.a);
  });
  // “更多板块”按钮 + 侧边抽屉
  var more=document.createElement('button');more.type='button';more.className='snav-more';
  more.innerHTML='更多板块<span class="caret">▾</span>';inner.appendChild(more);
  var dbd=document.createElement('div');dbd.className='drawer-bd';
  var dr=document.createElement('div');dr.className='drawer';
  dr.innerHTML='<div class="drawer-head"><h4>全部板块</h4><button type="button" class="dclose" aria-label="关闭">&times;</button></div><div class="drawer-list"></div>';
  document.body.appendChild(dbd);document.body.appendChild(dr);
  var dlst=dr.querySelector('.drawer-list');
  items.slice(PRIMARY).forEach(function(it){it.d=mkDraw(it);dlst.appendChild(it.d);});
  function go(el){window.scrollTo({top:el.getBoundingClientRect().top+window.pageYOffset-66,behavior:'smooth'});}
  function setActive(el){items.forEach(function(it){
    if(it.a)it.a.classList.toggle('active',it.el===el);
    if(it.d)it.d.classList.toggle('active',it.el===el);
  });}
  function spy(){var o=78,cur=null;cards.forEach(function(c){if(c.getBoundingClientRect().top<=o)cur=c;});setActive(cur);}
  window.addEventListener('scroll',spy,{passive:true});spy();
  function openDrawer(){more.classList.add('open');dbd.classList.add('open');dr.classList.add('open');document.body.style.overflow='hidden';}
  function closeDrawer(){more.classList.remove('open');dbd.classList.remove('open');dr.classList.remove('open');document.body.style.overflow='';}
  more.addEventListener('click',function(){if(dr.classList.contains('open'))closeDrawer();else openDrawer();});
  dr.querySelector('.dclose').addEventListener('click',closeDrawer);
  dbd.addEventListener('click',closeDrawer);
  document.addEventListener('keydown',function(e){if(e.key==='Escape')closeDrawer();});
  var tb=document.getElementById('totop');if(tb){
    function onSc(){tb.classList.toggle('show',window.pageYOffset>480);}
    window.addEventListener('scroll',onSc,{passive:true});onSc();
    tb.addEventListener('click',function(){window.scrollTo({top:0,behavior:'smooth'});});
  }
})();
</script>"""


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
def render(fetched, ventusky_paths, nmc_charts, narr, site_dir, generated, alarms=None, alarm_cnt=None, stations=None):
    ts = generated.strftime("%Y-%m-%d %H:%M")
    dstr = generated.strftime("%m月%d日")
    publish = next((c["publish"] for c in fetched.values() if c.get("publish")), "-")

    # 官方预警卡片（插入为卡片 4）
    alarm_html = build_alarm_card(alarms or [], alarm_cnt or {})
    # 川渝全站点实况 · 地图（插入为卡片 6）
    map_html = build_map_card(fetched, stations or [])
    # 前端实时预报刷新所需的 城市→站点代码 映射（来自实际抓取到的 fetched；无代码的城市自动跳过）
    live_codes = {nm: c.get("code") for nm, c in fetched.items() if c.get("code")}

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
            crows += ("<div class='crow' data-city='{nm}'><div class='cn'>{nm}<span class='cb' style='{cls}'>{bl}</span></div>"
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
            rows += ("<tr data-city='{nm}'><td>{r}</td><td><b>{nm}</b></td>"
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

    narr_block = ""

    try:
        ml_html = build_multilevel()
    except Exception:
        ml_html = ("<div class='card'><h2><span class='no'>11</span>多层次高空与地面形势研读（SFC~100hPa）</h2>"
                   "<div class='sec-sub'>本卡生成失败，请下次构建重试</div></div>")

    html = """<!DOCTYPE html>
   <html lang="zh">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>川渝天气展望 · 分区风险分析报告 {d}</title><style>{css}</style></head>
<body>
<nav id="snav" class="sidenav" aria-label="板块导航"><div class="sidenav-inner" id="snav-inner"></div></nav>
<div class="wrap">
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

{ml_html}

<div class="card"><h2><span class="no">12</span>关注与提示</h2>
<div class="sec-sub">按分区风险生成，红标为首要关注</div>
<div class="foc">{foc_html}</div>
{narr_block}</div>

<footer>
  <div class="foot">本页由 GitHub Actions 定时自动生成 · 数据来自中央气象台 NMC（含官方预报图）{vent_note} · 未经人工审核，仅供参考<br/>
  灾害性天气请以属地气象部门发布的预报预警为准。实况/预报与官方预警在页面加载后持续自动向 NMC、国家预警中心实时追更。</div>
 </footer>
 {live_js}
 {live_forecast_js}
 {live_alarm_js}
 {nav_js}
 <button class="totop" id="totop" aria-label="回到顶部">↑</button>
 </body></html>""".format(
        css=CSS, d=dstr, pub=publish, gen=ts, syn=syn, now_cards=now_cards,
        nmc_html=nmc_html, expert_html=expert_html, alarm_html=alarm_html,
        map_html=map_html, regions_html=regions_html,
        rows=rows, proc_html=proc_html, foc_html=foc_html,
        vent=vent, narr_block=narr_block, ml_html=ml_html,
        live_js=LIVE_JS,
        live_forecast_js=LIVE_FORECAST_JS.replace("__LIVE_CODES__", json.dumps(live_codes, ensure_ascii=False)),
        live_alarm_js=LIVE_ALARM_JS,
        nav_js=NAV_JS,
        vent_note=(" / Ventusky" if ventusky_paths else ""))

    os.makedirs(site_dir, exist_ok=True)
    out = os.path.join(site_dir, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成:", out)
    return out
  # 已按需求移除卡片11的"未启用 LLM 润色…"说明段落


# ---------- 新增：多层次高空与地面形势研读（官方实况分析图 × 公开多层格点读场） ----------
ML_STATIONS = [["成都", 30.67, 104.07], ["雅安", 30.01, 103.00], ["广元", 32.43, 105.84],
               ["南充", 30.80, 106.08], ["重庆", 29.56, 106.55], ["达州", 31.21, 107.47],
               ["康定", 30.00, 101.96], ["西昌", 27.90, 102.27]]
ML_LV = [925, 850, 700, 500, 200, 100]


# ---------- 新增：区域加密格点 → 槽/脊/高低压/切变线/锋面等天气系统粗识别 ----------
# 在代表站之外再布设覆盖川渝及周边的中尺度矩形格点，读取海平面气压、850/700/500hPa 位势高度、
# 850hPa 温度、925/850hPa 风场；据此用空间场（局地极值、梯度、风向辐合）启发式识别天气系统。
ML_SYS_LAT = [27.0, 29.0, 31.0, 33.0, 35.0]
ML_SYS_LON = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0]
_SYSH = [("mlp", "pressure_msl"), ("H500", "geopotential_height_500hPa"),
         ("H700", "geopotential_height_700hPa"), ("H850", "geopotential_height_850hPa"),
         ("T850", "temperature_850hPa"), ("WS925", "wind_speed_925hPa"),
         ("WD925", "wind_direction_925hPa"), ("WS850", "wind_speed_850hPa"),
         ("WD850", "wind_direction_850hPa")]


def _sys_grid():
    """拉取区域加密格点的空间读场，返回 (F, ok)。F 为 {key: [[..]] 2D}，ok 为成功格点数。"""
    nla, nlo = len(ML_SYS_LAT), len(ML_SYS_LON)

    def mk():
        return [[None] * nlo for _ in range(nla)]

    F = dict((k, mk()) for k, _ in _SYSH)
    F["_ele"] = mk()
    hv = [h for _, h in _SYSH]

    def cell(i, j):
        q = urllib.parse.urlencode({
            "latitude": "%.2f" % ML_SYS_LAT[i], "longitude": "%.2f" % ML_SYS_LON[j],
            "hourly": ",".join(hv), "pressure_level": "500,700,850,925",
            "forecast_hours": "2", "timezone": "Asia/Shanghai"})
        d = json.loads(http_get("https://api.open-meteo.com/v1/forecast?" + q, timeout=20).decode())
        ho = d.get("hourly") or {}
        out = {"elev": float(d["elevation"]) if d.get("elevation") is not None else 0.0}
        for hk in hv:
            a = ho.get(hk) or []
            out[hk] = (float(a[0]) if a and a[0] is not None else None)
        return out

    cells = [(i, j) for i in range(nla) for j in range(nlo)]
    ok = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        mp = {ex.submit(cell, i, j): (i, j) for i, j in cells}
        for fut in as_completed(mp):
            i, j = mp[fut]
            try:
                d = fut.result()
            except Exception:
                continue
            for k, hk in _SYSH:
                if d.get(hk) is not None:
                    F[k][i][j] = d[hk]
            F["_ele"][i][j] = d.get("elev", 0.0)
            ok += 1
    return (F, ok) if ok >= 12 else (None, ok)


def _wind_name(deg):
    names = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
    return names[int((deg % 360 + 22.5) // 45) % 8] + "风"


def _circ_mean(deg_list):
    if not deg_list:
        return None
    x = sum(math.cos(math.radians(d)) for d in deg_list)
    y = sum(math.sin(math.radians(d)) for d in deg_list)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _ang_diff(a, b):
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d


def _pos(la, lo):
    """相对盆地中心(30.5N,105E)的粗略方位名。"""
    ang = (math.degrees(math.atan2(lo - 105.0, la - 30.5)) + 360) % 360
    return ["正北", "东北", "正东", "东南", "正南", "西南", "正西", "西北"][int((ang + 22.5) // 45) % 8]


def _analyze_weather_systems():
    """对加密格点做天气系统粗识别，返回 {key: html片段}；格点不足时返回空 dict。"""
    F, ok = _sys_grid()
    if F is None:
        return {}
    nla, nlo = len(ML_SYS_LAT), len(ML_SYS_LON)
    out = {}

    def m2(arr):
        v = [x for row in arr for x in row if x is not None]
        return (sum(v) / len(v)) if v else None

    def reg(arr, ir, jr):
        v = [arr[i][j] for i in ir for j in jr if arr[i][j] is not None]
        return (sum(v) / len(v)) if v else None

    def localmin(arr, i, j):
        if arr[i][j] is None:
            return False
        v = arr[i][j]
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                if di == 0 and dj == 0:
                    continue
                ni, nj = i + di, j + dj
                if 0 <= ni < nla and 0 <= nj < nlo and arr[ni][nj] is not None and arr[ni][nj] <= v:
                    return False
        return True

    def localmax(arr, i, j):
        if arr[i][j] is None:
            return False
        v = arr[i][j]
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                if di == 0 and dj == 0:
                    continue
                ni, nj = i + di, j + dj
                if 0 <= ni < nla and 0 <= nj < nlo and arr[ni][nj] is not None and arr[ni][nj] >= v:
                    return False
        return True

    # ---- 地面（海平面）气压系统：闭合低压 / 高压中心（仅低海拔格点，规避高原气压还原假高压） ----
    ele = F.get("_ele")

    def is_lowland(i, j):
        # 850hPa 已被格点遮蔽（高原无850层）则视为非盆地格点，不用作地面气压判据
        if F["T850"][i][j] is None:
            return False
        return (ele is None) or (ele[i][j] <= 1200)

    if m2(F["mlp"]) is not None:
        pts = [(i, j, F["mlp"][i][j]) for i in range(nla) for j in range(nlo)
               if F["mlp"][i][j] is not None and is_lowland(i, j)]
        if len(pts) >= 6:
            pmin = min(p[2] for p in pts); pmax = max(p[2] for p in pts); pmean = sum(p[2] for p in pts) / len(pts)
            lows = [(i, j, F["mlp"][i][j]) for i in range(nla) for j in range(nlo) if F["mlp"][i][j] is not None and is_lowland(i, j) and localmin(F["mlp"], i, j)]
            highs = [(i, j, F["mlp"][i][j]) for i in range(nla) for j in range(nlo) if F["mlp"][i][j] is not None and is_lowland(i, j) and localmax(F["mlp"], i, j)]
            lows = [p for p in lows if p[2] < pmean - 2]
            highs = [p for p in highs if p[2] > pmean + 2]
            tx = "海平面气压区域平均约 <b>%d hPa</b>（%d～%d hPa）。" % (pmean, pmin, pmax)
            if lows:
                i, j, v = min(lows, key=lambda p: p[2])
                tx += "格网内存在<b>闭合低压</b>中心（约 <b>%d hPa</b>，位于盆地%s），低层辐合利于降水或对流。" % (v, _pos(ML_SYS_LAT[i], ML_SYS_LON[j]))
            else:
                tx += "未见明显闭合低压中心，地面气压梯度较缓，近地面辐合抬升条件一般。"
            if highs:
                i, j, v = max(highs, key=lambda p: p[2])
                tx += "同时在盆地%s存在<b>闭合高压</b>中心（约 <b>%d hPa</b>），近地面以下沉、晴稳为主。" % (_pos(ML_SYS_LAT[i], ML_SYS_LON[j]), v)
            out["sfc"] = tx

    # ---- 500 hPa：槽 / 脊 / 高/低压 + 副高(5880领域范围) ----
    if m2(F["H500"]) is not None:
        Hc = reg(F["H500"], [1, 2], [2, 3]) or 0
        NW = reg(F["H500"], [2, 3, 4], [0, 1, 2]); NE = reg(F["H500"], [2, 3, 4], [3, 4, 5])
        SW = reg(F["H500"], [0, 1], [0, 1, 2]); SE = reg(F["H500"], [0, 1], [3, 4, 5])
        E = (NE + SE) / 2 if (NE is not None and SE is not None) else None
        W = (NW + SW) / 2 if (NW is not None and SW is not None) else None
        N = (NW + NE) / 2 if (NW is not None and NE is not None) else None
        S = (SW + SE) / 2 if (SW is not None and SE is not None) else None
        hi = [v for v in (NW, NE, SW, SE) if v is not None]
        mx = max(hi) if hi else None; mn = min(hi) if hi else None
        hp = [F["H500"][i][j] for i in range(nla) for j in range(nlo) if F["H500"][i][j] is not None]
        vcnt = len(hp)
        frac = (sum(1 for x in hp if x >= 5880) / vcnt * 100) if vcnt else 0
        if frac >= 70:
            tp = "受<b>大范围高压（副高/大陆高压）</b>面状控制，≥5880线覆盖约 <b>%d%%</b>，局地槽脊不显著" % frac
            note = ""
        elif mx is not None and Hc - mx >= 60:
            tp = "盆地500hPa偏<b>高值，受高压脊/高压区控制</b>"
            note = ("区域约 <b>%d%%</b> 格点≥5880线，副高对盆地有明显控制影响。" % frac) if frac else ""
        elif mn is not None and mn - Hc >= 60:
            tp = "盆地500hPa处于<b>低值（低槽/低值区）</b>"
            note = ("副高5880线主体偏东南退，仅约 <b>%d%%</b> 格点≥5880线。" % frac) if frac < 40 else ""
        elif W is not None and E is not None and W - E >= 60:
            tp = "高度<b>西高东低</b>，盆地处于<b>槽前</b>西南暖湿气流区"
            note = ("约 <b>%d%%</b> 格点≥5880线，副高5880线大致在盆地边缘附近。" % frac) if frac < 40 else ""
        elif E is not None and W is not None and E - W >= 60:
            tp = "高度<b>东高西低</b>，盆地处于<b>槽后/高压脊前</b>西北气流区"
            note = ""
        elif N is not None and S is not None and N - S >= 60:
            tp = "高度<b>北高南低</b>，呈典型西风带纬向引导"
            note = ""
        else:
            tp = "环流<b>较平直</b>，无明显强槽脊"
            note = ("盆地未明显落入5880线所围副高范围。" if frac < 10 else ("约 <b>%d%%</b> 格点≥5880线，副高5880线紧邻或逼近盆地。" % frac))
        tx = "盆地500hPa位势高度平均约 <b>%d gpm</b>，判读：%s。" % (Hc, tp)
        if note:
            tx += note
        out["h500"] = tx

    # ---- 700 hPa：低涡 / 切变 / 中空暖脊 ----
    if m2(F["H700"]) is not None:
        Hc7 = reg(F["H700"], [1, 2], [2, 3])
        o7 = []
        for ir, jr in (([0, 1], [0, 1, 2]), ([0, 1], [3, 4, 5]), ([2, 3, 4], [0, 1, 2]), ([2, 3, 4], [3, 4, 5])):
            v = reg(F["H700"], ir, jr)
            if v is not None:
                o7.append(v)
        if o7 and Hc7 is not None:
            if min(o7) - Hc7 >= 45:
                out["h700"] = "700hPa盆地为<b>低值（低涡/切变）中心</b>，中心高度约 <b>%d gpm</b>，低层辐合显著，是本站区域降水/对流的动力系统。" % Hc7
            elif Hc7 - max(o7) >= 45:
                out["h700"] = "700hPa盆地偏<b>高值（中空暖脊/高压）</b>，高度约 <b>%d gpm</b>，中低空以下沉、晴稳为主。" % Hc7
            else:
                out["h700"] = "700hPa位势高度在盆地约 <b>%d gpm</b>，与四周差异不大，无显著低涡或暖脊特征。" % Hc7

    # ---- 850 hPa 温度锋面 ----
    if m2(F["T850"]) is not None:
        TN = reg(F["T850"], [2, 3, 4], range(nlo)); TS = reg(F["T850"], [0, 1], range(nlo))
        if TN is not None and TS is not None:
            dT = TS - TN
            frow = None
            best = -1
            for i in range(1, nla):
                r = [F["T850"][i][j] - F["T850"][i - 1][j] for j in range(nlo)
                     if F["T850"][i][j] is not None and F["T850"][i - 1][j] is not None]
                if r:
                    m = abs(sum(r) / len(r))
                    if m > best:
                        best = m; frow = i
            n_wd = [F["WD850"][i][j] for i in range(2, nla) for j in range(nlo) if F["WD850"][i][j] is not None]
            s_wd = [F["WD850"][i][j] for i in range(2) for j in range(nlo) if F["WD850"][i][j] is not None]
            tx = "850hPa平均温度<b>北约%.1f℃、南约%.1f℃</b>，南北温差约 <b>%.1f℃</b>。" % (TN, TS, dT)
            if dT >= 8:
                parts = ["存在<b>明显温度锋区</b>，锋面大致位于约 %d°N 一线" % ML_SYS_LAT[frow]]
                if n_wd:
                    nw = _circ_mean(n_wd)
                    if nw is not None and (270 <= nw <= 360 or 0 <= nw <= 90):
                        parts.append("锋后（北侧）为%s（%d°），属<b>冷锋/冷空气南下</b>配置，易激发降温与强对流" % (_wind_name(nw), nw))
                if s_wd:
                    sw = _circ_mean(s_wd)
                    if sw is not None and 180 <= sw <= 270:
                        parts.append("锋前（南侧）为%s暖湿输送，供需条件较好" % _wind_name(sw))
                tx += "，".join(parts) + "。"
            else:
                tx += "南北温差不大，<b>无明显锋面</b>，850hPa温度场较均匀。"
            out["front"] = tx

    # ---- 850 hPa 切变线（风向辐合） ----
    if m2(F["WD850"]) is not None:
        Wd = [F["WD850"][i][j] for i in (1, 2) for j in (0, 1, 2) if F["WD850"][i][j] is not None]
        Ed = [F["WD850"][i][j] for i in (1, 2) for j in (3, 4, 5) if F["WD850"][i][j] is not None]
        Sd = [F["WD850"][i][j] for i in (0, 1) for j in range(nlo) if F["WD850"][i][j] is not None]
        Nd = [F["WD850"][i][j] for i in (3, 4) for j in range(nlo) if F["WD850"][i][j] is not None]
        parts = []
        if Wd and Ed:
            wm, em = _circ_mean(Wd), _circ_mean(Ed)
            if wm is not None and em is not None and _ang_diff(wm, em) >= 120:
                parts.append("850hPa盆地<b>东西两侧风向辐合</b>（西侧%s、东侧%s），存在<b>切变/辐合线</b>，大致沿105°E一线，是触发带状降水的关键系统" % (_wind_name(wm), _wind_name(em)))
        if Sd and Nd:
            sm, nm = _circ_mean(Sd), _circ_mean(Nd)
            if sm is not None and nm is not None and _ang_diff(sm, nm) >= 120:
                parts.append("南北风向<b>切变</b>明显（南侧%s、北侧%s），利于低层辐合抬升与对流组织化" % (_wind_name(sm), _wind_name(nm)))
        out["shear"] = ("，".join(parts) + "。") if parts else "850 hPa 环流风向过渡较平缓，<b>未见明显切变线</b>，辐合抬升条件一般。"
    return out


def build_multilevel():
    """多层次高空与地面形势研读：A.官方NMC实况分析图(SFC~100hPa)；B.公开多层格点读场(7-8代表站区域平均)。"""
    # ---- A. 官方高空实况分析图：SFC/925/850/700/500/200/100 ----
    tabs, panes, cap_ok = "", "", 0
    pages = [("h000", "地面(SFC)·海平面气压", "SFC"), ("h925", "925 hPa", "925"),
             ("h850", "850 hPa", "850"), ("h700", "700 hPa", "700"), ("h500", "500 hPa", "500"),
             ("h200", "200 hPa", "200"), ("h100", "100 hPa", "100")]

    def _chart(i, pg, lbl, short):
        try:
            h = http_get("http://www.nmc.cn/publish/observations/china/dm/weatherchart-%s.htm" % pg, timeout=10).decode("utf-8", "ignore")
            m = re.search(r'id=imgpath[^>]*\bdata-time="([^"]*)"[^>]*\bsrc="(https://[^"]*)"', h)
            if not m:
                return None
            tm = m.group(1).strip()
            u = m.group(2).split("?")[0]
            data = http_get(u, timeout=18)
            if not data or len(data) < 3000:
                return None
            data = _compress_jpg(data, 900, 72)
            return (i, short, lbl, tm, base64.b64encode(data).decode())
        except Exception:
            return None

    got = []
    with ThreadPoolExecutor(max_workers=7) as ex:
        futs = [ex.submit(_chart, i, pg, lbl, short) for i, (pg, lbl, short) in enumerate(pages)]
        for f in as_completed(futs):
            r = f.result()
            if r:
                got.append(r)
    got.sort()
    tabs, panes, cap_ok = "", "", 0
    for r in got:
        i, short, lbl, tm, b64 = r
        act = " active" if i == 0 else ""
        tabs += "<button class='ml-tab%s' data-lv='%d'>%s</button>" % (act, i, short)
        src_attr = "src='data:image/jpeg;base64,%s'" % b64
        if i != 0:
            src_attr = "data-src='data:image/jpeg;base64,%s'" % b64
        panes += ("<div class='ml-pane%s' data-lv='%d'><img %s loading='lazy' decoding='async'/>"
                  "<div class='ml-cap'>%s · 北京时 %s</div></div>") % (act, i, src_attr, lbl, tm)
        cap_ok += 1
    figs = ("<div class='ml-tabs'>%s</div><div class='ml-view'>%s</div>" % (tabs, panes)) if cap_ok else ""

    # ---- B. 公开多层格点读场：逐层区域平均（气温/位势高度/风/相对湿度） ----
    acc = {L: {"T": [], "RH": [], "H": [], "WS": [], "WD": []} for L in ML_LV}
    omit = []

    def _grid(la, lo):
        try:
            varlist = []
            for L in ML_LV:
                varlist += ["temperature_%shPa" % L, "relative_humidity_%shPa" % L,
                            "geopotential_height_%shPa" % L, "wind_speed_%shPa" % L, "wind_direction_%shPa" % L]
            q = urllib.parse.urlencode({"latitude": "%.3f" % la, "longitude": "%.3f" % lo,
                                        "hourly": ",".join(varlist), "pressure_level": ",".join(map(str, ML_LV)),
                                        "forecast_hours": "2", "timezone": "Asia/Shanghai"})
            d = json.loads(http_get("https://api.open-meteo.com/v1/forecast?" + q, timeout=18).decode())
            return d.get("hourly") or {}
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_grid, la, lo): nm for nm, la, lo in ML_STATIONS}
        for f in as_completed(futs):
            nm = futs[f]
            ho = f.result()
            if ho is None:
                omit.append(nm)
                continue
            for L in ML_LV:
                for k, key in (("T", "temperature_%shPa" % L), ("RH", "relative_humidity_%shPa" % L),
                               ("H", "geopotential_height_%shPa" % L), ("WS", "wind_speed_%shPa" % L),
                               ("WD", "wind_direction_%shPa" % L)):
                    arr = ho.get(key) or []
                    if arr and arr[0] is not None:
                        acc[L][k].append(float(arr[0]))

    def M(L, k):
        V = [x for x in acc.get(L, {}).get(k, []) if x is not None]
        return (sum(V) / len(V)) if V else None

    def Blk(t, txt):
        return "<div class='ml-blk'><b>%s</b><p>%s</p></div>" % (t, txt)

    t925, t850, t700, t500, t200, t100 = (M(925, 'T'), M(850, 'T'), M(700, 'T'), M(500, 'T'), M(200, 'T'), M(100, 'T'))
    h925, h850, h700, h500, h200, h100 = (M(925, 'H'), M(850, 'H'), M(700, 'H'), M(500, 'H'), M(200, 'H'), M(100, 'H'))
    rh850, rh700, rh500 = M(850, 'RH'), M(700, 'RH'), M(500, 'RH')
    ws925, ws500, ws200 = M(925, 'WS'), M(500, 'WS'), M(200, 'WS')
    wd850, wd925 = M(850, 'WD'), M(925, 'WD')

    ms = lambda v: ("%.0f" % (v / 3.6)) if v is not None else "-"
    read = []
    if h500 is None:
        read.append(Blk("读场失败", "公开多层格点暂不可用，本卡仅展示官方实况分析图。"))
    else:
        # 副高（500hPa位势高度 5880m 判据）
        if h500 >= 5920:
            sub = "盆地500hPa位势高度平均约 <b>%d gpm</b>，明显高于5880线，受强副热带高压（或大陆高压）控制，中高空下沉增温显著，利于持续晴热、抑制对流。" % h500
        elif h500 >= 5880:
            sub = "盆地500hPa位势高度平均约 <b>%d gpm</b>，位于5880线附近及以上，副热带高压对盆地有控制或边缘影响，是高温与少雨的主导系统。" % h500
        elif h500 >= 5850:
            sub = "盆地500hPa位势高度平均约 <b>%d gpm</b>，接近5880线下沿，副高偏弱或偏东，盆地多处于副高边缘，午后局地对流易发。" % h500
        else:
            sub = "盆地500hPa位势高度平均约 <b>%d gpm</b>，明显低于5880线，副高主体偏东南退，盆地受西风带短波槽/切变影响更大，降水过程增多。" % h500
        read.append(Blk("500hPa位势高度 · 环流主导", sub))
        # 850/700 温湿
        if t850 is not None:
            if rh700 is not None and rh700 >= 80:
                wet = "700hPa平均相对湿度约 <b>%d%%</b>，配合850hPa约 <b>%d%%</b>，湿层深厚，水汽条件充沛。" % (rh700, rh850 if rh850 is not None else 0)
            elif rh850 is not None and rh850 >= 80 and rh700 is not None and rh700 < 60:
                wet = "850hPa相对湿度约 <b>%d%%</b> 高湿而700hPa约 <b>%d%%</b> 偏干，存在中空干层，易触发短时强降水/冰雹等强对流。" % (rh850, rh700)
            else:
                wet = "850hPa平均相对湿度约 <b>%s%%</b>、700hPa约 <b>%s%%</b>，水汽条件总体一般。" % (("-" if rh850 is None else "%d" % rh850), ("-" if rh700 is None else "%d" % rh700))
            ht = "中低层850hPa平均气温约 <b>%d℃</b>、700hPa约 <b>%d℃</b>。" % (t850, t700) if t700 is not None else "中低层850hPa平均气温约 <b>%d℃</b>。" % t850
            read.append(Blk("850/700hPa 温度与水汽层结", ht + wet))
        # 风场：低空急流与水汽输送
        if ws925 is not None and ws500 is not None:
            dir_txt = ""
            if wd925 is not None:
                if 180 <= wd925 < 270:
                    dir_txt = "925hPa为<b>西南风</b>（约占平均风向%d°），利于从南海—孟加拉湾向盆地输送暖湿水汽。" % wd925
                elif wd925 < 90 or wd925 >= 315:
                    dir_txt = "925hPa为<b>偏北/东北风</b>（平均风向约%d°），水汽输送偏弱，利于干冷空气南下。" % wd925
                else:
                    dir_txt = "925hPa平均风向约<b>%d°</b>（东南-南风），水汽输送一般。" % wd925
            ws = ms(ws925)
            if ws925 >= 43:  # 12 m/s
                we = "925hPa平均风速约 <b>%s m/s</b>，达到<b>低空急流</b>量级，水汽与动量输送显著增强，低层辐合有利抬升。" % ws
            else:
                we = "925hPa平均风速约 <b>%s m/s</b>，低空急流不明显。" % ws
            up = "500hPa平均风速约 <b>%s m/s</b>。" % ms(ws500)
            read.append(Blk("风场 · 低空急流与水汽输送", dir_txt + we + up))
        # 高空急流与辐散
        if ws200 is not None:
            ws2 = ms(ws200)
            if ws200 >= 108:  # 30 m/s
                div = "200hPa平均风速约 <b>%s m/s</b>，高空西风急流强盛，高空辐散明显，与低层辐合配合则垂直上升运动强、利于中尺度系统发展。" % ws2
            else:
                div = "200hPa平均风速约 <b>%s m/s</b>，高空辐散一般。" % ws2
            read.append(Blk("200hPa 高空急流与辐散", div))
        # 对流稳定度
        if t850 is not None and t500 is not None:
            dT = t850 - t500
            if dT >= 28:
                st = "850-500hPa温差约 <b>%d℃</b>，层结<b>极不稳定</b>，配合触发机制极易出现强对流。" % dT
            elif dT >= 24:
                st = "850-500hPa温差约 <b>%d℃</b>，层结<b>较不稳定</b>，午后热力对流潜势较高。" % dT
            else:
                st = "850-500hPa温差约 <b>%d℃</b>，层结相对稳定，对流潜势一般。" % dT
            read.append(Blk("垂直稳定度（850-500hPa）", st))
        # 分区研判
        parts = []
        parts.append("盆西（成都-雅安）沿山若850hPa为西南风且水汽充沛，叠加不稳定层结，午后到夜间局地对流与沿山降水风险偏高；")
        if h500 is not None and h500 >= 5880:
            parts.append("盆东（重庆-达州）500hPa高度偏高，中低空多为暖脊控制，持续晴热、高温伏旱风险为主；")
        else:
            parts.append("盆东（重庆-达州）若处于副高边缘或切变南侧，仍以闷热与局地雷阵雨为主；")
        parts.append("川西高原及川西南山地受地形抬升影响，当低层偏南风＋中空较干时，午后局地强对流（短时强降雨/雷暴）需关注。")
        read.append(Blk("川渝分区研判", "".join(parts)))
        if omit:
            read.append(Blk("说明", "以下代表站读场未取到：" + "、".join(omit) + "，结果以其余站点区域平均为准。"))

    # ---- C. 天气系统粗识别：槽/脊/高低压/切变线/锋面（区域加密格点空间场） ----
    sys_read = ""
    try:
        sysd = _analyze_weather_systems()
    except Exception:
        sysd = {}
    if sysd:
        order = [("sfc", "地面气压系统（海平面气压场）"),
                 ("h500", "500 hPa 高低压与槽脊（环流型考察）"),
                 ("h700", "700 hPa 低涡 / 暖脊"),
                 ("front", "850 hPa 温度锋面"),
                 ("shear", "850 hPa 切变线")]
        cls = []
        for k, t in order:
            if k in sysd:
                cls.append(Blk(t, sysd[k]))
        head = ("<div class='ml-blk'><b>天气系统识别（加密格点空间场粗判）</b>"
                "<p>基于川渝及周边 %d×%d 格点的空间读场，对<b>槽、脊、高/低压、切变线、锋面</b>作启发式识别，"
                "供与上方 NMC 官方实况分析图横向对照（结果随加密格点质量波动，供参考）。</p></div>"
                % (len(ML_SYS_LAT), len(ML_SYS_LON)))
        sys_read = head + "".join(cls)

    # ---- 0. 依据实时天气图 + 格点场 综合研判（置顶）：优先视觉LLM读官方实况图 ----
    top_read = ""
    if cap_ok:
        cmap = {short: (lbl, tm, b64) for (i, short, lbl, tm, b64) in got}
        pick = [("SFC", "地面海平面气压"), ("925", "925 hPa"), ("850", "850 hPa"),
                ("700", "700 hPa"), ("500", "500 hPa")]
        sel = [(cmap[s][0], cmap[s][2]) for s, _ in pick if s in cmap]
        vis = _visual_review_charts(sel) if sel else None
        if vis:
            labels = "、".join("[%s]" % t for s, t in pick if s in cmap)
            top_read = Blk("实时天气图综合研判",
                           vis + "<span style='color:#8a99aa;font-size:12px'>（依据中央气象台官方实况分析图%s）</span>" % labels)
        else:
            anchor = "、".join(t for s, t in pick if s in cmap) or "实况图"
            core = ""
            if h500 is not None:
                core = "中空500hPa平均高度约 <b>%d gpm</b>" % h500
            if sysd:
                s = (sysd.get("sfc", "").strip() + "；" if "sfc" in sysd else "") + sysd.get("h500", "").strip()
                core = (core + "；" if core else "") + "加密格点空间场识别：" + s[:180]
            if core:
                top_read = Blk("实时天气图综合研判",
                               "本研判以中央气象台官方【实时】实况分析图（%s）为锚，叠加多层格点与系统识别综合给出：%s。" % (anchor, core))
            else:
                top_read = Blk("实时天气图综合研判",
                               "本研判以中央气象台官方【实时】实况分析图（%s）为锚定性给出；当前多层格点读场暂不可用，天气系统详情请以上方官方实况图为准。" % anchor)
    read_html = "<div class='ml-read'>%s%s%s</div>" % (top_read, sys_read, "".join(read))

    return """<div class="card" id="mlcard"><h2><span class="no">11</span>多层次高空与地面形势研读（SFC~100hPa）</h2>
<div class="sec-sub">NMC官方实况分析图 × 公开多层格点读场 · 自下而上解读川渝垂直配置，并识别槽/脊/高/低压/切变线/锋面</div>
__MLREAD____MLFIGS__
<style>
.ml-read{margin:6px 0 14px}
.ml-blk{padding:7px 0;border-bottom:1px dashed #e2e8ee}
.ml-blk b{color:#c23b2e;font-size:13px;display:block;margin-bottom:3px}
.ml-blk p{margin:0;line-height:1.75;color:#31455c;font-size:13.5px}
.ml-tabs{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 8px}
.ml-tab{border:1px solid #c9d6e2;background:#fff;color:#41556a;border-radius:18px;padding:5px 13px;cursor:pointer;font-size:12.5px}
.ml-tab.active{background:#12395b;border-color:#12395b;color:#fff}
.ml-view{background:#e9edf2;border:1px solid #e3eaf0;border-radius:10px;padding:8px;overflow:hidden}
.ml-pane{display:none}
.ml-pane.active{display:block}
.ml-pane img{width:100%;display:block;border-radius:6px}
.ml-cap{font-size:12px;color:#7b8ca0;padding:6px 2px 2px;text-align:center}
</style>
<script>(function(){var c=document.getElementById('mlcard');if(!c)return;var b=c.querySelectorAll('.ml-tab'),p=c.querySelectorAll('.ml-pane');
function sel(btn){var i=btn.getAttribute('data-lv');for(var j=0;j<b.length;j++)b[j].classList.toggle('active',b[j]===btn);for(var j=0;j<p.length;j++){var on=(p[j].getAttribute('data-lv')===i);p[j].classList.toggle('active',on);if(on){var im=p[j].querySelector('img');if(im&&!im.getAttribute('src')&&im.getAttribute('data-src')){im.setAttribute('src',im.getAttribute('data-src'));im.removeAttribute('loading');}}}}
for(var i=0;i<b.length;i++)b[i].onclick=(function(btn){return function(){sel(btn);};})(b[i]);})();</script>
</div>""".replace("__MLREAD__", read_html).replace("__MLFIGS__", figs)



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

    stations = build_stations(args.out)

    narr = llm_summary(fetched) if args.llm else None
    if not narr:
        narr = "（未启用 LLM 润色，采用上方规则化形势概览与分区风险，数据取官方实时。）"

    render(fetched, vent, nmc_charts, narr, args.out, datetime.now(), alarms, alarm_cnt, stations)


if __name__ == "__main__":
    main()