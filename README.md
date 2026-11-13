# astrbot_plugin_suite · AstrBot 功能套件（电费 / 游戏资讯 / 备忘录）

> 一个插件装齐三个模块：**宿舍电费监控预警**、**游戏资讯聚合**、**备忘录定时提醒**。
> 模块可全局开关，也可按配置档单独启停（`/套件`）；旧单体插件的配置与数据首次启动自动迁移。

> 📦 仓库归档：旧单体插件仓库 `astrbot_plugin`（电费）与 `astrbot_plugin_games_monitor`
> （游戏资讯）已合并进本仓并于 2026-11-12 删除，**本仓库是唯一来源**。

> ⚠️ 框架约束（宿主按「装饰器函数的定义模块」归属 handler）：所有 `@filter` 装饰器
> 物理写在仓库根 `main.py` 的 `SuitePlugin` 上；`modules/<模块>/service.py` 只保留
> 无装饰器的 Core 方法体，三个模块互不依赖。

## 三模块一览

| 模块 | 指令 | AI 工具 | 功能 |
|---|---|---|---|
| `electric` 宿舍电费 | `/电费 …`（16 条） | `dorm_electric_*`（10 个） | 汉江师范学院缴费系统余额轮询、低余额/紧急预警、每日播报、AI 对话绑定、WebUI 仪表盘 |
| `games` 游戏资讯 | `/游戏 …` | `games_*`（3 个） | 《重返未来：1999》《明日方舟》官方公告/卡池/维护/兑换码、B站官方动态、扫码登录、AI 要点摘要 |
| `memo` 备忘录 | `/备忘 …` | `memo_*`（3 个） | 会话级待办 + 定时提醒（「明天 8:00 交作业」「30分钟后 起锅」到点自动推送） |

## 安装

AstrBot WebUI → 插件 → 从 GitHub 安装 → 填入本仓库地址。依赖见
`requirements.txt`（`apscheduler`、`httpx` 等，宿主一般已内置）。

## 快速开始

```text
/套件                  # 看三模块开关状态（全局 + 本配置档）
/电费 帮助             # 电费指令一览；绑定：/电费 绑定 → 校区 → 楼栋 → 楼层 → 房间 → 绑定 1
/游戏                  # 两游戏 AI 要点；/游戏 1999 卡池、/游戏 方舟 兑换、/游戏 订阅 全部
/备忘 添加 明天 8:00 交作业   # 带时间 → 到点推送；/备忘 交作业 → 纯待办
```

也可以直接跟机器人说话（AI 工具在幕后干活）：

- 「春雪楼817 电费多少」 → 反查任意宿舍双费种余额（群里可用，只读）
- 「低于 20 提醒我」「别提醒了」 → 自定义预警线 / 预警静音
- 「帮我绑定到 A-8-01」 → 私聊发确认码，一码完成改绑
- 「提醒我明天 8 点交作业」 → 备忘录记账，到点自动推送本会话

## 指令集

### /电费（16 条）

`帮助`、`状态`、`检查`、`查询`、`凭证`（仅管理员私聊）、`历史`、`日志`、
`绑定`（仅私聊）、`确认`（仅私聊）、`静音`（仅私聊）、`预警线`（仅私聊，支持
`预警线 空调 15` 单费种）、`校区`、`楼栋`、`楼层`、`房间`（无参翻页 / `p<页码>` 跳页 /
纯数字按全楼层绝对编号选择）、`解绑`（仅私聊）。

### /游戏

裸指令 + 内部路由：`帮助`、默认视图（AI 要点）、`列表`（逃生口，永远原始列表）、
`卡池`、`兑换`、`版本`、`订阅 <游戏|全部|关闭>`。

### /备忘（别名 待办 / todo / memo）

`帮助`、`添加 [时间] 内容`、`列表`、`完成 编号`、`删除 编号`；未识别的子指令
兜底当内容记下（`/备忘 交作业` = `/备忘 添加 交作业`）。时间写法：
`21:30`、`明天 8:00`、`后天 9点半`、`30分钟后`、`3天后`、`10月1日 9:00`；
不带时间就是纯待办。到点推送**发送成功才记账**，失败下轮自动重发。

### /套件（管理员）

`状态`（无参）：三模块「全局 开关 + 本档 开关」一览；
`模块 <电费|游戏|备忘> <开|关>`：全局开关（保存配置，重启后后台轮询不再启动）；
`档案 <档名> <模块> <开|关|默认>`：按配置档覆盖（存 `profiles.json`，指令拦截立即生效）。

## AI 对话工具（16 个）

| 工具 | 说明 | 群聊 |
|---|---|---|
| `dorm_electric_browse` / `pick` | 绑定向导浏览/选择 | 只读可用 |
| `dorm_electric_bind_room` / `unbind` / `confirm` | 绑定/解绑/提交确认码（写操作） | 拒绝，引导私聊 |
| `dorm_electric_balance` | 余额 + 预警线 + 撑几天 | 可用 |
| `dorm_electric_query_room` | 按房间名反查任意宿舍余额 | 可用 |
| `dorm_electric_mute_alerts` / `set_alert_threshold` | 预警静音 / 自定义预警线 | 拒绝 |
| `dorm_electric_check` | 凭证/绑定/余额/事件流自检 | 可用 |
| `games_news` / `games_codes` / `games_dynamics` | 资讯 / 兑换码 / B站动态 | 可用 |
| `memo_add` / `memo_list` / `memo_complete` | 记备忘 / 列出 / 完成 | 可用（按会话隔离） |

写操作（绑定/解绑/改绑）必须用户亲手把 6 位验证码发回来才生效；验证码只由机器人
直发私聊，任何工具输出都不回显码与 Cookie。查询类工具不写历史（不稀释统计）。

## 配置

`_conf_schema.json` 四组：`modules`（三个总开关）、`electric`、`games`、`memo`。
重点字段：

- electric：`hjnu_cookie`（凭证，WebUI 或 `tools/extract_cookie.py` 更新）、
  `poll_interval_minutes`（轮询）、`keepalive_interval_minutes`（独立保活探测）、
  `credential_confirm_min_span_minutes`（凭证失效判定时间窗，默认 30 分钟）、
  预警线（通用 + 按费种）、`ai_tools_enabled` / `ai_prompt_hint`
- games：`push_enabled`、轮询间隔、`bilibili_sessdata`（扫码登录自动写入）、
  `ai_summary_enabled`（后台预生成 AI 要点）
- memo：`memo_scan_seconds`（扫描间隔）、`memo_keep_done_days`、
  `memo_max_per_session`、`ai_memo_tools_enabled` / `ai_memo_prompt_hint`

「聊到电费/游戏/备忘时在幕后提示 AI 用工具」默认开启：关键词命中才注入
system_prompt，平时零额外 token。

## 从旧单体插件迁移（自动）

首次 `initialize` 时（`modules/migrate.py`）：

1. **配置**：`data/config/astrbot_plugin_{dorm_electric,games_monitor}_config.json`
   合并进套件配置的 `electric` / `games` 组；套件里已有的非空值优先（重装/手工
   配置过时旧值不回写覆盖）。旧文件改名 `*.migrated-<时间戳>` 留底。
2. **数据**：`data/plugin_data/astrbot_plugin_{dorm_electric,games_monitor}/` 下的
   数据文件复制到 `data/plugin_data/astrbot_plugin_suite/{electric,games}/`，
   **原件保留**可随时回滚。

迁移在首次启动日志可见。卸载旧插件前建议先装套件、验证绑定与订阅都在。

## 工作原理

- **注册面**：`main.py` 的包装器只做「模块开关检查 → 转发 Core」，全部业务在
  `modules/<模块>/`（纯逻辑 + 各自独立的 `AsyncIOScheduler`，job id 互不冲突）。
- **配置视图**：宿主给插件的是扁平 `AstrBotConfig`；`modules/config_view.py` 的
  `SectionConfig` 让每个 Core 只看到自己组的扁平 dict，Core 代码与旧单体完全同构。
- **数据目录**：`data/plugin_data/astrbot_plugin_suite/{electric,games,memo}/`。
- **提醒可靠性**：memo 扫描推送、电费预警/失联通知都是「发送成功才记账，失败
  下轮重发」；凭证失效有防误报（91001 阶梯复验换新连接 + 连续两周期确认 +
  连续被拒满 30 分钟时间窗，窗内混杂 5xx/超时窗口加倍）。

## 更新日志

| 版本 | 要点 |
|---|---|
| v1.2.0 | 游戏：`/游戏 静音 [小时]`（群聊仅管理员，解除随时可逆）；AI 摘要落盘（重启秒出）；兑换码补发队列落盘（重启不丢、补发滤过期码） |
| v1.1.2 | 游戏：兑换码落盘回归修复（无订阅者也入库即刻落盘，新码才写不浪费 IO）；查询缓存（60 秒内重复提问不打官网，配置 `query_cache_seconds`）；两游戏查询并行；改订阅不再冲掉静音 |
| v1.1.1 | 游戏：兑换码广播改「发送成功才记账」（失败进 pending 下轮补发，静音/无订阅者不算失败）、轮询逐源隔离（一处炸不拖垮整轮）、B 站动态全部送达才记已读；备忘录存储补 fsync |
| v1.1.0 | 电费：修「学校波动造成凭证假失效」（阶梯复验换新连接 / 连续确认 / 30 分钟时间窗 / 混杂窗口加倍，`/电费 检查` 显示判定进度，探测全关不再硬说失效）；事件流按会话隔离、反查缓存 TTL 清扫、历史采样增量裁剪；新配置 `credential_confirm_min_span_minutes` |
| v1.0.0 | 套件诞生：合并电费（v1.3.0 基线）与游戏资讯（v1.1.0 基线）两插件为单插件三模块；新增备忘录模块（待办 + 定时提醒 + 3 个 AI 工具）；`/套件` 模块开关与配置档覆盖；旧插件配置与数据自动迁移；全历史去敏（IP/账号/密码 0 命中） |

详细变更见 [CHANGELOG.md](CHANGELOG.md)。
