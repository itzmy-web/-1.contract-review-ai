"""通用闲聊 / 实时信息兜底的回归测试（2026-10-13）。

背景：用户反馈「闲聊兜底不对」——问「今天北京天气怎么样」时，助手只回了一段
固定的欢迎话术，没有真的答出天气。根因是旧的闲聊兜底只有关键词知识库，
未命中就返回 `_GREET`，且大模型 Key 失效。

本文件锁住新的行为契约：
  1) 天气类问题 → 抓真实数据（中国天气网主源 / wttr.in 备源）后作答
  2) 时间 / 日期 / 四则运算 → 本地确定性回答
  3) 大模型不可用时，上述三类**依然可用**，且兜底话术要「有信息量」而非复述欢迎语
  4) 确定性工具优先于大模型（实时数据不给模型猜）
  5) 安全求值拒绝一切函数调用 / 属性访问
所有网络访问均在测试内被 monkeypatch 掉，用例离线且确定性。
"""
from __future__ import annotations

import json

import pytest

from backend import assistant, chitchat, llm

# ---------------- 固定桩数据（格式与线上真实响应一致） ----------------
SK_JS = ('var dataSK={"nameen":"beijing","cityname":"北京","city":"101010100","temp":"13.8",'
         '"tempf":"56.8","WD":"西南风","wde":"SW","WS":"1级","wse":"2km/h","SD":"36%",'
         '"sd":"36%","qy":"1018","njd":"30km","time":"19:40","rain":"0","rain24h":"0",'
         '"aqi":"22","aqi_pm25":"22","weather":"晴","weathere":"Sunny","weathercode":"d00",'
         '"date":"10月13日(星期二)"};')

DZ_JS = ('var cityDZ101010100 ={"weatherinfo":{"city":"101010100","cityname":"北京",'
         '"fctime":"202610131800","temp":"21℃","tempn":"8℃","weather":"晴","weathercode":"d0",'
         '"weathercoden":"n0","wd":"西北风转西南风","ws":"3-4级转<3级"}};'
         'var alarmDZ101010100 ={"w":[]}')

WTTR_JSON = json.dumps({
    "current_condition": [{
        "temp_C": "20", "FeelsLikeC": "16", "humidity": "12", "windspeedKmph": "7",
        "winddir16Point": "WNW", "observation_time": "11:09 AM",
        "weatherDesc": [{"value": "Smoky haze"}], "lang_zh": [{"value": "烟霾"}],
    }],
    "weather": [{"date": "2026-10-13", "maxtempC": "22", "mintempC": "17"}],
}, ensure_ascii=False)


def _patch_get(monkeypatch, handler):
    monkeypatch.setattr(chitchat, "_get", handler)


# ---------------- 城市解析 ----------------
def test_normalize_city_strips_suffix():
    assert chitchat.normalize_city("北京市") == "北京"
    assert chitchat.normalize_city("　杭州 ") == "杭州"
    assert chitchat.normalize_city("广东省") == "广东"
    assert chitchat.normalize_city("北京？") == "北京"


def test_resolve_city_code_builtin_table(monkeypatch):
    _patch_get(monkeypatch, lambda url, referer=None: None)  # 断网也不能影响内置表
    assert chitchat.resolve_city_code("北京") == "101010100"
    assert chitchat.resolve_city_code("北京市") == "101010100"
    assert chitchat.resolve_city_code("深圳") == "101280601"


def test_resolve_city_code_online_search(monkeypatch):
    """内置表没有的城市，走中国天气网搜索接口。"""
    def fake(url, referer=None):
        assert "toy1.weather.com.cn/search" in url
        return '[{"ref":"101250101~hunan~长沙~Changsha~长沙~Changsha~731~410000~CS~湖南"}]'

    _patch_get(monkeypatch, fake)
    assert chitchat.resolve_city_code("长沙") == "101250101"


# ---------------- 天气意图 ----------------
@pytest.mark.parametrize("msg,city", [
    ("今天北京天气怎么样", "北京"),
    ("北京今天天气如何", "北京"),
    ("上海天气", "上海"),
    ("深圳今天多少度", "深圳"),
    ("杭州会下雨吗", "杭州"),
    ("查一下成都的天气", "成都"),
])
def test_detect_weather_city_hits(msg, city):
    assert chitchat.detect_weather_city(msg) == city


@pytest.mark.parametrize("msg", ["现在几点", "你好", "帮我算 1+1", "怎么上传合同", ""])
def test_detect_weather_city_misses(msg):
    assert chitchat.detect_weather_city(msg) is None


# ---------------- 天气抓取与格式化 ----------------
def test_fetch_weather_primary_source_cn(monkeypatch):
    def fake(url, referer=None):
        if "sk_2d" in url:
            return SK_JS
        if "dingzhi" in url:
            return DZ_JS
        return None

    _patch_get(monkeypatch, fake)
    w = chitchat.fetch_weather("北京")
    assert w and w["source"] == "中国天气网"
    assert w["city"] == "北京" and w["temp"] == "13.8" and w["weather"] == "晴"
    assert w["today_high"] == "21" and w["today_low"] == "8"
    assert w["aqi"] == "22"


def test_format_weather_is_informative():
    txt = chitchat.format_weather({
        "city": "北京", "temp": "13.8", "weather": "晴", "wind_dir": "西南风",
        "wind_level": "1级", "humidity": "36%", "aqi": "22", "time": "19:40",
        "today_high": "21", "today_low": "8", "date": "10月13日(星期二)",
        "source": "中国天气网",
    })
    for token in ("北京", "晴", "13.8℃", "西南风", "36%", "AQI 22（优）", "8~21℃", "中国天气网"):
        assert token in txt, f"格式化结果缺少 {token}：\n{txt}"


def test_fetch_weather_falls_back_to_wttr(monkeypatch):
    """中国天气网不可达时必须换备源，而不是直接失败。"""
    def fake(url, referer=None):
        if "wttr.in" in url:
            return WTTR_JSON
        return None

    _patch_get(monkeypatch, fake)
    w = chitchat.fetch_weather("北京")
    assert w and w["source"] == "wttr.in"
    assert w["weather"] == "烟霾"          # lang=zh 生效时用中文描述
    assert w["temp"] == "20"


def test_weather_unreachable_gives_honest_answer(monkeypatch):
    """两个源都挂：明确告知取不到，而不是编一个天气。"""
    _patch_get(monkeypatch, lambda url, referer=None: None)
    r = chitchat.answer("今天北京天气怎么样")
    assert r and r["topic"] == "weather"
    assert "没能取到" in r["reply"]
    assert "北京" in r["reply"]


# ---------------- 时间 / 计算 ----------------
def test_try_time_answers_clock():
    r = chitchat.try_time("现在几点")
    assert r and "北京时间" in r


def test_try_time_answers_weekday():
    r = chitchat.try_time("今天星期几")
    assert r and "星期" in r


def test_try_time_ignores_unrelated():
    assert chitchat.try_time("合同怎么上传") is None


@pytest.mark.parametrize("msg,expect", [
    ("帮我算 1250*8", "10000"),
    ("1加1", "2"),
    ("100 除以 4 等于多少", "25"),
    ("(3+5)*2", "16"),
])
def test_try_calc(msg, expect):
    r = chitchat.try_calc(msg)
    assert r and r.endswith("= " + expect), r


def test_try_calc_ignores_plain_text():
    assert chitchat.try_calc("合同违约金怎么算") is None


@pytest.mark.parametrize("evil", [
    "__import__('os').system('id')",
    "(1).__class__.__mro__",
    "open('/etc/passwd').read()",
    "9**9**9",
])
def test_safe_eval_rejects_dangerous_input(evil):
    assert chitchat.try_calc(evil) is None
    with pytest.raises(Exception):
        chitchat.safe_eval(evil)


# ---------------- assistant 编排 ----------------
def test_assistant_answers_weather_instead_of_greet(monkeypatch):
    """核心回归：问天气必须答天气，不能退回欢迎话术。"""
    def fake(url, referer=None):
        if "sk_2d" in url:
            return SK_JS
        if "dingzhi" in url:
            return DZ_JS
        return None

    _patch_get(monkeypatch, fake)
    d = assistant.chat("今天北京天气怎么样")
    assert d["mode"] == "tool" and d["topic"] == "weather"
    assert "晴" in d["reply"] and "13.8℃" in d["reply"]
    assert "我是合同审批审查系统的智能助手" not in d["reply"]


def test_assistant_weather_works_without_llm(monkeypatch):
    """大模型完全不可用时，天气仍然答得出来。"""
    assert llm.enabled() is False
    def fake(url, referer=None):
        return SK_JS if "sk_2d" in url else (DZ_JS if "dingzhi" in url else None)

    _patch_get(monkeypatch, fake)
    d = assistant.chat("上海天气")
    assert d["mode"] == "tool" and "天气" in d["reply"]


def test_tool_takes_priority_over_llm(monkeypatch):
    """有真实数据就不该让大模型猜 —— 天气走工具，不调 LLM。"""
    monkeypatch.setattr(assistant.llm, "enabled", lambda: True)
    called = {"n": 0}

    def fake_chat_json(prompt, system=""):
        called["n"] += 1
        return {"reply": "模型猜的天气", "view": None}

    monkeypatch.setattr(assistant.llm, "chat_json", fake_chat_json)
    monkeypatch.setattr(chitchat, "_get",
                        lambda url, referer=None: SK_JS if "sk_2d" in url else DZ_JS)
    d = assistant.chat("北京天气")
    assert d["mode"] == "tool"
    assert called["n"] == 0


def test_assistant_offtopic_fallback_is_informative(monkeypatch):
    """无关问题 + 无大模型：兜底要说明「现在能答什么」，不能只复述欢迎语。"""
    assert llm.enabled() is False
    d = assistant.chat("讲个笑话吧")
    assert d["mode"] == "local"
    assert "大模型" in d["reply"]
    assert "天气" in d["reply"]
    assert d["reply"] != assistant._GREET


def test_assistant_kb_still_answers_system_questions():
    d = assistant.chat("怎么上传合同")
    assert d["mode"] == "local" and "上传" in d["reply"]


def test_assistant_greet_mentions_chitchat():
    d = assistant.chat("你好")
    assert "聊天" in d["reply"] or "随便聊" in d["reply"]


def test_assistant_uses_llm_for_general_questions(monkeypatch):
    """有可用大模型时，通用问题走 LLM，且能带出跳转按钮。"""
    monkeypatch.setattr(assistant.llm, "enabled", lambda: True)
    monkeypatch.setattr(assistant.llm, "chat_json",
                        lambda prompt, system="": {"reply": "法国首都是巴黎。", "view": None})
    d = assistant.chat("法国的首都是哪里")
    assert d["mode"] == "llm" and "巴黎" in d["reply"]


def test_assistant_passes_view_whitelist(monkeypatch):
    monkeypatch.setattr(assistant.llm, "enabled", lambda: True)
    monkeypatch.setattr(assistant.llm, "chat_json",
                        lambda prompt, system="": {"reply": "去审查大盘上传", "view": "dashboard"})
    d = assistant.chat("我该去哪上传")
    assert d["view"] == "dashboard"

    monkeypatch.setattr(assistant.llm, "chat_json",
                        lambda prompt, system="": {"reply": "乱指挥", "view": "evil_page"})
    assert assistant.chat("随便问问")["view"] is None


def test_assistant_empty_message():
    d = assistant.chat("   ")
    assert "请输入" in d["reply"]


def test_system_prompt_forbids_fabrication():
    """系统提示必须显式禁止编造实时数据（防止模型瞎报天气）。"""
    assert "不要编造" in assistant._SYSTEM or "绝对不要编造" in assistant._SYSTEM
    assert "通用问答" in assistant._SYSTEM
