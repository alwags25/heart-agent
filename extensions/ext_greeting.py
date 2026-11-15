import datetime







META = {



    "name": "greeting",



    "description": "按时段与语气生成个性化问候语，可选附加场景说明与多条变体"



}







_PERIOD_TABLE = (



    (5, 8, "早上"),



    (9, 11, "上午"),



    (12, 13, "中午"),



    (14, 17, "下午"),



    (18, 22, "晚上"),



)







_CORES = {



    "casual": {



        "早上": [



            "{name}, 早上好!",



            "嗨 {name}, 早上好呀!",



            "{name}, 早哇, 新的一天开始啦!",



        ],



        "上午": [



            "{name}, 上午好!",



            "嗨 {name}, 上午好, 状态还在线吗?",



            "{name}, 上午好呀, 继续加油!",



        ],



        "中午": [



            "{name}, 中午好!",



            "嗨 {name}, 中午好, 吃过了吗?",



            "{name}, 中午好呀, 记得休息一下~",



        ],



        "下午": [



            "{name}, 下午好!",



            "嗨 {name}, 下午好, 犯困了就起来走两步~",



            "{name}, 下午好呀, 胜利在望!",



        ],



        "晚上": [



            "{name}, 晚上好!",



            "嗨 {name}, 晚上好呀, 今天过得怎么样?",



            "{name}, 晚上好, 忙完就该放松啦~",



        ],



        "深夜": [



            "{name}, 夜这么深了, 还在忙呀",



            "嗨 {name}, 深夜了, 还没睡吗?",



            "{name}, 这么晚还没休息呀?",



        ],



    },



    "formal": {



        "早上": [



            "{name}, 您好, 早上好。",



            "您好, 早上好, {name}。",



            "{name}, 早上好, 祝您今天诸事顺利。",



        ],



        "上午": [



            "{name}, 您好, 上午好。",



            "您好, 上午好, {name}。",



            "{name}, 上午好, 祝您工作顺利。",



        ],



        "中午": [



            "{name}, 您好, 中午好。",



            "您好, 中午好, {name}。",



            "{name}, 中午好, 祝您用餐愉快。",



        ],



        "下午": [



            "{name}, 您好, 下午好。",



            "您好, 下午好, {name}。",



            "{name}, 下午好, 请注意适当休息。",



        ],



        "晚上": [



            "{name}, 您好, 晚上好。",



            "您好, 晚上好, {name}。",



            "{name}, 晚上好, 祝您晚间安好。",



        ],



        "深夜": [



            "{name}, 您好, 时间已晚。",



            "您好, {name}, 夜深了, 请注意休息。",



            "{name}, 夜已深, 还请您保重身体。",



        ],



    },



    "warm": {



        "早上": [



            "亲爱的{name}, 早上好呀!",



            "早安呀, {name}! 新的一天开始啦~",



            "亲爱的{name}, 早上好, 今天也要开开心心的哦!",



        ],



        "上午": [



            "亲爱的{name}, 上午好呀!",



            "{name}, 上午好, 今天也要加油哦~",



            "亲爱的{name}, 上午过得还顺利吗?",



        ],



        "中午": [



            "亲爱的{name}, 中午好呀, 记得好好吃饭!",



            "{name}, 中午好, 别忘了午休一会儿~",



            "亲爱的{name}, 中午好, 今天想吃点什么呀?",



        ],



        "下午": [



            "亲爱的{name}, 下午好呀!",



            "{name}, 下午好, 累了就歇一歇哦~",



            "亲爱的{name}, 下午好, 再坚持一下下!",



        ],



        "晚上": [



            "亲爱的{name}, 晚上好呀!",



            "{name}, 晚上好, 今天辛苦啦~",



            "亲爱的{name}, 晚上好, 愿你有个好心情!",



        ],



        "深夜": [



            "亲爱的{name}, 夜这么深了, 还在忙呀",



            "{name}, 夜深了, 早点休息吧~",



            "亲爱的{name}, 别太累着自己哦, 晚安~",



        ],



    },



}







_EXTRAS = {



    "casual": ["", " 今天也要开心哦~", " 加油加油!"],



    "formal": ["", " 如有需要, 随时联系。", " 祝您一切顺遂。"],



    "warm": ["", " 要好好的哦~", " 记得按时吃饭哦~"],



}







_CONTEXT_MAP = (



    (("起床", "醒了", "刚醒"), [



        "刚起床的话, 记得先喝杯温水, 慢慢清醒过来。",



        "起床后伸个懒腰, 开启元气满满的一天吧。",



    ]),



    (("加班",), [



        "还在加班的话, 别太拼, 记得抽空吃点东西。",



        "加班辛苦啦, 忙完记得早点休息。",



    ]),



    (("下班",), [



        "刚下班的话, 好好放松一下, 辛苦啦。",



        "下班了就先把工作放一放, 好好享受自己的时间。",



    ]),



    (("上班", "工作", "通勤", "开会", "会议"), [



        "工作再忙, 也别忘了抽空歇一歇。",



        "事情一件件来, 别急, 你一定能搞定。",



    ]),



    (("睡觉", "睡了", "晚安", "休息", "熬夜"), [



        "准备休息的话, 就早点睡吧, 晚安。",



        "早点放下手机, 睡个好觉最重要。",



    ]),



    (("吃", "饭", "饿", "外卖", "早餐", "午餐", "晚餐"), [



        "记得按时吃饭, 照顾好自己。",



        "好好吃一顿, 胃舒服了心情也会好起来。",



    ]),



    (("累", "疲", "困", "忙"), [



        "累了就停下来歇一会儿, 你已经很棒了。",



        "别硬撑, 休息好了效率才高。",



    ]),



    (("出差", "旅行", "赶路", "路上", "高铁", "飞机"), [



        "在外奔波, 注意安全和作息。",



        "路上注意安全, 到了记得报个平安。",



    ]),



    (("考试", "复习", "学习", "备考", "作业"), [



        "备考辛苦, 保持节奏, 你一定可以的。",



        "学习之余也要记得放松眼睛和脑子。",



    ]),



    (("生日",), [



        "祝你生日快乐, 天天开心!",



        "生日快乐呀, 愿你所有心愿都能实现。",



    ]),



    (("周末",), [



        "周末愉快, 好好享受属于自己的时间。",



        "周末就别太紧绷啦, 放松一点。",



    ]),



    (("节", "新年", "春节", "假期"), [



        "节日快乐, 愿你一切顺遂。",



        "假期里好好休息, 玩得开心点。",



    ]),



    (("下雨", "天气", "降温", "冷", "热", "台风"), [



        "天气多变, 记得照顾好自己。",



        "出门前看一眼天气预报, 注意保暖带伞。",



    ]),



)











def _period_of(hour):



    for start, end, label in _PERIOD_TABLE:



        if start <= hour <= end:



            return label



    return "深夜"











def _to_text(value, default=""):



    if value is None:



        return default



    try:



        text = str(value).strip()



    except Exception:



        return default



    return text if text else default











def _to_int(value, default=1, low=1, high=5):



    result = default



    if value is None:



        return default



    try:



        if isinstance(value, bool):



            return default



        if isinstance(value, (int, float)):



            result = int(value)



        elif isinstance(value, str):



            s = value.strip()



            result = int(float(s)) if s else default



        else:



            result = int(value)



    except Exception:



        return default



    if result < low:



        result = low



    if result > high:



        result = high



    return result











def _context_sentences(context):



    if not context:



        return []



    low = context.lower()



    for keys, sentences in _CONTEXT_MAP:



        for key in keys:



            if key in context or key in low:



                return list(sentences)



    return [



        "关于「%s」, 也请照顾好自己。" % context,



        "「%s」之余, 别忘了多休息。" % context,



        "愿「%s」一切顺利。" % context,



    ]











def run(args: dict) -> dict:



    try:



        if args is None:



            args = {}



        if not isinstance(args, dict):



            return {"ok": False, "error": "args 必须是字典(dict)类型"}







        now = datetime.datetime.now()



        hour = now.hour



        period = _period_of(hour)







        name = _to_text(args.get("name"), "朋友")[:30] or "朋友"







        style = _to_text(args.get("style"), "casual").lower()



        if style not in _CORES:



            style = "casual"







        context = _to_text(args.get("context"), "")[:80]







        count = _to_int(args.get("count", 1), default=1, low=1, high=5)







        cores = _CORES[style][period]



        extras = _EXTRAS[style]



        ctx_sentences = _context_sentences(context)







        greetings = []



        seen = set()



        i = 0



        while len(greetings) < count and i < 200:



            core = cores[i % len(cores)]



            extra = ""



            if i >= len(cores):



                block = (i - len(cores)) // len(cores)



                extra = extras[block % len(extras)]



            try:



                text = core.format(name=name) + extra



            except Exception:



                text = str(core) + extra



            if ctx_sentences:



                text += " " + ctx_sentences[len(greetings) % len(ctx_sentences)]



            if text not in seen:



                seen.add(text)



                greetings.append(text)



            i += 1







        while len(greetings) < count:



            base = greetings[-1] if greetings else "你好, %s!" % name



            greetings.append("%s(%d)" % (base, len(greetings) + 1))







        return {



            "ok": True,



            "greeting": greetings[0],



            "greetings": greetings,



            "period": period,



            "hour": hour,



            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),



        }



    except Exception as exc:



        return {"ok": False, "error": "生成问候失败: %s" % exc}