# 💓 HeartAgent — 会自己长本事的桌面 AI 员工

> **它替你盯任务、记偏好、写扩展、跑求职;你睡觉,它心跳。**
> 零第三方依赖 · 纯 Python 标准库 · 7×24 无人值守 · 双模型协作
> [English](README.en.md)

<!-- CI 徽章:推送到 GitHub 后把「你的用户名」替换为实际仓库路径 -->
[![CI](https://github.com/你的用户名/HeartAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/你的用户名/HeartAgent/actions/workflows/ci.yml)
![自主运行](docs/badges/uptime.svg)
![任务执行](docs/badges/tasks.svg)
![长期记忆](docs/badges/memory.svg)
![回归健康分](docs/badges/eval.svg)
![本月花费](docs/badges/cost.svg)
![稳定性](docs/badges/stability.svg)

<video src="docs/demo.webm" controls muted width="720"></video>
*▲ 27 秒实机演示:对话 → 工具调用 → 真实模型回复 → 记忆图谱 → 评估台*

**它和普通"聊天机器人"的区别:会主动干活。** 心跳机制让它每 30 秒巡检一次 —— 空闲时整理记忆、发现记忆矛盾会向你求证、把常用的代码片段沉淀成扩展(经你审批)、每天早上发日报;定时任务失败自动重试并推送通知到你的微信。上面那排徽章就是它自己统计的真实运行数据。

本地优先、零第三方依赖(纯 Python 标准库)的 Agent 工具。目标驱动 · 自我进化 · 无人值守。

| 特性 | 实现 |
|---|---|
| 🤖 **双模型协作** | 主控AI(思考决策)+ 协作AI(记忆管家),可用不同模型;原生 Function Calling,不支持时自动回退 JSON 协议 |
| 🔧 **自己写扩展** | AI 生成扩展代码 → **审批门** → 版本备份 → 审计日志 → 子进程隔离调用(内存 Job 限制+超时杀树);**技能收割**:run_code 成功片段自动提炼为扩展候选 |
| 💓 **心跳机制** | 空闲整理记忆(学习)、**好奇心引擎**(RSS/网页/文件夹每日探索)、自我反思💡、每日日报📊、**提示词自进化🧬**、配置热重载 |
| 👀 **双屏透明化** | 左屏 = 对话/任务/目标/记忆/扩展;右屏 = 实时事件流按类型过滤;流式输出、可随时停止、Markdown 渲染、**亮/暗主题切换(跟随系统偏好)** |
| 📋 **任务管理** | 定时/循环/cron × 提示词/代码,失败重试、**任务链**、并发上限、失败通知(Server酱/邮件/桌面)、**DAG 流程图视图** |
| 🎯 **目标系统** | 长期目标 → 主控AI自动拆解为任务+记忆 → 心跳定期检查推进 → 进展记录与日报汇总 |
| 🧠 **记忆系统** | SQLite + FTS5 + 向量语义检索(Ollama 嵌入,**无 Ollama 时零依赖哈希向量兜底**)、相似记忆去重合并、记忆编辑、多会话、旧版自动迁移 |
| 📚 **知识库 RAG** | 本地文件/文件夹/URL 摄入 → 分块嵌入 → `search_knowledge` 语义检索;**按内容指纹增量摄入** |
| 📨 **接入渠道** | 邮件(IMAP 收/SMTP 回)与 Telegram Bot 双向接入,白名单安全;通知支持企业微信/钉钉/飞书,**通用 webhook 入站**(`/api/webhook/in`) |
| 🤖 **多实例编队** | `delegate_to_agent` 把子任务委托给同伴 HeartAgent 实例(`/api/a2a`) |
| ⚡ **混合路由** | 简单消息走本地小模型快答,复杂消息走完整工具循环(不可用时自动回退;**连续失败自动熔断**) |
| 💤 **睡眠整理** | 遗忘曲线归档弱记忆 + **矛盾检测**(候选对→模型判定→待澄清卡片,可一键裁决)
| 🧪 **评估台** | Agent 单元测试(预期工具/关键词断言)+ 回归**健康分曲线**,改提示词/换模型即见退化
| ⏪ **思考回放器** | 对话轮落盘回放 + **反事实分支**(换模型重跑同一输入,对比决策差异)
| 🛡️ **安全围栏** | 扩展**能力令牌**(net/proc/fs-write 静态检测+子进程强制执行)+ **信任分级**(L0-L3,失败降分)+ **受限令牌降权**(可选,`security.restricted_token`)
| 💰 **成本钱包** | 按月预算+单价表,自主活动预算闸门(对话不受限),UI 实时花费
| ⚔️ **红蓝辩论** | 困难决策正反方多轮交锋+裁判裁决+保留少数派异议
| 🧠 **心智包** | 记忆/扩展/任务/目标/提示词打包导出导入(密钥脱敏+完整性清单+安全合并)
| 💼 **求职管家包** | 自指型演示:简历匹配/面试题库扩展+岗位追踪目标
| 👁️ **视觉能力** | take_screenshot 截屏 → 图像消息进上下文,主模型直接看懂屏幕内容
| 🕸️ **记忆图谱** | 睡眠时抽取实体关系三元组,recall 注入图谱上下文,记忆页 SVG 力导向图可视化
| 🤝 **第二意见** | 高危操作(删任务/危代码/联网扩展)先经协作AI安全审查,否决即拦截 —— 双模型从分工到制衡
| 🔐 **访问安全** | Host 防DNS重绑定 + Origin 跨站校验 + 可选访问令牌
| ⚡ **对话优先级** | 用户消息可抢占自主运行(任务自动让位,可重跑);向量回填、评估LLM裁判+轨迹指标
| 🛡️ **自治等级** | L0旁观~L3完全自治;低于L2时AI创建的任务/扩展自动进入待批状态
| 📦 **运维** | token 用量统计、日志轮转、一键备份导出、API Key 环境变量覆盖、上下文自动压缩、防提示词注入 |

## 快速开始

```bash
cd heart-agent
python main.py          # 打开 http://127.0.0.1:8765
python main.py --cli    # 终端模式
```

未填 API Key 时以 **mock 模式**运行。编辑 `config.json` 填 Key 后**保存即热重载**。

### 配置模型(config.json)

```json
{
  "main_model":   {"provider": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key": "你的Key", "model": "glm-4.6"},
  "assist_model": {"provider": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key": "你的Key", "model": "glm-4-flash"}
}
```

DeepSeek:base_url `https://api.deepseek.com`。本地模型:`"provider": "ollama"`。
密钥可用环境变量 `HEARTAGENT_MAIN_API_KEY` / `HEARTAGENT_ASSIST_API_KEY`。

### 推荐的本地增强(可选)

```bash
ollama pull nomic-embed-text   # 语义记忆 + RAG 向量检索
ollama pull qwen2.5:0.5b       # 混合路由:简单消息本地快答
```

## 架构

```
                ┌────────────────── heart-agent ──────────────────┐
 用户/邮件/TG ──►│  Web双屏UI(左:结果 右:思考)  /  --cli 终端      │
 同伴Agent ────►│  (/api/a2a)                                      │
                │   ┌───▼─────┐                                    │
                │   │ 主控AI   │──原生FC──► 工具(17个)              │
                │   │ (Brain) │   任务/目标/记忆/扩展/RAG/委托      │
                │   └───┬─────┘   run_code成功 ──► 技能日志 ─┐      │
                │   混合路由: 简单→本地小模型                    │      │
                │   ┌───▼─────┐                                ▼      │
                │   │ 协作AI   │        心跳: 整理记忆 / 好奇心探索      │
                │   │ 记忆管家 │             技能收割→审批队列 ✨       │
                │   └─────────┘             反思💡/日报📊/进化🧬      │
                │        └──► memory.db(FTS5+向量+知识库+会话)      │
                │             调度线程: cron/重试/任务链/通知          │
                └──────────────────────────────────────────────────┘
```

### 主控AI 工具清单(17)

`remember / recall / call_assistant / write_extension / call_extension / list_extensions / run_code / add_task / list_tasks / update_task / run_task_now / delete_task / add_goal / list_goals / update_goal / delete_goal / ingest_knowledge / search_knowledge`(+配置同伴后 `delegate_to_agent`)

### 关键机制

- **审批门**:`auto_approve_extensions: false`(默认)时 AI 写的扩展进入 pending/,UI 扩展页查看代码后批准/拒绝;覆盖安装自动备份;全程审计(`data/ext_audit.jsonl`)。
- **技能收割**:每次 run_code 成功记入 `data/skills_log.json`;心跳按 `skill_harvest.every_hours` 让主模型挑出值得固化的片段 → 生成扩展候选(走审批门)。
- **好奇心**:`curiosity.sources` 配置 rss/url/folder 源,每日 `explores_per_day` 次预算,发现写入记忆(来源=好奇心引擎)并播报💡。
- **目标系统**:设定目标 → 主控AI 自主拆解(add_task/remember)→ 心跳按 `check_every_hours` 回顾推进 → 进展卡片 + 日报;UI 目标页可手动检查/暂停/完成。
- **提示词进化**:`prompt_evolution.enabled: true` 开启;定期从近期对话取样,候选"附加准则"与现行版本 A/B 对比(LLM 评分),胜出才应用;`GET/POST /api/prompt` 可查看/手改,`/api/prompt/reset` 回滚。
- **上下文压缩**:会话超过 ~28 条时,早期消息由协作模型压缩为滚动摘要注入系统提示,长对话不失忆。
- **A2A**:`peer_agents` 配置同伴实例;主控AI 用 `delegate_to_agent` 同步委托;对端需开放 `/api/a2a`(仅本机回环)。
- **接入渠道**:`inbox.email`(IMAP/SMTP)与 `inbox.telegram`;**只响应 allow 列表内的发件人**;入站消息走无人值守模式并原渠道回复。

## 目录

```
heart-agent/
├── main.py             # 入口(装配/热重载/日志轮转)
├── runner.py           # 扩展/代码任务子进程执行器
├── agentcore/
│   ├── brain.py        # 主控AI(FC/流式/路由/压缩/防注入)
│   ├── assistant.py    # 协作AI(记忆管家)
│   ├── memory.py       # SQLite(FTS5+向量+知识库+会话)
│   ├── goals.py        # 目标系统(OKR)
│   ├── skills.py       # 技能日志(收割原料)
│   ├── rag.py          # 知识摄入(文件/URL→分块→嵌入)
│   ├── evolve.py       # 提示词自进化(A/B+评审)
│   ├── inbox.py        # 外部接入(邮件/Telegram)
│   ├── extensions.py   # 扩展(审批门/版本/审计/Job限制)
│   ├── tasks.py        # 任务(cron/重试/链/并发/通知)
│   ├── heartbeat.py    # 心跳(学习/好奇/反思/日报/收割/热重载)
│   ├── events.py / server.py / notify.py / usage.py / llm.py
├── web/index.html      # 双屏前端(目标页/DAG流程图/审批面板)
├── extensions/         # 动态扩展 + pending/ + versions/
└── data/               # memory.db / tasks.json / goals.json / 日志 / 审计 / 用量
```

## 测试与质量保障

```bash
python -m unittest discover tests -v        # 19 个单元测试(工具序列化/cron/记忆/围栏/评估裁判)
python smoke_test.py                        # 全链路接口冒烟(默认 8765,可用 HEARTAGENT_TEST_URL 覆盖)
python smoke_test.py --real                 # 追加真实模型端到端回归(需已配 Key)
```

> 实战教训已固化为测试:工具表元组/字典双格式序列化(真实 Key 首跑才暴露的崩溃)、能力围栏子进程拦截、推理模型流式 reasoning_content 等。

## 故障排查

| 症状 | 原因与处理 |
|---|---|
| 对话收到"(mock 模式)…"回复 | 发消息时 Key 尚未保存/热重载未生效;保存 config.json 后等 30s 心跳重载,或重启 |
| 消息一直"思考中"无任何事件 | 推理模型(如 deepseek-flash)深思考属正常,右屏会显示"💭 深度推理中";确认卡死点停止按钮;持续无事件看 `data/heartagent.log` |
| "❌ 主模型 连通失败"启动自检 | 检查 config.json 的 api_key / model 名(DeepSeek 只有 deepseek-chat、deepseek-reasoner 等)/ base_url;改完保存即热重载 |
| 端口被占用 | `python main.py --port 9000` |
| 找不到 main.py | 必须在 heart-agent 目录内运行,或直接双击 `start.bat` |
| 健康分突降 | 评估台按用例断言行为;换模型后行为不同属正常,按新模型调整用例 |

## 安全说明

扩展与代码任务子进程执行(超时 + 512MB Job 限制)但**并非沙箱**;AI 生成的扩展默认需人工批准。服务只绑定 `127.0.0.1`;接入渠道务必配置白名单;`/api/a2a` 只应在可信的本机实例间使用。
