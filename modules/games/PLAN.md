# astrbot_plugin_games_monitor · 游戏资讯监控插件 计划书

> 定稿日期：2026-10-19。本文档为完整实施计划，**已于 2026-10-19 实施完毕**。
> 数据源全部经过真机实测验证（2026-10-19），调研结论直接写在第四节，无需重新调研。
>
> **后续版本**：v1.1.0（同日）按主人需求追加「AI 后台实时分析、前台快速调用」——
> `/游戏` 默认输出改为后台预生成的 AI 要点摘要、聊天输出去链接化、
> 修掉「关推送则轮询不启动」与「无订阅者不扫码」两处 v1.0.0 遗留。
> 详见 CHANGELOG.md [1.1.0]。
>
> ⚠️ **实施过程中对本文的三处修正**（真机实测推翻，代码按修正后的写）：
> 1. §4.3 方舟 B站 UID `161432940` → **`161775300`**（前者是同名的个人号「hc9326」）
> 2. §4.3 feed 请求只带 `host_mid`+`wts` → **必须带 `timezone_offset`/`platform`/`web_location`**，
>    否则静默返回空列表
> 3. §三 用 `command_group` 注册 6 个指令 → **改为 1 条裸指令 + 内部路由**
>    （AstrBot v4.27 指令组无「默认子指令」，`/游戏` 无参会报「参数不足」）
> 详见本文末「实施记录」。

---

## 实施记录（2026-10-19）

### 与计划的偏差

| 项 | 计划 | 实际 | 原因 |
|---|---|---|---|
| 指令注册 | `command_group` + 6 子指令 | 1 条裸指令 `@filter.command("游戏")` + 内部路由 | AstrBot v4.27 只发组名时抛 `ValueError` 打印指令树；`@group.command("")` 名字变成 `"游戏 "` 永不匹配 |
| 群管理员闸门 | `PermissionType.ADMIN` 装饰器 | 运行时 `event.is_admin()` | 装饰器是命令级的，会把 `/游戏 1999` 这类公开查询也一起拦住；一条裸指令内部分发只能运行时判 |
| 卡池空态 | 直接说「没命中」 | 退给最近的限时活动 + 时间段 | 实测两个游戏当前都没有卡池公告，关键词空态几乎必然发生；用户真正想问的是「现在有什么活动」 |
| 关键词匹配 | 全文匹配 | 先看标题，全不命中才退全文 | 真跑抓到：正文里到处是「开启/联动」，把「1999制作组播报」选进了活动列表 |
| 时间引导词 | 一张表 | 拆成 `ACTIVITY_CUES` / `MAINTENANCE_CUES` | 真跑抓到：维护公告的「维护时间：…」被当成卡池活动时间推给用户 |
| 目录结构 | `pages/` 通用 | `pages/login/` | v1 只有扫码登录一个页面 |
| 测试目标 | ≥60 用例 | 271 用例 | 含 5 个真跑才发现的 bug 的回归测试 |

### 真跑渲染抓到、单测抓不到的 5 个 bug

1. `fold()` 会吃掉空格 → `parse_route` 把 `1999 卡池` 压成一个词，子指令全部失配
2. `RANGE_RE` 多包了一层捕获组 → 时间段左右两段的下标整体错位，`_fmt` 拿到 `None`
3. HTTP 退休池里 `_safe_aclose` 漏了 `await` → 悬挂协程，连接一个都没关（正是本模块要防的泄漏）
4. B站 `code=0` 但 items 为空是风控的典型表现，原实现直接当"没动态"
5. B站对未登录请求把 `major.opus` 裁成 `null`，纯图片动态渲染成空壳

前三个是纯代码错误（但单测全绿），后两个是数据源现实。**都已被回归测试钉住**。

### 部署验证清单

- `/游戏` → 两游戏各 5 条；`/游戏 帮助` 显示的轮询频率与配置一致
- `/游戏 1999 卡池` → 当前应落到「最近没看到卡池相关的公告，先给你最近的活动」
- `/游戏 方舟 版本` → 每条带 `🕒` 维护时间窗口（如 `2026年10月16日 16:00 - 16:10`）
- `/游戏 1999 兑换` / `/游戏 方舟 兑换` → 「没有兑换码QWQ」（无新码时的正确形态）
- `/游戏 原神` → 「没认出是哪个游戏」+ 帮助
- WebUI 扫码登录页出现二维码 → 扫码 → 状态变「✅ 已登录」，配置里 `bilibili_sessdata` 被写入
- 扫码后 `/游戏` 的 AI 工具 `games_dynamics` 应能取到纯图片动态的文字正文
- 群里非管理员 `/游戏 订阅 全部` 被拒；管理员可订阅
- `/api/v1/logs/history` 搜 `games_monitor` 看 provider 失败与退避日志


---

## 一、项目定位

AstrBot 插件：实时聚合《重返未来：1999》《明日方舟》官方资讯（公告/活动/新闻），
覆盖**卡池日期、新版本/版本更新、新角色、前瞻兑换码、B站官方动态**；
支持指令查询、AI 对话工具、群订阅推送、WebUI 扫码登录 B站。

- 插件名：`astrbot_plugin_games_monitor`，主指令 `/游戏`
- 宿主：腾讯云 `203.0.113.10` 的 AstrBot v4.27.2（与 astrbot_plugin_dorm_electric 同实例）
- 仓库：`github.com/Cecilian-Elysian/astrbot_plugin_games_monitor`（main 分支，WebUI 从 GitHub 安装）
- 首发 v1.0.0，两游戏；架构做 provider 注册表，后续加游戏（原神/星铁/绝区零等）= 加模块

## 二、指令集（最终版，6 个）

| # | 指令 | 权限 | 行为 |
|---|---|---|---|
| 1 | `/游戏 帮助` | 所有人 | 帮助文案（读配置显示轮询频率等） |
| 2 | `/游戏 [游戏名]` | 所有人 | 无参=两游戏最新资讯各 5 条；带参=该游戏最新 5 条（混合分类，带分类标签+日期+链接） |
| 3 | `/游戏 <游戏名> 卡池` | 所有人 | 命中「卡池/寻访/唤醒/新干员/新神秘学家」的近期公告 + 正则提取活动时间段（如「10月16日 16:00 - 10月30日 03:59」） |
| 4 | `/游戏 <游戏名> 兑换` | 所有人 | 兑换码列表（B站动态自动提取 + 公告正文兜底扫描），含来源/时间/过期提醒；**空态固定输出「没有兑换码QWQ」**；别名：`码`/`兑换码` |
| 5 | `/游戏 <游戏名> 版本` | 所有人 | 命中「版本更新/停服维护/前瞻/制作组通讯」的近期公告 |
| 6 | `/游戏 订阅 <游戏\|全部\|关闭>` | **群内仅管理员/群主；私聊人人可用** | 开关本会话推送（新公告/新码/新动态） |

- 游戏名宽松归一（去空格、全半角、大小写不敏感）：
  - `重返未来` / `重返未来1999` / `1999` / `九九` / `重返` / `re1999` → re1999
  - `方舟` / `明日方舟` / `ak` / `arknights` → arknights
- 二级指令不带游戏名时（如 `/游戏 卡池`）宽松处理为两游戏合并，不报错
- B站动态**不设指令**，走推送 + AI 工具
- 群管理员判定：用 AstrBot 权限装饰器（`PermissionType.ADMIN`），实现时以宿主实际 API 为准，conftest 桩同步补

## 三、AI 工具（3 个 llm_tool，只读）

| 工具全名 | 签名 | 说明 |
|---|---|---|
| `game_news` | `(game, category, keyword)` | 查资讯，keyword 支持卡池/版本等关键词检索 |
| `game_codes` | `(game)` | 查兑换码 |
| `game_dynamics` | `(game)` | 查 B站官方动态 |

- docstring **必须写 `Args:` 段**（AstrBot 只从 docstring 解析参数 schema，类型限 string/number/object/array/boolean；参数都给默认值——宿主生成的 schema 无 required）
- 返回值语义（dorm 插件已验证）：一律 `return str` 喂 LLM；`yield event.plain_result` 是直接发给用户、LLM 收不到
- 订阅（写操作）不接 AI，避免权限链复杂化
- `on_llm_request` 幕后提示钩子：聊到「卡池/兑换码/版本/1999/方舟」等关键词时在 system_prompt 末尾追加固定提示（必须调工具/严禁编造），幂等 + try/except 全包 + `getattr(filter,"on_llm_request",None)` 防御宿主不支持（同 dorm v1.1.7 模式），配置开关 `ai_prompt_hint`

## 四、数据源（已实测验证，含坑位记录）

### 4.1 明日方舟官网（arknights.py）— 稳定性：高

```
GET https://ak.hypergryph.com/api/news?category={CAT}&page={n}
CAT ∈ LATEST | ANNOUNCEMENT(公告) | ACTIVITY(活动) | NEWS(新闻)
响应: {"code":0,"data":{"list":[{cid,tab,sticky,title,author,displayTime,cover,extraCover,brief}],"total":N,"end":bool}}
tab: "0"=公告 "1"=活动 "2"=新闻；displayTime 为 Unix 秒
详情页: https://ak.hypergryph.com/news/{cid}（Next.js SSR HTML）
```
- curl 可直连，无任何风控；UA 随意
- 接口是官网 SPA 自己的后端（从 `web.hycdn.cn/.../app/news/page-*.js` 里挖出），多年未变

### 4.2 重返未来：1999 官网（re1999.py）— 稳定性：较高

```
POST https://re.bluepoch.com/activity/official/websites/information/query
Content-Type: application/json; charset=utf-8
Referer: https://re.bluepoch.com/home/    Origin: https://re.bluepoch.com
body: {"id":"","informationType":"","current":1,"pageSize":8,"isTop":""}
informationType: ""=最新 2=公告 3=活动 4=新闻
响应: {"code":200,"msg":"成功","data":{"pageData":[{id,title,shortTitle,collectionId,status,
      onlineTime:"YYYY-MM-DD HH:MM:SS",offlineTime,content(HTML全文)}],"total":N}}
详情链接: https://re.bluepoch.com/home/detail.html#newsId?{id}
```
- ⚠️ **必须用 Python TLS（urllib/httpx 均可过腾讯 EdgeOne）**：curl 的 TLS 指纹被 EdgeOne 拦截，返回 `{"code":5001,"msg":"系统繁忙，请稍后再试"}`（参数怎么改都没用，不是参数问题）
- ⚠️ 列表接口**直接含公告全文 HTML**（content 字段），无需二次请求详情
- 正文是 HTML，展示前要剥标签/截断；码兜底扫描也直接用 content
- 相关端点备用：`/activity/official/websites/picture/banner/query`（轮播图）、`/activity/official/websites/collection/query`（分类）

### 4.3 B站官方动态（bilibili.py）— 稳定性：中（无登录访问，风控不透明）

**官方号 UID**：1999 官方 = `1197454103`（官网外链确认）；方舟官方 = `161432940`（实现时再核验一次）

**动态拉取链路（实测通过，code=0 返回 13 条）**：
```
1. GET https://api.bilibili.com/x/frontend/finger/spi
   → data.b_3 即 buvid3（b_4 即 buvid4）
2. GET https://api.bilibili.com/x/web-interface/nav（未登录）
   → data.wbi_img.img_url / sub_url → 取文件名去扩展名得 img_key/sub_key
   → mixin_key = 按 64 位重排表从 (img_key+sub_key) 取字符，截前 32 位
3. params = {"host_mid": uid, "wts": int(time)}
   q = urlencode(sorted(params.items()))
   w_rid = md5(q + mixin_key)
4. GET https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?{q}&w_rid={w_rid}
   Header: Cookie: buvid3={b_3}   Referer: https://www.bilibili.com/
响应: {"code":0,"data":{"items":[...]}}
字段: modules.module_dynamic.desc.text（文本动态正文）
      modules.module_dynamic.major.archive（视频投稿：title/bvid）
      modules.module_tag.text（置顶标记）  modules.module_author.pub_ts（发布时间）
```

wbi 重排表（64 int，B站公开算法，多年未变）：
```
[46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,
 12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,
 57,62,11,36,20,34,44,52]
```

⚠️ **实测踩坑（重要）**：
- **只挂 `buvid3` 一个 cookie 最稳**——实测带上主站 `b_nut` 等多余 cookie 反而触发 -352
- -352 风控是**间歇性**的（同配方时好时坏），必须指数退避重试（单轮上限 2~3 次）
- wbi key 有有效期（B站会轮换），每次拉取现取现签，不做长缓存（可缓存 1~2 小时）

**兑换码提取**：动态正文正则 `r"(?:兑换码|礼包码|激活码)[码:：\s]*([A-Za-z0-9]{6,16})"`（多条码分开提取），去重入库；文本含「有效期/截止/过期」则解析出过期时间，否则提示「直播码一般次日失效，速兑」。

### 4.4 扫码登录获取 SESSDATA（webui.py）— 便利功能

```
1. GET https://passport.bilibili.com/x/passport-login/web/qrcode/generate
   → data.qrcode_key + data.url（二维码内容）
2. 页面渲染二维码（前端 JS 库，CDN+SRI，服务端零新 pip 依赖）→ 用户 B站 App 扫码确认
3. 每 2 秒 poll GET https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key=...
   状态码: 86101=未扫 86090=已扫未确认 86038=已过期(二维码180s失效,页面自动刷新) 0=成功
4. 成功: 响应 data.url 参数 & Set-Cookie 均含 SESSDATA → 插件自动写入配置(secret 字段) → 页面 ✅
```
- 端点经 `register_web_api` 挂 WebUI 鉴权之后（dorm 仪表盘同模式 + getattr 守卫）
- **手动粘贴 SESSDATA 的配置字段保留**（双通道）
- SESSDATA 等于账号登录态：任何聊天输出不得回显（测试加断言）
- 新增 `webui.py` 模块**只干扫码这件事**（v1 不做仪表盘）

### 4.5 零成本增强：公告正文兜底扫码

资讯轮询本来就会拉到两家公告全文 → 顺手用同一兑换码正则扫 content（1999 感谢庆典/前瞻类公告常带码）。
不增加任何请求。价值：B站风控持续抽风时码仍有来源。

## 五、推送机制

- apscheduler `IntervalTrigger`：资讯轮询默认 **10 分钟**（可配 5~120）；B站动态轮询默认 **15 分钟**
- 每轮：各 provider 拉「最新」（方舟 `LATEST`、1999 `informationType=""`）首页 → 与 storage `seen_ids` 比对 → 新条目组装（分类/标题/日期/链接/摘要前 80 字）→ `await self.context.send_message(umo, MessageChain().message(text))` 逐订阅会话发送
- 新码独立文案「🎮 新兑换码 …」即时推送；动态新帖按订阅推送
- ⚠️ **发送成功才记账**（`send_message` 返回 bool；失败不写 seen，下轮重发）——dorm `_flush_alerts` 教训
- ⚠️ 连续失败指数退避 + 单轮上限；学校/官网凌晨抽风不轰炸（dorm 教训）
- 订阅存储：`{umo: {games:[...], dynamics:bool, muted_until:ts}}`；退订即删
- 用户动作全量 `save(force=True)`，轮询侧走去抖（dorm storage 同款）

## 六、目录结构与工程惯例（沿用电费插件）

```
astrbot_plugin_games_monitor/
├── metadata.yaml        # v1.0.0 / Cecilian / desc 完整功能一句话
├── main.py              # @register + 全部指令/llm_tool 装饰器 + 轮询推送编排
├── providers/
│   ├── __init__.py      # NewsItem dataclass + Provider 协议 + 注册表(GAMES)
│   ├── arknights.py     # 方舟（httpx GET）
│   ├── re1999.py        # 1999（httpx POST + 浏览器头）
│   └── bilibili.py      # B站动态 client（spi/wbi 签名/feed + 重试退避）+ 扫码登录 client
├── codes.py             # 码库：提取正则/去重/过期/合并(动态+公告双来源)
├── keywords.py          # 卡池/版本 词表分类 + 时间段提取(纯函数)
├── formatting.py        # 列表/详情/码列表/空态 渲染纯函数
├── storage.py           # seen_ids/订阅/码库/历史 JSON 原子写+fsync+坏文件隔离 *.corrupt-<ts>
├── webui.py             # 扫码登录 2 个端点（generate/poll）
├── pages/               # WebUI 小页面（Bridge apiGet，二维码 JS 库 CDN+SRI）
├── _conf_schema.json
├── requirements.txt     # httpx（apscheduler 宿主自带，复用）
├── pyproject.toml / .github/workflows/ci.yml / README.md / CHANGELOG.md
└── tests/               # conftest 桩（抄 dorm 模式）+ 各模块用例
```

**工程红线（dorm 血泪教训，违反必返工）**：
1. **所有装饰器 handler 留在 main.py**——宿主元数据扫描对 Mixin 未验证，别赌（v1.2.0 曾当场红）
2. 新模块 try/except 双路导入（包内 `from .x` / 兜底 `from x`）
3. 插行必须放在装饰器之上（v1.2.1 装饰器漂移事故）
4. 一切列表方向/分页/合并逻辑**必须真数据渲染验证**（v1.0.7/1.0.8/1.1.0 三次真跑抓 bug）
5. PowerShell 里复杂 python 一律写临时文件或 here-string 喂 stdin，禁内联 `python -c` 多行
6. 服务器 API 调试用 `curl.exe --noproxy "*"`（本机 Clash 劫持教训）；1999 接口调试必须 python

## 七、配置项草案（_conf_schema.json）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| push_enabled | bool | false | 推送总开关 |
| poll_interval_minutes | int | 10 | 资讯轮询间隔（5~120） |
| dynamics_poll_minutes | int | 15 | 动态轮询间隔 |
| arknights_enabled / re1999_enabled | bool | true | 单游戏开关 |
| bilibili_sessdata | string(secret) | "" | B站登录态，WebUI 扫码自动写入或手动粘贴 |
| bilibili_uids | dict | 两官号 | 官方号 UID（可替换） |
| max_items_per_push | int | 5 | 单次推送上限 |
| code_ttl_hint_hours | int | 24 | 无明确过期时间的码的提醒话术参数 |
| history_keep_days | int | 30 | 历史保留 |
| request_timeout_seconds | int | 15 | 请求超时 |
| user_agent | string | 浏览器UA | 全部出站请求共用 |
| ai_tools_enabled / ai_prompt_hint | bool | true | AI 工具与幕后提示开关 |

## 八、测试与质量

- pytest + conftest astrbot 桩（复制 dorm 的 `make_plugin()`/`StubAstrBotConfig` 模式），目标 **≥60 用例**：
  - providers 解析（**用本次实测抓的真实 JSON/HTML 存 fixture**，不用编造样本）
  - wbi 签名 mixin key 纯函数（已知向量断言）
  - 1999 请求头/EdgeOne 失败识别（5001 → 明确报「被风控」而非「接口坏了」）
  - 码提取正则矩阵/去重/双来源合并/过期
  - keywords 分类 + 时间段提取
  - storage 原子写/坏文件隔离
  - 推送编排：新条目检测/发送失败不记账/订阅过滤/管理员闸门
  - 指令注册面冒烟（`__dorm_command__` 元数据法，锁死「6 指令 + 3 工具」清单）
- `ruff check`（含 B 规则组）+ GitHub Actions CI（ruff+pytest，抄 dorm）
- 真跑渲染验证（复用 conftest 桩 + 假 quart）覆盖：`/游戏`、`/游戏 1999 卡池`、空码「没有兑换码QWQ」、订阅闸门、扫码端点状态机

## 九、风险与维护预期（对用户的承诺口径）

| 渠道 | 何时会坏 | 坏了影响 | 维护动作 |
|---|---|---|---|
| 方舟官网 | 官网大改版（年级别） | 只影响方舟查询 | 改 arknights.py 一个文件 |
| 1999 官网 | 改版 / EdgeOne 收紧到拦 Python | 只影响 1999 查询 | 改 re1999.py；极端情况换 curl_cffi 依赖 |
| B站动态 | 半年~一年一次概率（-352/wbi 变更） | **只影响码提取+动态推送**，公告/卡池/版本不受影响 | 改 bilibili.py；SESSDATA 可大幅缓解 |

- 方舟**没有兑换码体系**（补偿走游戏内邮件）→ `/游戏 方舟 兑换` 长期「没有兑换码QWQ」是数据源现实
- 关键词偶尔漏新说法 = 展示缺漏非故障，随发现随补词表
- 正常预期：**半年到一年动一次代码**，大概率是 B站那头，单文件隔离修复

## 十、实施顺序（checklist，2026-10-19 全部完成）

1. ☑ 脚手架：目录/metadata/_conf_schema/空 main/CI/pyproject/ruff 配置
2. ☑ providers 三件套 + fixture 测试（先 arknights → re1999 → bilibili，每个过测再下一个）
3. ☑ codes.py / keywords.py / formatting.py / storage.py + 单测
4. ☑ 指令 6 条行为 + 注册面冒烟测试（**实现为 1 条裸指令 + 内部路由**，见文首修正 3）
5. ☑ AI 工具 3 个 + 幕后提示钩子
6. ☑ 轮询推送编排 + 编排测试
7. ☑ webui.py 扫码 + pages/login/ + 状态机测试
8. ☑ 全量 pytest（271 例）+ ruff + 真跑渲染验证（`tools/smoke.py`）
9. ☑ README（指令表/配置表/工作原理/SESSDATA 获取两法）+ CHANGELOG
10. ☑ 四处版本同步（CHANGELOG 节 + README 摘要 + metadata.yaml + main.py @register）
11. ☑ commit + push（`ac44c26`，main 为默认分支；部署由用户在 WebUI 操作，装完跑文首的部署验证清单）

**部署验证清单**：见本文开头的「实施记录 → 部署验证清单」。

## 十一、已知限制

- 码的手动登记通道按用户决策**不提供**（唯一来源=自动提取：B站动态 + 公告正文兜底）
- B站动态风控持续拦截且未配 SESSDATA 时，码与动态功能可能暂时哑掉（有明确报错提示，不影响其他功能）
- v1 不做：仪表盘、折线图、多语言服（只国服）、原神系游戏
