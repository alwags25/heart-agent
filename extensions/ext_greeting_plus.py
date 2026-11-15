"""greeting_plus —— 多语气、多语言、可变数量的个性化问候语生成器。

只依赖 Python 标准库，不联网，执行迅速。
"""

import random
from datetime import datetime

META = {
    "name": "greeting_plus",
    "description": "多语气、多语言、可变数量的个性化问候语生成器，支持场景补充与时段自动识别",
}

# ---------------------------------------------------------------- 常量配置

_VALID_LANGS = ("zh", "en")
_VALID_TONES = ("formal", "casual", "warm", "playful")
_VALID_PERIODS = ("morning", "afternoon", "evening", "night")

_DEFAULT_LANG = "zh"
_DEFAULT_TONE = "warm"
_DEFAULT_VARIANTS = 3
_MAX_VARIANTS = 5

_PERIOD_WORD = {
    "zh": {
        "morning": "早上好",
        "afternoon": "下午好",
        "evening": "晚上好",
        "night": "夜深了",
    },
    "en": {
        "morning": "Good morning",
        "afternoon": "Good afternoon",
        "evening": "Good evening",
        "night": "Good evening",
    },
}

_PERIOD_EMOJI = {
    "morning": "🌅",
    "afternoon": "☀️",
    "evening": "🌇",
    "night": "🌙",
}

# 每种 (语言, 语气) 的句式模板，模板内只用 {g} 与 {name} 两个占位符
_TEMPLATES = {
    ("zh", "formal"): [
        "{g}，{name}",
        "您好，{name}，{g}",
        "{g}，{name}，见字如晤",
        "谨向{name}道一声：{g}",
        "{g}，{name}，愿您一切顺遂",
        "{g}，{name}，祝您今日安好",
    ],
    ("zh", "casual"): [
        "{g}，{name}",
        "嘿，{name}，{g}",
        "{g}啊，{name}",
        "哟，{name}，{g}",
        "{g}，{name}，今天也一起加油",
        "{name}，{g}啦",
    ],
    ("zh", "warm"): [
        "{g}，{name}",
        "{g}呀，{name}",
        "亲爱的{name}，{g}",
        "{g}，{name}，愿你今天一切顺心",
        "{name}，{g}，新的一天也要好好照顾自己",
        "{g}，{name}，愿你被这个世界温柔以待",
    ],
    ("zh", "playful"): [
        "{g}，{name}",
        "{g}啦，{name}",
        "叮咚，{name}，你的{g}已送达",
        "{g}呀，{name}，今天也要元气满满",
        "嗨嗨，{name}，{g}",
        "{g}，{name}，快准备好接收今日份好运吧",
    ],
    ("en", "formal"): [
        "{g}, {name}",
        "{g}, {name}. It is a pleasure to greet you",
        "{g}, {name}. I wish you a pleasant day",
        "{g}, {name}. May everything go smoothly",
        "Dear {name}, {g}",
        "{g}, {name}. Wishing you all the best",
    ],
    ("en", "casual"): [
        "{g}, {name}",
        "Hey {name}, {g}",
        "{g} there, {name}",
        "Yo {name}, {g}",
        "{g}, {name}. Have a good one",
        "Hi {name}, {g}",
    ],
    ("en", "warm"): [
        "{g}, {name}",
        "{g}, dear {name}",
        "Hey {name}, {g}",
        "{g}, {name}. Hope your day treats you kindly",
        "{g}, {name}. Wishing you a lovely day",
        "{g}, {name}. Take good care of yourself today",
    ],
    ("en", "playful"): [
        "{g}, {name}",
        "Hey hey, {name}, {g}",
        "{g} there, {name}",
        "{g}, {name}. Ready for some good vibes",
        "{g}, {name}. Let's make today awesome",
        "Yay, {name}, {g}",
    ],
}

# 句末标点：语气决定标点风格（playful 用波浪号/感叹号，formal 更克制）
_END_PUNCT = {
    "zh": {"formal": "。", "casual": "！", "warm": "～", "playful": "！～"},
    "en": {"formal": ".", "casual": "!", "warm": "!", "playful": "!~"},
}

# 场景补充句（仅当 scene 非空时追加）
_SCENE_SENTENCE = {
    "zh": {
        "formal": "关于{scene}，也祝一切顺利",
        "casual": "{scene}，也加油啊",
        "warm": "也祝{scene}顺顺利利",
        "playful": "{scene}？冲呀",
    },
    "en": {
        "formal": "Regarding {scene}, wishing you the best",
        "casual": "Good luck with {scene} too",
        "warm": "Hope {scene} goes well too",
        "playful": "Go crush {scene} too",
    },
}


# ---------------------------------------------------------------- 工具函数

def _detect_period(hour: int) -> str:
    """按本机小时判断时段：5-11 morning，12-17 afternoon，18-22 evening，其余 night。"""
    if 5 <= hour <= 11:
        return "morning"
    if 12 <= hour <= 17:
        return "afternoon"
    if 18 <= hour <= 22:
        return "evening"
    return "night"


def _coerce_bool(value, default: bool = True) -> bool:
    """宽松地把各种输入转成 bool，无法识别时回退默认值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "y", "on"):
            return True
        if text in ("false", "0", "no", "n", "off", ""):
            return False
    return default


def _clean_choice(value, valid, default):
    """字符串枚举参数清洗：非法/缺失则回退默认值。"""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in valid:
            return text
    return default


def _clean_text(value, fallback=""):
    """把任意输入安全地转成去空白的字符串。"""
    if value is None:
        return fallback
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return fallback
    value = value.strip()
    return value if value else fallback


# ---------------------------------------------------------------- 主入口

def run(args: dict) -> dict:
    try:
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return {"ok": False, "error": "参数 args 必须是 dict 类型"}

        now = datetime.now()
        hour = int(now.hour)

        # 语言
        lang = _clean_choice(args.get("lang"), _VALID_LANGS, _DEFAULT_LANG)

        # 语气
        tone = _clean_choice(args.get("tone"), _VALID_TONES, _DEFAULT_TONE)

        # 时段：auto 或非法值一律按当前时间自动判断
        period_arg = args.get("period", "auto")
        if isinstance(period_arg, str) and period_arg.strip().lower() in _VALID_PERIODS:
            period = period_arg.strip().lower()
        else:
            period = _detect_period(hour)

        # 称呼
        name = _clean_text(args.get("name"), "")
        if not name:
            name = "朋友" if lang == "zh" else "Friend"

        # 场景（可选）
        scene = _clean_text(args.get("scene"), "")

        # 条数：非法回退默认，超范围截断
        raw_variants = args.get("variants", _DEFAULT_VARIANTS)
        try:
            variants = int(raw_variants)
        except (TypeError, ValueError):
            variants = _DEFAULT_VARIANTS
        variants = max(1, min(_MAX_VARIANTS, variants))

        # 表情开关
        use_emoji = _coerce_bool(args.get("emoji", True), True)

        # ------------------------------ 生成问候语
        greet_word = _PERIOD_WORD[lang][period]
        punct = _END_PUNCT[lang][tone]
        emoji = _PERIOD_EMOJI.get(period, "")
        sep = " " if lang == "en" else ""

        templates = list(_TEMPLATES.get((lang, tone), _TEMPLATES[(_DEFAULT_LANG, _DEFAULT_TONE)]))
        random.shuffle(templates)

        seen = set()
        greetings = []
        for tpl in templates:
            if len(greetings) >= variants:
                break
            try:
                text = tpl.format(g=greet_word, name=name)
            except Exception:
                continue
            text = text + punct
            if scene:
                seg = _SCENE_SENTENCE[lang][tone].format(scene=scene)
                text = text + sep + seg + punct
            if use_emoji and emoji:
                text = text + sep + emoji
            if text in seen:
                continue
            seen.add(text)
            greetings.append(text)

        # 极端兜底（正常情况下不会走到）
        if not greetings:
            base = "{g}，{name}".format(g=greet_word, name=name) if lang == "zh" \
                else "{g}, {name}".format(g=greet_word, name=name)
            base = base + punct + (sep + emoji if (use_emoji and emoji) else "")
            greetings = [base]

        return {
            "ok": True,
            "period": period,
            "hour": hour,
            "tone": tone,
            "lang": lang,
            "name": name,
            "scene": scene,
            "emoji": use_emoji,
            "count": len(greetings),
            "greetings": greetings,
            "primary": greetings[0],
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        }

    except Exception as exc:  # 任何意外都不抛出，统一返回错误结构
        return {"ok": False, "error": "生成问候语失败：{0}".format(exc)}