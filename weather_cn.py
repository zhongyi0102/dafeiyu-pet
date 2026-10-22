# -*- coding: utf-8 -*-
"""weather_cn.py — wttr.in 天气描述汉化 + 人设短评

wttr.in 的 weatherDesc 是英文（lang_zh 也不全），这里建完整映射表。
没命中的词做兜底处理，保证永远不会把英文直接露给用户。
"""

# wttr.in weatherDesc → 中文（覆盖 wttr.in 全部 48 种描述）
WEATHER_CN = {
    # 晴 / 云
    "Sunny": "晴",
    "Clear": "晴",
    "Partly cloudy": "多云",
    "Cloudy": "多云",
    "Overcast": "阴",
    "Mist": "薄雾",
    "Fog": "雾",
    "Freezing fog": "冻雾",
    "Patchy rain possible": "可能有零星小雨",
    "Patchy rain nearby": "附近有零星小雨",
    "Patchy light rain": "零星小雨",
    "Light rain": "小雨",
    "Moderate rain": "中雨",
    "Heavy rain": "大雨",
    "Light rain shower": "阵雨",
    "Moderate or heavy rain shower": "强阵雨",
    "Patchy light drizzle": "零星毛毛雨",
    "Light drizzle": "毛毛雨",
    "Freezing drizzle": "冻毛毛雨",
    "Heavy freezing drizzle": "强冻毛毛雨",
    "Patchy snow possible": "可能有零星小雪",
    "Patchy snow nearby": "附近有零星小雪",
    "Patchy light snow": "零星小雪",
    "Light snow": "小雪",
    "Moderate snow": "中雪",
    "Heavy snow": "大雪",
    "Light snow showers": "阵雪",
    "Moderate or heavy snow showers": "强阵雪",
    "Blowing snow": "风吹雪",
    "Blizzard": "暴风雪",
    "Patchy sleet possible": "可能有零星雨夹雪",
    "Light sleet": "雨夹雪",
    "Moderate or heavy sleet": "强雨夹雪",
    "Light sleet showers": "雨夹雪阵雨",
    "Moderate or heavy sleet showers": "强雨夹雪阵雨",
    "Patchy freezing drizzle possible": "可能有零星冻毛毛雨",
    "Light freezing rain": "小冻雨",
    "Moderate or heavy freezing rain": "强冻雨",
    "Ice pellets": "冰粒",
    "Light showers of ice pellets": "小阵冰粒",
    "Moderate or heavy showers of ice pellets": "强阵冰粒",
    "Thundery outbreaks possible": "可能有雷阵雨",
    "Patchy light rain with thunder": "零星雷雨",
    "Moderate or heavy rain with thunder": "雷雨",
    "Patchy light snow with thunder": "零星雷雪",
    "Moderate or heavy snow with thunder": "雷雪",
    "Haze": "霾",
    "Smoky haze": "烟霾",
    "Sleet": "雨夹雪",
}

# 每种天气类型配一句人设短评（按关键词分组匹配）
WEATHER_TIPS = [
    # (关键词组, 短评) —— 按顺序匹配，命中第一个就停
    (("雷",), "打雷了，别在窗户边上待着。"),
    (("大雨", "强阵雨", "暴雨"), "雨这么大，今天出门记得带伞，不许淋回来。"),
    (("中雨", "阵雨"), "下雨了呢……伞放包里了没？"),
    (("小雨", "毛毛雨", "零星"), "毛毛雨最烦人了，带把小伞吧。"),
    (("雨夹雪", "冻雨", "冰粒"), "这种天气路滑，走路看着点。"),
    (("雪", "暴风雪"), "下雪了呀……多穿点，笨蛋。"),
    (("雾", "霾", "烟霾"), "外面雾蒙蒙的，出门慢点走。"),
    (("晴",), "天气不错呢……不过别晒太久。"),
    (("多云",), "云有点多，凉快倒是凉快。"),
    (("阴",), "天阴阴的，心情别跟着阴。"),
    (("冻",), "气温都在零下了，手别冻着。"),
]


def weather_cn(desc_en):
    """英文天气描述 → 中文。没命中返回空串（由调用方决定省略还是兜底）。"""
    t = (desc_en or "").strip()
    if not t:
        return ""
    if t in WEATHER_CN:
        return WEATHER_CN[t]
    # 模糊兜底：按关键词猜
    low = t.lower()
    if "thunder" in low:
        return "雷雨"
    if "snow" in low or "sleet" in low or "blizzard" in low:
        return "有雪"
    if "rain" in low or "drizzle" in low or "shower" in low:
        return "有雨"
    if "fog" in low or "mist" in low or "haze" in low:
        return "有雾"
    if "cloud" in low or "overcast" in low:
        return "多云"
    if "sun" in low or "clear" in low:
        return "晴"
    return ""          # 实在认不出，宁可不说也不说英文


def weather_tip(desc_en):
    """按天气给人设短评。认不出给通用句。"""
    t = (desc_en or "").strip()
    cn = weather_cn(t)
    for keys, tip in WEATHER_TIPS:
        if any(k in cn for k in keys):
            return tip
    # 全都没匹配（罕见）给通用句
    return "出门前看一眼窗外吧。"
