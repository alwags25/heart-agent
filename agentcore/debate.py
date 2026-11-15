"""红蓝对抗辩论:同一个模型戴两顶帽子(正方/反方)多轮交锋,裁判总结裁决。
用于困难决策:主控AI 可用 request_debate 工具发起,UI 也可手动触发。
全程发布 debate 事件(右屏可见),裁决与少数派异议存入记忆。"""
import time


class Debate:
    def __init__(self, llm, events, memory):
        self.llm = llm
        self.events = events
        self.memory = memory

    def run(self, topic, rounds=2):
        rounds = max(1, min(3, int(rounds or 2)))
        transcript = []
        sid = "debate%d" % int(time.time() * 1000)

        def speak(side, instruction, tag):
            prompt = ("辩题:「%s」\n你的立场:%s\n%s\n\n历史交锋:\n%s\n\n"
                      "输出你的发言(中文,≤180字,直接说观点和论据)。"
                      % (topic, side, instruction,
                         "\n".join("[%s] %s" % (t, x) for t, x in transcript[-6:]) or "(无)"))
            res = self.llm.chat([{"role": "user", "content": prompt}], temperature=0.7)
            text = (res.content or "").strip()[:400]
            transcript.append((tag, text))
            self.events.publish("debate", debate_id=sid, side=tag, text=text)
            return text

        for r in range(1, rounds + 1):
            speak("正方", "第 %d 轮:陈述支持该主张的核心论据(第2轮起先反驳对方上一轮)。" % r,
                  "正方R%d" % r)
            speak("反方", "第 %d 轮:陈述反对该主张的核心论据,并指出正方论证的漏洞。" % r,
                  "反方R%d" % r)
        judge = self.llm.chat(
            [{"role": "user",
              "content": "你是中立裁判。辩题:「%s」。交锋记录:\n%s\n\n"
                         "输出裁决(≤200字):1) 双方最强论点各一;2) 你的裁决与理由;"
                         "3) 少数派异议(即使裁决明确,也写出反方最有价值的一点)。"
                         % (topic, "\n".join("[%s] %s" % (t, x) for t, x in transcript))}],
            temperature=0.3)
        verdict = (judge.content or "").strip()
        self.events.publish("debate", debate_id=sid, side="裁决", text=verdict)
        self.memory.add_summary("辩论「%s」(%s)\n%s" % (topic, time.strftime("%m-%d %H:%M"),
                                                        verdict[:500]), kind="debate")
        return {"ok": True, "verdict": verdict, "turns": len(transcript)}
