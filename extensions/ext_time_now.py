"""示例扩展:获取当前本地时间(HeartAgent 出厂自带,可作为扩展编写模板)"""







from datetime import datetime















META = {"name": "time_now", "description": "获取当前本地日期时间与星期"}























def run(args: dict) -> dict:







    now = datetime.now()







    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]







    return {







        "ok": True,







        "now": now.strftime("%Y-%m-%d %H:%M:%S"),







        "weekday": weekdays[now.weekday()],







    }