"""通用闲聊与实时信息兜底（不依赖大模型也能答得出）。

设计目标
--------
用户问「跟合同审查无关」的问题时，助手必须**真的答出来**，而不是甩一段固定
话术。这里的策略是「确定性能力优先，大模型兜底」：

  1. 实时天气   → 中国天气网（主源，真实中文数据）+ wttr.in（备源）
  2. 日期时间   → 本地时钟（北京时间 UTC+8）
  3. 四则运算   → AST 白名单安全求值（拒绝任何函数调用 / 属性访问）
  4. 其他问题   → 返回 None，交回 assistant 走大模型通用问答

零第三方依赖（urllib / json / re / ast / datetime）。
"""
from __future__ import annotations

import ast
import json
import logging
import operator
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

BJ_TZ = timezone(timedelta(hours=8))
_HTTP_TIMEOUT = 6.0
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------------- HTTP 基础（显式禁用代理，避免环境代理把请求带偏） ----------------
def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url: str, referer: str | None = None) -> str | None:
    headers = {"User-Agent": _UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with _opener().open(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read()
    except Exception as e:  # 网络抖动 / 超时 / DNS 失败：静默降级，由上层换源
        logger.debug("chitchat: 请求失败 %s（%s）", url, e)
        return None
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _json_of_js(text: str, prefix: str) -> dict | None:
    """从 `var <prefix> = {...};var ...` 里抠出第一段 JSON 对象。"""
    if not text:
        return None
    i = text.find(prefix)
    if i < 0:
        return None
    i = text.find("{", i)
    if i < 0:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(i, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[i:j + 1])
                except Exception:
                    return None
    return None


# ---------------- 城市 → 中国天气网城市编码 ----------------
CITY_CODES: dict[str, str] = {
    "北京": "101010100", "上海": "101020100", "天津": "101030100", "重庆": "101040100",
    "哈尔滨": "101050101", "长春": "101060101", "沈阳": "101070101", "大连": "101070201",
    "呼和浩特": "101080101", "石家庄": "101090101", "太原": "101100101", "西安": "101110101",
    "济南": "101120101", "青岛": "101120201", "烟台": "101120501", "郑州": "101180101",
    "南京": "101190101", "苏州": "101190401", "无锡": "101190201", "徐州": "101190801",
    "武汉": "101200101", "杭州": "101210101", "宁波": "101210401", "温州": "101210701",
    "合肥": "101220101", "福州": "101230101", "厦门": "101230201", "南昌": "101240101",
    "长沙": "101250101", "广州": "101280101", "深圳": "101280601", "珠海": "101280701",
    "东莞": "101281601", "佛山": "101280800", "南宁": "101300101", "海口": "101310101",
    "三亚": "101310201", "成都": "101270101", "贵阳": "101260101", "昆明": "101290101",
    "拉萨": "101140101", "西宁": "101150101", "兰州": "101160101", "银川": "101170101",
    "乌鲁木齐": "101130101", "香港": "101320101", "澳门": "101330101", "台北": "101340101",
    "常州": "101191101", "南通": "101190501", "扬州": "101190601", "泉州": "101230501",
    "嘉兴": "101210301", "绍兴": "101210501", "洛阳": "101180901", "唐山": "101090501",
}

_CITY_SUFFIX = ("特别行政区", "自治区", "自治州", "地区", "城市", "市辖区", "新区", "市", "省", "县", "区")


def normalize_city(name: str) -> str:
    c = (name or "").strip().strip("　 的？?。，,、")
    for suf in _CITY_SUFFIX:
        if c.endswith(suf) and len(c) > len(suf):
            c = c[: -len(suf)]
            break
    return c


def resolve_city_code(city: str) -> str | None:
    """内置表 → 中国天气网在线搜索接口。"""
    c = normalize_city(city)
    if not c:
        return None
    if c in CITY_CODES:
        return CITY_CODES[c]
    # 内置表里做一次包含匹配（"北京朝阳" → "北京"）
    for k, v in CITY_CODES.items():
        if k in c:
            return v
    text = _get("http://toy1.weather.com.cn/search?cityname=" + urllib.parse.quote(c),
                referer="http://www.weather.com.cn/")
    if not text:
        return None
    try:
        arr = json.loads(text.strip().lstrip("(").rstrip(")"))
    except Exception:
        return None
    if isinstance(arr, list) and arr:
        ref = str(arr[0].get("ref") or "")
        code = ref.split("~", 1)[0].strip()
        if re.fullmatch(r"\d{9}", code):
            return code
    return None


# ---------------- 天气抓取 ----------------
def _aqi_level(aqi: int) -> str:
    if aqi <= 50:
        return "优"
    if aqi <= 100:
        return "良"
    if aqi <= 150:
        return "轻度污染"
    if aqi <= 200:
        return "中度污染"
    if aqi <= 300:
        return "重度污染"
    return "严重污染"


def _fetch_cn(city: str) -> dict | None:
    code = resolve_city_code(city)
    if not code:
        return None
    ref = "https://www.weather.com.cn/"
    sk = _json_of_js(_get(f"https://d1.weather.com.cn/sk_2d/{code}.html", ref) or "", "dataSK")
    if not isinstance(sk, dict) or not sk.get("cityname"):
        return None
    out: dict = {
        "city": str(sk.get("cityname") or city),
        "temp": str(sk.get("temp") or "").strip(),
        "weather": str(sk.get("weather") or "").strip(),
        "wind_dir": str(sk.get("WD") or "").strip(),
        "wind_level": str(sk.get("WS") or "").strip(),
        "humidity": str(sk.get("SD") or "").strip(),
        "aqi": str(sk.get("aqi") or "").strip(),
        "time": str(sk.get("time") or "").strip(),
        "date": str(sk.get("date") or "").strip(),
        "rain": str(sk.get("rain") or "").strip(),
        "source": "中国天气网",
    }
    # 今日高低温（dingzhi 预报接口）
    dz = _json_of_js(_get(f"https://d1.weather.com.cn/dingzhi/{code}.html", ref) or "",
                     f"cityDZ{code}")
    if isinstance(dz, dict):
        info = dz.get("weatherinfo") or {}
        if isinstance(info, dict):
            out["today_high"] = str(info.get("temp") or "").strip().replace("℃", "")
            out["today_low"] = str(info.get("tempn") or "").strip().replace("℃", "")
            out["today_weather"] = str(info.get("weather") or "").strip()
            out["today_wind"] = str(info.get("ws") or "").strip()
    return out


_WTTR_WIND_ZH = {"N": "北风", "NNE": "东北偏北风", "NE": "东北风", "ENE": "东北偏东风",
                 "E": "东风", "ESE": "东南偏东风", "SE": "东南风", "SSE": "东南偏南风",
                 "S": "南风", "SSW": "西南偏南风", "SW": "西南风", "WSW": "西南偏西风",
                 "W": "西风", "WNW": "西北偏西风", "NW": "西北风", "NNW": "西北偏北风"}


def _fetch_wttr(city: str) -> dict | None:
    """备源：wttr.in（国际源，中文描述不总是可用，英文兜底）。"""
    text = _get("https://wttr.in/" + urllib.parse.quote(city) + "?format=j1&lang=zh")
    if not text:
        return None
    try:
        d = json.loads(text)
        cur = d["current_condition"][0]
        today = (d.get("weather") or [{}])[0]
    except Exception:
        return None
    lang = cur.get("lang_zh")
    if isinstance(lang, list) and lang and isinstance(lang[0], dict):
        desc = str(lang[0].get("value") or "").strip()
    else:
        desc = str((cur.get("weatherDesc") or [{}])[0].get("value") or "").strip()
    return {
        "city": city,
        "temp": str(cur.get("temp_C") or "").strip(),
        "weather": desc,
        "wind_dir": _WTTR_WIND_ZH.get(str(cur.get("winddir16Point") or "").upper(), ""),
        "wind_level": (str(cur.get("windspeedKmph") or "").strip() + "km/h").strip("h"),
        "humidity": str(cur.get("humidity") or "").strip() + "%",
        "aqi": "",
        "time": str(cur.get("observation_time") or "").strip(),
        "date": str(today.get("date") or "").strip(),
        "today_high": str(today.get("maxtempC") or "").strip(),
        "today_low": str(today.get("mintempC") or "").strip(),
        "source": "wttr.in",
    }


def fetch_weather(city: str) -> dict | None:
    """主源中国天气网 → 备源 wttr.in，都失败返回 None。"""
    w = _fetch_cn(city)
    if w:
        return w
    return _fetch_wttr(city)


def format_weather(w: dict) -> str:
    lines = [f"{w.get('city') or '该城市'} 实时天气"
             + (f"（{w['time']} 更新）" if w.get("time") else "")]
    if w.get("weather"):
        lines.append(f"· 天气：{w['weather']}")
    if w.get("temp"):
        lines.append(f"· 气温：{w['temp']}℃")
    hi, lo = w.get("today_high") or "", w.get("today_low") or ""
    if hi and lo:
        lines.append(f"· 今日：{w.get('today_weather') or w.get('weather') or ''} "
                     f"{lo}~{hi}℃".replace("  ", " ").strip())
    wind = " ".join(x for x in (w.get("wind_dir"), w.get("wind_level")) if x)
    if wind:
        lines.append(f"· 风：{wind}")
    if w.get("humidity"):
        lines.append(f"· 湿度：{w['humidity']}")
    if w.get("aqi"):
        try:
            aqi = int(float(w["aqi"]))
            lines.append(f"· 空气质量：AQI {aqi}（{_aqi_level(aqi)}）")
        except ValueError:
            lines.append(f"· 空气质量：AQI {w['aqi']}")
    if w.get("rain") and w["rain"] not in ("0", "0.0"):
        lines.append(f"· 降水：{w['rain']}mm")
    lines.append(f"\n数据来源：{w.get('source')}（实时抓取，仅供参考）")
    return "\n".join(lines)


# ---------------- 意图识别 ----------------
_CITY_TAIL = r"([\u4e00-\u9fa5]{2,10}?)(?:市|省|区|县)?"
_GEO_NOISE = ("今天", "明天", "后天", "现在", "目前", "请问", "帮我", "查一下", "查查", "想知道",
              "那里", "这边", "当地", "早上", "中午", "晚上", "白天", "夜里")

_WEATHER_KW = ("天气", "气温", "温度", "下雨", "下雪", "会不会冷", "冷不冷", "热不热", "多少度",
               "刮风", "空气质量", "雾霾", "pm2.5", "pm25", "aqi", "紫外线", "降雨", "降水")


def detect_weather_city(msg: str) -> str | None:
    """识别「天气类问题」并抽出城市；不是天气问题返回 None。"""
    m = (msg or "").strip()
    if not m:
        return None
    low = m.lower()
    if not any(k in low for k in _WEATHER_KW):
        return None
    # 1) 「XX的天气」「XX天气怎么样」「XX今天多少度」
    for pat in (r"([\u4e00-\u9fa5]{2,10}?)(?:市|省)?的?天气",
                r"([\u4e00-\u9fa5]{2,10}?)(?:市|省)?(?:今天|明天|现在)?(?:多少度|气温|温度)",
                r"([\u4e00-\u9fa5]{2,10}?)(?:市|省)?(?:会|要)?(?:下雨|下雪|降温)"):
        mm = re.search(pat, m)
        if mm:
            c = normalize_city(mm.group(1))
            for n in _GEO_NOISE:
                c = c.replace(n, "")
            if 2 <= len(c) <= 8:
                return c
    # 2) 问句里任何位置的已知城市名
    for k, _ in sorted(CITY_CODES.items(), key=lambda x: -len(x[0])):
        if k in m:
            return k
    # 3) 「天气」前面紧跟的两个字
    mm = re.search(r"(?:查|看|知道|问)?(?:一下|下)?([\u4e00-\u9fa5]{2,4})(?:的)?(?:天气|气温|温度)", m)
    if mm:
        c = normalize_city(mm.group(1))
        if 2 <= len(c) <= 4:
            return c
    return None


def is_weather_question(msg: str) -> bool:
    return detect_weather_city(msg) is not None


_TIME_PAT = r"几点|现在时间|当前时间|什么时间|几号|今天日期|今天是几|星期几|周几|礼拜几|哪一年|今年是哪年"
_CALC_CH = {"加": "+", "减": "-", "乘": "*", "除": "/", "×": "*", "÷": "/", "＝": "=",
            "加上": "+", "减去": "-", "乘以": "*", "除以": "/"}

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
           ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def safe_eval(expr: str) -> float:
    """只允许数字与 + - * / // % ** 的安全求值（AST 白名单，拒绝一切调用/属性访问）。"""
    node = ast.parse(expr, mode="eval").body
    return _eval_node(node)


def _eval_node(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("非法字面量")
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 64:
            raise ValueError("指数过大")
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("不支持的表达式")


def try_calc(msg: str) -> str | None:
    m = (msg or "").strip().rstrip("=?？ ")
    if not m or len(m) > 60:
        return None
    q = m
    for k in ("帮我算", "算一下", "计算", "等于多少", "是多少", "等于几", "等于", "请问"):
        q = q.replace(k, "")
    for ch in sorted(_CALC_CH, key=len, reverse=True):   # 长词优先，避免「除以」被「除」截断
        q = q.replace(ch, _CALC_CH[ch])
    q = q.replace("＝", "=").replace("，", "").replace(",", "").strip()
    if "=" in q:
        q = q.split("=")[0]
    if not re.fullmatch(r"[\d\s.+\-*/%()]+", q or ""):
        return None
    if not re.search(r"\d", q) or not re.search(r"[+\-*/%]", q):
        return None
    try:
        val = safe_eval(q)
    except Exception:
        return None
    if isinstance(val, float):
        txt = f"{val:.10g}"
    else:
        txt = str(val)
    return f"{m} = {txt}"


def try_time(msg: str) -> str | None:
    m = (msg or "").strip()
    if not m or not re.search(_TIME_PAT, m):
        return None
    now = datetime.now(BJ_TZ)
    wk = "一二三四五六日"[now.weekday()]
    if re.search(r"几点|时间", m):
        return (f"现在北京时间 {now.strftime('%H:%M:%S')}"
                f"（{now.year} 年 {now.month} 月 {now.day} 日 星期{wk}）。")
    if re.search(r"星期几|周几|礼拜几", m):
        return f"今天是 {now.year} 年 {now.month} 月 {now.day} 日，星期{wk}。"
    if re.search(r"几号|日期|几月", m):
        return f"今天是 {now.year} 年 {now.month} 月 {now.day} 日，星期{wk}。"
    return f"现在是 {now.year} 年 {now.month} 月 {now.day} 日 星期{wk}，北京时间 {now.strftime('%H:%M')}。"


# ---------------- 统一入口 ----------------
def answer(message: str) -> dict | None:
    """命中「确定性能力」时返回结构化回复，否则返回 None（交给大模型）。"""
    m = (message or "").strip()
    if not m:
        return None

    city = detect_weather_city(m)
    if city:
        w = fetch_weather(city)
        if w:
            return {"reply": format_weather(w), "mode": "tool", "view": None,
                    "topic": "weather", "source": w.get("source")}
        return {"reply": (f"没能取到「{city}」的实时天气（天气源暂时不可达）。\n"
                          "你可以稍后再问一次，或先用手机天气 App 查一下～"),
                "mode": "tool", "view": None, "topic": "weather", "source": None}

    t = try_time(m)
    if t:
        return {"reply": t, "mode": "tool", "view": None, "topic": "time", "source": "本地时钟"}

    c = try_calc(m)
    if c:
        return {"reply": c, "mode": "tool", "view": None, "topic": "calc", "source": "本地计算"}

    return None
