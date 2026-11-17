# zFrontier 抽签福利自动参与系统 —— 需求文档

| 项目 | 内容 |
|---|---|
| 项目代号 | `zf_lucky` |
| 目标站点 | https://www.zfrontier.com |
| 部署环境 | `root@129.146.58.153` / Ubuntu 24.04 / aarch64 / 4C24G |
| 文档版本 | v1.0 |
| 状态 | **需求确认稿（待评审，未开工）** |
| 编写日期 | 2026-11-17 |

---

## 1. 项目概述

### 1.1 背景

zFrontier（装备前线）的「情报」专栏（tag id = `2007`）中，部分帖子带有「抽签福利」活动。用户需要在这些帖子下**回复**才能参与抽签，开奖时由站方「@抽签助手」私信通知中奖者。

情报专栏日更约 5 条，其中约 50% 是抽奖帖。人工每天刷一遍、逐帖判断并回复，重复且容易漏。

### 1.2 目标

构建一个**单账号、每日运行一次**的自动化服务：

1. 自动扫描「情报」专栏，发现新的抽奖帖；
2. 对未参与且仍在抽奖期的帖子，自动完成「关注作者 → 回复 → 点赞」；
3. 用本地数据库记录参与状态，保证**不重复、不遗漏**；
4. 提供最小可视化界面，展示帖子状态与参与结果。

### 1.3 核心约束

- **单账号**，不做多账号；
- **每天固定时间运行一次**；
- **绝不程序化破解验证码**（滑块拼图），触发即暂停等人工处理；
- 所有操作走**真实浏览器**（复用现有 Selenium 容器），不直接构造裸 HTTP 请求实现关键动作。

---

## 2. 术语定义

| 术语 | 含义 |
|---|---|
| **抽奖帖** | 帖子详情 JSON 中存在 `flow.lottery` 字段的帖子 |
| **有效抽奖帖** | 抽奖帖且 `flow.lottery.status == 1`（待抽奖） |
| **参与** | 完成「关注作者 + 回复」两个动作。点赞仅作为已完成的外部可见标记 |
| **已参与** | 本地数据库 `participations.state == 'done'` |
| **倒挂** | 列表中出现「后一条的发帖时间比前一条更新」的乱序现象 |
| **覆盖率水位** | 本地记录的「已成功扫描到的最早发帖时间」，用于增量扫描 |

---

## 3. 系统边界与非目标

### 3.1 明确不做

| 非目标 | 原因 |
|---|---|
| ❌ 程序化破解滑块验证码 | 站点明确的「证明你是人」机制；破解属对抗升级，且不可持续 |
| ❌ 多账号 / 批量养号 | 性质从「个人自动化」变为「刷抽奖」，风控关联风险高 |
| ❌ 自动兑奖 / 填写收货信息 | 中奖后走站内私信，人工处理 |
| ❌ 外部通知（Telegram/邮件/Bark 等） | 用户明确要求只看面板 |
| ❌ 取消关注（事后取关） | 用户未要求；且取关是公开社交动作 |
| ❌ 自动发帖 / 转发 / 收藏 | 无此需求 |
| ❌ 翻遍全部 18823 条历史 | 不现实且无意义（见 §6.3） |

### 3.2 系统边界

```
┌─────────────────────────────────────────────┐
│  zf_lucky 应用容器（Python）                  │
│  ├─ 调度器（每日定时）                        │
│  ├─ 扫描器（情报列表）                        │
│  ├─ 判定器（抽奖识别）                        │
│  ├─ 执行器（关注/回复/点赞）                  │
│  ├─ SQLite（状态存储）                        │
│  └─ Web 面板（状态展示）                      │
└──────────────┬──────────────────────────────┘
               │ WebDriver (HTTP/4444)
               ▼
┌─────────────────────────────────────────────┐
│  bili-selenium（已有，selenium/standalone-  │
│  chrome:157.0-20261101，原生 aarch64）       │
│  + noVNC（127.0.0.1:7900）供人工介入         │
└──────────────┬──────────────────────────────┘
               │ HTTPS
               ▼
        www.zfrontier.com
```

**复用而非新建**：直接使用现有 `bili-selenium` 容器（已在 `bili-net` 网络），不新建浏览器容器。

---

## 4. 功能需求

### FR-1 定时调度

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-1.1 | 每天在固定时间（默认 `09:00`，可配置）自动执行一次完整流程 | P0 |
| FR-1.2 | 支持通过面板手动「立即运行」（不受定时限制，但需防并发） | P1 |
| FR-1.3 | 同一时刻只允许一个任务实例运行，重入时忽略新请求 | P0 |
| FR-1.4 | 单次任务失败不影响进程存活，不得导致容器重启循环 | P0 |
| FR-1.5 | 时区固定为 `Asia/Shanghai` | P0 |

### FR-2 抽奖帖扫描（发现阶段）

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-2.1 | 打开情报专栏页面 `https://www.zfrontier.com/app/tag/2007` | P0 |
| FR-2.2 | 从页面 Vue 实例数据中读取列表（`tabList.normal.list`），**不调用列表 API**（理由见 §5.2） | P0 |
| FR-2.3 | 通过滚动触发瀑布流加载更多 | P0 |
| FR-2.4 | 按**时间边界 + 安全边距**判定扫描结束（规则见 §6） | P0 |
| FR-2.5 | 收集字段：`hash_id`、`title`、`created_at`、`user.id`、`user.hash_id`、`user.nickname` | P0 |
| FR-2.6 | 每轮循环设置硬上限（默认 500 条 / 15 次滚动），防御性终止 | P1 |
| FR-2.7 | 列表页加载失败时重试 2 次，仍失败则本轮中止并记录 | P0 |

> **重要**：列表页 DOM 与列表数据中**均无抽奖标记**（无 `.tag.lottery`、无 `is_lottery` 字段），因此**无法从列表直接判断是否抽奖**。必须逐帖查询详情。详见 §5.3。

### FR-3 抽奖判定（筛选阶段）

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-3.1 | 对「本地未判定过」的帖子，查询 `POST /v2/flow/detail {id: hash}` | P0 |
| FR-3.2 | 判定规则：存在 `data.flow.lottery` → 抽奖帖；否则 → 非抽奖帖 | P0 |
| FR-3.3 | 记录抽奖元信息：`lottery.status`、`lottery.lottery_at`、`lottery.filters`、`lottery.prizesGroup` | P0 |
| FR-3.4 | `lottery.status == 1` → 有效抽奖帖；其他值 → 已开奖/已结束，不再参与 | P0 |
| FR-3.5 | 判定结果写入数据库，**永久缓存**，下轮不再重复查询（除非状态需要复查） | P0 |
| FR-3.6 | 对「已判定为有效抽奖但未开奖」的存量帖，每轮复查一次 `status`（廉价，用于感知开奖） | P1 |

> **性能要点**：列表是廉价的（1 次页面加载 + 数次滚动），详情是昂贵的（每帖 1 次请求）。必须用数据库过滤，**每轮真正发出的详情请求应控制个位数**（首次建库除外）。

### FR-4 参与流程（执行阶段）

对每条「有效抽奖帖 且 本地未完成」的帖子，按以下**固定顺序**执行：

| 编号 | 步骤 | 校验要求 | 优先级 |
|---|---|---|---|
| FR-4.1 | **前置健康检查**：确认登录态有效、`identity_pass == 1`（已绑手机） | 任一不满足 → 整轮中止并告警 | P0 |
| FR-4.2 | **关注作者**：检查 `POST /v2/user/detail {id: author.hash_id}` 的 `user.followed` | 读取成功 | P0 |
| FR-4.3 | 若 `followed == 0` → `POST /api/follow {uid: author.id, action: 'follow'}` | 关注后**回读** `followed == 1` 才算成功 | P0 |
| FR-4.4 | 若 `followed == 1` → 跳过关注（已关注） | — | P0 |
| FR-4.5 | **回复**：`POST /v2/flow/reply {id: flow.id, type: 'flow', reply_id: 0, content: <语料>}` | 接口返回 `reply.id` | P0 |
| FR-4.6 | **回复回读校验**：`POST /api/circle/flowReplyList {id, type:'flow', page, sortBy, unfold:1}`，确认列表中**存在本人 uid 的回复** | 必须回读确认，不可仅信任接口返回码 | P0 |
| FR-4.7 | **点赞**：`POST /api/circle/zanFlow {id: flow.id, action: 'add'}` | 点赞后**回读**详情 `flow.has_zan == 1` | P0 |
| FR-4.8 | **落库**：记录 `followed_at`、`replied_at`、`reply_id`、`liked_at`、`state='done'` | — | P0 |
| FR-4.9 | 任一步骤失败 → 记录 `state='failed'` + `fail_reason`，**继续处理下一帖**，不中断整轮 | — | P0 |

**顺序不可调整的理由**：

> 必须是「先回复、后点赞」。这样 `has_zan == 1` 在语义上就蕴含了「回复也已成功」，不会出现「赞了但没回复」的假阳性。若点赞在前、回复在后，中途失败会产生**永久漏帖**（第二天看到已点赞而跳过）。

### FR-5 回复内容

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-5.1 | 回复内容从**语料池**中随机选取，同一天内不重复 | P0 |
| FR-5.2 | 语料池可配置（`.env` 中 `REPLY_CORPUS`，以 `\|` 分隔） | P1 |
| FR-5.3 | 默认语料参考站点自身的快捷回复（`冲冲冲`），但需多样 | P1 |
| FR-5.4 | 回复内容**不得包含**：广告、联系方式、其他平台链接、政治敏感内容 | P0 |
| FR-5.5 | 回复长度需满足站点限制（待实测确认最小/最大字符数） | P0 |

**默认语料池（初始值，可配置）**：
```
冲冲冲 | 来了来了 | 参与一下 | 祝我好运 | 抽我抽我 | 蹲一个 | 支持支持 | 希望能中
```

### FR-6 状态管理与重试

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-6.1 | 每条帖子的参与状态持久化，程序重启不丢失 | P0 |
| FR-6.2 | 失败的帖子：**下一轮自动重试**，最多 3 次 | P0 |
| FR-6.3 | 连续失败 3 次 → 标记 `state='give_up'`，不再自动重试，面板标红 | P1 |
| FR-6.4 | 支持面板「强制重跑」单条帖子（忽略「已完成」状态） | P1 |
| FR-6.5 | 抽奖帖开奖后（`status != 1`），停止跟进 | P0 |

### FR-7 验证码与风控处理

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-7.1 | 检测到响应 `ok == 20001`（操作太频繁）→ 记录并**退避** | P0 |
| FR-7.2 | 检测到页面出现 `.capthcah-overlay` / `.slidercaptcha` → **立即暂停整轮任务** | P0 |
| FR-7.3 | 暂停时在面板显著位置显示告警，指引用户到 noVNC 手动完成 | P0 |
| FR-7.4 | 用户手动完成滑块后，点击验证码触发 `POST /v2/resetRateLimit`（前端自动），任务恢复 | P0 |
| FR-7.5 | **严禁**任何自动破解滑块、轨迹模拟、图像识别的代码 | P0 |
| FR-7.6 | 请求之间插入随机延迟（默认 2–5 秒） | P0 |
| FR-7.7 | 单轮写操作（关注/回复/点赞）总数设上限（默认 30 次），超出则中止 | P1 |

### FR-8 配置管理

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-8.1 | 通过 `.env` 文件配置，容器启动时注入 | P0 |
| FR-8.2 | Cookie 通过 `.env` 配置（支持整段 Cookie 字符串，自动解析为键值对） | P0 |
| FR-8.3 | 账号标识（user id / hash）**自动探测**，无需手工填写 | P0 |
| FR-8.4 | 探测方式：登录后调用 `/v2/user/card` 或从页面读取 | P0 |
| FR-8.5 | Cookie 失效时启动阶段即报错，不进入业务逻辑 | P0 |
| FR-8.6 | 配置项缺失时给出明确中文错误提示 | P1 |

**完整配置项清单**：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ZF_COOKIE` | （必填） | zFrontier 完整 Cookie 字符串 |
| `RUN_AT` | `09:00` | 每日运行时间（HH:MM） |
| `RUN_ON_START` | `false` | 容器启动时是否立即跑一次 |
| `SCAN_WINDOW_DAYS` | `14` | 扫描窗口天数 |
| `SCAN_BUFFER_DAYS` | `2` | 安全边距（应对倒挂） |
| `SCAN_CONFIRM_COUNT` | `10` | 连续 N 条早于边界才停止 |
| `SCAN_MAX_SCROLL` | `15` | 滚动次数硬上限 |
| `SCAN_MAX_ITEMS` | `500` | 条数硬上限 |
| `REPLY_CORPUS` | 见 FR-5 | 回复语料池，`\|` 分隔 |
| `REQUEST_DELAY_MIN` | `2` | 请求最小间隔（秒） |
| `REQUEST_DELAY_MAX` | `5` | 请求最大间隔（秒） |
| `MAX_WRITE_PER_RUN` | `30` | 单轮写操作上限 |
| `MAX_RETRY` | `3` | 单帖最大重试次数 |
| `SELENIUM_URL` | `http://bili-selenium:4444` | Selenium 地址 |
| `DB_PATH` | `/app/data/zf_lucky.db` | SQLite 路径 |
| `PANEL_PORT` | `8080` | 面板容器内端口 |
| `TZ` | `Asia/Shanghai` | 时区 |

### FR-9 Web 面板（Phase 2）

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-9.1 | 单页面，展示帖子表格 | P1 |
| FR-9.2 | 列：标题 / 作者 / 发帖时间 / 开奖时间 / 奖品 / 状态 / 操作时间 / 失败原因 | P1 |
| FR-9.3 | 顶部状态条：登录态、下次运行时间、上次运行结果 | P1 |
| FR-9.4 | 异常告警区（仅异常时显示）：验证码待处理、登录失效、连续失败 | P1 |
| FR-9.5 | 操作按钮：「立即运行」「暂停」「强制重跑此帖」 | P2 |
| FR-9.6 | 只读为主，不对站点发起任何操作（除「立即运行」外） | P1 |
| FR-9.7 | 无鉴权，仅通过反向代理 + 回环绑定暴露 | P1 |

> 面板不是本期重点，主进程跑通后再实现。

---

## 5. 站点技术事实（已验证）

> 本节所有结论均来自**实测**，非推测。未验证项单独标注。

### 5.1 关键接口清单

| 用途 | 方法 | 路径 | 参数 | 已验证 |
|---|---|---|---|---|
| 帖子详情 | POST | `/v2/flow/detail` | `id=<hash>` | ✅ |
| 回复 | POST | `/v2/flow/reply` | `id=<flow.id>`, `type=flow`, `reply_id=0`, `content=<文本>` | ⚠️ 待实测 |
| 回复列表 | POST | `/api/circle/flowReplyList` | `id=<flow.id>`, `type=flow`, `page`, `sortBy`, `unfold=1` | ✅ |
| 点赞 | POST | `/api/circle/zanFlow` | `id=<flow.id>`, `action=add\|cancel` | ⚠️ 待实测 |
| 关注 | POST | `/api/follow` | `uid=<author.id>`, `action=follow\|unfollow` | ⚠️ 待实测 |
| 关注状态 | POST | `/v2/user/detail` | `id=<author.hash>` | ✅ |
| 发布规则 | POST | `/v2/publish/rules` | 无 | ⚠️ 需登录才能读 |
| 用户卡片 | POST | `/v2/user/card` | 无 | ⚠️ 需登录 |
| 限流重置 | POST | `/v2/resetRateLimit` | 验证码 token | ✅（逻辑已验证） |

**通用请求头**（必须）：
```
Content-Type: application/x-www-form-urlencoded
X-CSRF-TOKEN: <window.csrf_token>
X-CLIENT-LOCALE: zh-cn
X-Requested-With: XMLHttpRequest
credentials: include
```

**⚠️ 所有请求必须在页面内同源发起**（`fetch()`），不可用外部 HTTP 客户端直接构造。原因：CSRF token 与 Cookie 会话绑定，同源请求可自动携带，避免手工维护。

### 5.2 列表 API 不可用

`POST /v2/home/flow/list`（参数 `tagIds[]=2007`）实测返回：

```json
{"ok":20001,"msg":"操作太频繁了","data":[]}
```

- 无论匿名还是有 Cookie、无论是 curl 还是浏览器内 fetch，均返回 `20001`；
- 但从页面正常渲染出列表 → 说明**页面走了不同通道**（可能是 SSR 注入或带内部签名）；
- **结论：不使用列表 API，改为从页面 Vue 实例读取数据。**

### 5.3 列表无抽奖标记

实测情报专栏首页 10 条数据，字段共 26 个：

```
cid, circle, client_str, created_at, deleted, equip_list, flow_type, great,
hash_id, hot, id, imgs, is_disputed, item_id, item_type, manage_menu,
masonry_cover, past_time, stat, tags, text, title, top, url, user, view_url
```

- ❌ 无 `is_lottery` 字段
- ❌ DOM 中无 `.tag.lottery` 元素
- ❌ 页面文本中无「抽签福利」字样

**但前端代码中确实存在 `is_lottery` 的渲染逻辑**（首页侧栏「情报动态」与 board 卡片使用），说明该字段**只在部分接口返回**，tag 列表接口不返回。

**结论：必须逐帖查详情才能判断是否抽奖。**

### 5.4 列表排序规律（实测 60 条 / 12 天）

**整体趋势：按发帖时间从新到旧**，但存在**局部倒挂**。

实测数据（前 10 条）：

| 位置 | 发帖时间 | 被后文超越 | 最大倒挂 |
|---|---|---|---|
| 0 | 11-17 12:47 | 0 | 0 |
| 1 | 11-17 11:09 | 0 | 0 |
| 2 | 11-16 20:10 | **2** | **12.3 小时** |
| 3 | 11-17 08:30 | 0 | 0 |
| 4 | 11-16 23:50 | 0 | 0 |
| 5 | 11-16 15:02 | 2 | 0.8 小时 |
| 6 | 11-16 15:51 | 0 | 0 |
| 7 | 11-16 15:11 | 0 | 0 |
| 8 | 11-16 13:55 | 1 | 0.8 小时 |
| 9 | 11-16 14:41 | 0 | 0 |

**60 条样本统计**：
- 倒挂总对数：**16 对**
- 倒挂幅度：最小 0.2h / 中位 1.2h / **最大 12.3h**
- **超过 24 小时的倒挂：0 对**

**成因推测**：瀑布流两栏高度平衡（`appendToColumns`）、置顶帖、审核延迟发布。

**推论**：**不能"看到第一条早于边界就停"**，必须留安全边距。

### 5.5 发帖速率（实测）

12 天 60 条，**平均 5.0 条/天**。逐日分布：

| 日期 | 条数 | 日期 | 条数 |
|---|---|---|---|
| 11-06 | 7 | 11-12 | 1 |
| 11-07 | 3 | 11-13 | 2 |
| 11-08 | 4 | 11-14 | 5 |
| 11-09 | 9 | 11-15 | 3 |
| 11-10 | 6 | 11-16 | 9 |
| 11-11 | 8 | 11-17 | 3 |

**波动范围：1–9 条/天。**

### 5.6 抽奖比例（实测）

情报专栏首页 10 条中，**5 条是抽奖帖**（50%）：

| # | hash | 是否抽奖 | status | 开奖时间 | 参与条件 |
|---|---|---|---|---|---|
| 1 | `ZwPnZ6lzj5xy` | ✅ | 1 | 2026-12-01 23:51 | 回复 |
| 2 | `mpqrQK865ja5` | ❌ | — | — | — |
| 3 | `Y5L19vMxqKzB` | ✅ | 1 | 2026-11-23 20:00 | 回复 |
| 4 | `bz5K5jK0pGxk` | ❌ | — | — | — |
| 5 | `3xpORp7BQM3W` | ✅ | 1 | 2026-11-18 10:00 | 回复 |
| 6 | `Z5Z0YNBmnp6r` | ✅ | 1 | 2026-11-18 10:30 | 回复 |
| 7 | `7G9RMd35mP5p` | ✅ | 1 | 2026-11-30 10:00 | 回复 |
| 8 | `1kGgJ9dmXYmk` | ❌ | — | — | — |
| 9 | `Yky5m0gzbjM1` | ❌ | — | — | — |
| 10 | `ZYZodOLwlxkX` | ❌ | — | — | — |

> **更正记录**：早期调研曾错误断言「10 条里只有 1 条是抽奖」，实际为 5 条。已逐条核实。

### 5.7 抽奖字段结构

`flow.lottery` 实际返回：

```json
{
  "status": 1,
  "lottery_at": "2026-12-01 23:51",
  "version": 2,
  "status_str": "待抽奖",
  "officially": 0,
  "filters": { "reply": true },
  "time_str": "2026-12-01 23:51",
  "can_edit": false,
  "author": { "id": 3839009, "nickname": "M Z", "hash_id": "BOEeXlzZBG9RX", ... },
  "prizesGroup": [
    {
      "name": "Z1轴60颗",
      "cnt": 6,
      "list": [
        { "id": 44024, "name": "Z1轴60颗", "rewardUser": null },
        ... 共 6 个
      ]
    }
  ]
}
```

**字段说明**：

| 字段 | 用途 |
|---|---|
| `status` | `1` = 待抽奖；其他 = 已开奖/已结束 |
| `lottery_at` | 开奖时间 |
| `filters.reply` | 是否需要回复（实测全部为 `true`） |
| `filters.follow` | 是否需要关注（**当前样本均无此字段**，但代码中存在） |
| `author.id` / `author.hash_id` | 关注所需 |
| `prizesGroup[].list[].rewardUser` | 中奖者；`null` = 未开奖，非 `null` = 已开奖 |

### 5.8 抽奖周期（实测 5 条样本）

| 帖子 | 发帖 | 开奖 | 周期 |
|---|---|---|---|
| `ZwPnZ6lzj5xy` | 11-17 | 12-01 | **14 天** |
| `Y5L19vMxqKzB` | 11-16 | 11-23 | 7 天 |
| `3xpORp7BQM3W` | 11-16 | 11-18 | 2 天 |
| `Z5Z0YNBmnp6r` | 11-16 | 11-18 | 2 天 |
| `7G9RMd35mP5p` | 11-16 | 11-30 | 14 天 |

**最长 14 天** → 这是扫描窗口的取值依据。

### 5.9 登录与 Cookies

- 站点 CSRF token 渲染于页面：`window.csrf_token`
- Cookie 关键项（实测匿名访问）：`acw_tc`、`cdn_sec_tc`（阿里云 WAF）、`ZF_CLIENT_ID`、`_ga*`
- 登录后新增会话 Cookie（具体名称待实测确认）
- Cookie 通过 WebDriver 的 `add_cookie` 注入，或直接读取浏览器上下文

### 5.10 验证码机制

**类型：滑块拼图（slider captcha），非点选。**

DOM 结构：
```html
<div class="capthcah-overlay">
  <div class="captcha flex-center">
    <div class="slidercaptcha card">
      <div class="card-header"><span>请完成安全验证</span></div>
      <div class="card-body">
        <div id="captcha">
          <canvas width="278" height="155"></canvas>   <!-- 背景图 -->
          <i class="refreshIcon fa fa-repeat"></i>
          <canvas width="63" height="155" class="block"></canvas>  <!-- 拼图块 -->
          <div class="sliderContainer">
            <div class="sliderbg"></div>
            <div class="sliderMask"><div class="slider">...</div></div>
            <span class="sliderText">向右滑动完成拼图</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
```

**触发条件**：接口返回 `ok == 20001` 时，前端 dispatch `triggerRateLimit` → 全局弹层。

**恢复机制**：
1. 用户拖动滑块完成验证；
2. `sliderCaptcha` 的 `onSuccess` 回调触发 `$emit('passed', token)`；
3. 前端调用 `POST /v2/resetRateLimit`（带 token）；
4. 成功（`ok == 0`）后 dispatch `resetRateLimit` → 弹层消失；
5. 前端自动**重发**之前被拦截的请求（每 2 秒重试，最多 30 秒）。

**本系统策略**：检测到弹层 → 暂停 → 人工处理 → 自动恢复。**不写任何破解代码。**

### 5.11 其他前端行为（可作为实现参考）

| 行为 | 细节 |
|---|---|
| 抽奖帖快捷回复 | 帖子页对有效抽奖帖（`lottery.status == 1`）显示「点击抽奖」按钮，内容默认 `冲冲冲` |
| 抽奖楼层折叠 | 回复列表默认隐藏抽奖楼层，显示「已为你隐藏 N 个抽奖楼层」，需 `unfold=1` 才全部返回 |
| 回复前置校验 | 发回复前调用 `/v2/publish/rules`，检查 `identity_pass`；为 `0` 时提示「根据监管要求，请绑定手机号后回复」 |
| 限流重试 | 前端遇 `20001` 自动每 2 秒重试，30 秒后放弃 |

### 5.12 提取方式（Vue 实例）

列表数据获取方式（实测可行）：

```javascript
// 从任意 .list-flow 元素向上找到含 tabList 的组件
const roots = document.querySelectorAll('.list-flow');
let vm = roots[0].__vue__;
while (vm && !(vm.$data && vm.tabList)) vm = vm.$parent;
const list = vm.$data.tabList.normal.list;
// 每项含 id, hash_id, title, created_at, user{id,hash_id,nickname}, stat{...}
```

单条卡片的数据（备用路径）：`roots[i].__vue__.$props.flow`

---

## 6. 扫描策略（核心算法）

### 6.1 为什么不能"全扫"

情报专栏共 **18823 条**内容，日更约 5 条。全扫需要翻约 3000 屏，耗时数小时且毫无意义——**超过 14 天的帖子必然已开奖**。

### 6.2 为什么不能"看到旧的立刻停"

实测存在**最大 12.3 小时的倒挂**。若看到第 3 条（11-16 20:10，早于第 2 条 11-17 08:30）就判定"已到旧内容"并停止，会**漏掉第 4 条（11-17 08:30）这个更新的帖子**。

### 6.3 停止条件（最终设计）

```
停止条件 = 连续 N 条都满足 created_at < 边界时间

其中：
  边界时间 = 今天 - (SCAN_WINDOW_DAYS + SCAN_BUFFER_DAYS) 天
           = 今天 - (14 + 2) 天
           = 今天 - 16 天

  N = SCAN_CONFIRM_COUNT = 10
```

**参数依据**：

| 参数 | 值 | 依据 |
|---|---|---|
| 窗口天数 | 14 | 实测最长抽奖周期 14 天 |
| 安全边距 | 2 天 | 实测最大倒挂 12.3h，2 天 = 4 倍裕量 |
| 连续确认条数 | 10 | 实测倒挂最多影响 2 条，10 = 5 倍裕量 |

### 6.4 增量扫描（首次之后）

维护 `coverage_watermark`（覆盖率水位）= 上一轮成功扫描到的最早 `created_at`。

```
本轮边界 = min(
    今天 - (WINDOW + BUFFER) 天,        // 兜底：永远不扫 16 天前
    coverage_watermark - BUFFER 天       // 增量：从上次水位回退 2 天
)
```

**效果**：

| 场景 | 扫描条数 | 滚动次数 | 预计耗时 |
|---|---|---|---|
| **首次运行**（建库） | ~80 条（16 天） | 5–6 次 | ~60 秒 |
| **日常运行**（增量） | ~15–20 条 | 1 次 | ~10 秒 |

### 6.5 成本预算

| 项目 | 首次 | 日常 |
|---|---|---|
| 列表滚动 | 6 次 | 1 次 |
| 详情请求 | ~40 次（80 条 × 50%） | ~5 次 |
| 关注请求 | 0 | 0–2 次 |
| 回复请求 | 0 | 0–2 次 |
| 点赞请求 | 0 | 0–2 次 |
| **总计** | **~46 次** | **~12 次** |

> 首次运行因要建立全量抽奖判定缓存，请求较多。之后每天仅个位数。

---

## 7. 数据模型

### 7.1 表 `flows` —— 见过的所有帖子

| 字段 | 类型 | 说明 |
|---|---|---|
| `hash` | TEXT PRIMARY KEY | 帖子 hash（如 `ZwPnZ6lzj5xy`） |
| `flow_id` | INTEGER | 站内数字 id（如 `258582`） |
| `title` | TEXT | 标题 |
| `created_at` | TEXT | 发帖时间（ISO 格式） |
| `author_uid` | INTEGER | 作者数字 id |
| `author_hash` | TEXT | 作者 hash |
| `author_nick` | TEXT | 作者昵称 |
| `is_lottery` | INTEGER | `NULL`=未判定，`0`=非抽奖，`1`=抽奖 |
| `lottery_status` | INTEGER | 抽奖状态（`1`=待抽奖） |
| `lottery_at` | TEXT | 开奖时间 |
| `prizes` | TEXT | 奖品摘要（JSON） |
| `needs_follow` | INTEGER | 规则是否要求关注 |
| `first_seen_at` | TEXT | 首次发现时间 |
| `last_checked_at` | TEXT | 最近检查时间 |

**索引**：`created_at`、`is_lottery`、`lottery_status`

### 7.2 表 `participations` —— 参与记录

| 字段 | 类型 | 说明 |
|---|---|---|
| `hash` | TEXT PRIMARY KEY | 帖子 hash（外键 → flows） |
| `followed_at` | TEXT | 关注成功时间 |
| `replied_at` | TEXT | 回复成功时间 |
| `reply_id` | INTEGER | 回复 id（站内 id） |
| `reply_floor` | INTEGER | 楼层号 |
| `liked_at` | TEXT | 点赞成功时间 |
| `state` | TEXT | `pending`/`done`/`failed`/`give_up` |
| `retry_cnt` | INTEGER | 已重试次数 |
| `fail_reason` | TEXT | 失败原因 |
| `updated_at` | TEXT | 最后更新时间 |

**状态机**：

```
        ┌─────────┐
        │ pending │  ← 发现抽奖帖
        └────┬────┘
             │ 开始执行
             ▼
   ┌──────────────────┐
   │ 关注 → 回复 → 点赞 │
   └────────┬─────────┘
            │
     ┌──────┴───────┐
     ▼              ▼
  ┌──────┐      ┌────────┐
  │ done │      │ failed │ ──retry<3──┐
  └──────┘      └────────┘            │
                     │                │
                     └────────────────┘
                     │ retry>=3
                     ▼
                 ┌─────────┐
                 │ give_up │
                 └─────────┘
```

### 7.3 表 `run_logs` —— 每轮运行记录

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | — |
| `started_at` | TEXT | 开始时间 |
| `finished_at` | TEXT | 结束时间 |
| `scanned_cnt` | INTEGER | 扫描条数 |
| `detail_cnt` | INTEGER | 详情请求数 |
| `participated_cnt` | INTEGER | 成功参与数 |
| `failed_cnt` | INTEGER | 失败数 |
| `result` | TEXT | `success`/`aborted`/`captcha`/`login_invalid` |
| `note` | TEXT | 备注 |

### 7.4 表 `kv` —— 键值存储

| 字段 | 类型 | 说明 |
|---|---|---|
| `key` | TEXT PRIMARY KEY | 如 `coverage_watermark` |
| `value` | TEXT | 值 |
| `updated_at` | TEXT | 更新时间 |

---

## 8. 主流程时序

```
每天 09:00（或手动触发）
│
├─ [0] 前置检查
│   ├─ Selenium 可达？
│   ├─ Cookie 有效？（访问首页，检查是否已登录）
│   ├─ identity_pass == 1？（POST /v2/publish/rules）
│   └─ 任一失败 → 记 run_logs(result=aborted) → 结束
│
├─ [1] 扫描列表
│   ├─ 打开 /app/tag/2007
│   ├─ 循环：读 Vue 数据 → 判断停止条件 → 滚动
│   ├─ 停止：连续 10 条 < 边界时间
│   └─ 得到 items[]（hash, created_at, author, ...）
│
├─ [2] 写入 flows 表（新帖 INSERT，老帖跳过）
│
├─ [3] 筛选待查帖
│   └─ SELECT * FROM flows
│       WHERE is_lottery IS NULL          -- 未判定
│          OR (lottery_status = 1 AND last_checked_at < 今天)  -- 待复查
│
├─ [4] 逐帖判定（含请求间隔 2-5s）
│   ├─ POST /v2/flow/detail {id: hash}
│   ├─ 无 lottery → UPDATE is_lottery=0
│   ├─ 有 lottery → UPDATE is_lottery=1, lottery_status, lottery_at
│   └─ 遇 20001 / 验证码 → 暂停并告警
│
├─ [5] 筛选待参与帖
│   └─ SELECT * FROM flows f
│       LEFT JOIN participations p ON f.hash = p.hash
│       WHERE f.is_lottery = 1
│         AND f.lottery_status = 1
│         AND (p.state IS NULL OR p.state = 'failed')
│
├─ [6] 逐帖参与（含请求间隔 2-5s）
│   ├─ 关注：/v2/user/detail → followed? → /api/follow → 回读确认
│   ├─ 回复：/v2/flow/reply → 得到 reply_id
│   ├─ 回读：/api/circle/flowReplyList → 确认本人在列表中
│   ├─ 点赞：/api/circle/zanFlow → 回读 has_zan == 1
│   └─ 落库 state='done'
│
├─ [7] 更新 coverage_watermark
│
└─ [8] 写 run_logs，关闭浏览器会话
```

---

## 9. 风控与合规策略

### 9.1 频率控制

| 措施 | 值 |
|---|---|
| 请求间隔 | 2–5 秒随机 |
| 单轮写操作上限 | 30 次 |
| 运行频率 | 每天 1 次 |
| 并发 | 严格串行，无并发 |

**风险等级评估**：日常约 12 次请求/天，远低于触发阈值。实测连发 10+ 次详情请求未触发限流。

### 9.2 验证码策略（底线）

**绝不程序化破解。** 具体表现：

- ❌ 不写滑块轨迹模拟
- ❌ 不写拼图缺口图像识别
- ❌ 不引入任何打码平台 SDK
- ✅ 检测到即暂停，等人工在 noVNC 完成

### 9.3 行为拟人化

- 回复语料随机，不同帖不同内容
- 请求间随机延迟
- 关注前先访问作者主页（符合真人路径）
- 页面停留时间随机化

### 9.4 已知风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| 站点 ToS 灰区 | 中 | 单账号、低频、正常回复内容 |
| 账号被限流 | 中 | 严格频控 + 退避 |
| 账号被封禁 | 低 | 不批量、不刷屏 |
| 站点改版导致失效 | 高 | 选择器与接口集中管理，便于快速修复 |
| 关注列表被抽奖作者占满 | 中 | 用户已明确接受 |

---

## 10. 部署方案

### 10.1 目录结构

```
/root/zf_lucky/
├── REQUIREMENTS.md          # 本文档
├── docker-compose.yml
├── Dockerfile
├── .env                     # 配置（含 Cookie，不入库）
├── .env.example             # 配置模板
├── .gitignore
├── README.md
├── app/
│   ├── main.py              # 入口 + 调度
│   ├── config.py            # 配置加载
│   ├── db.py                # SQLite 操作
│   ├── models.py            # 数据模型
│   ├── zf/
│   │   ├── driver.py        # WebDriver 封装
│   │   ├── session.py       # 登录 + 会话管理
│   │   ├── scanner.py       # 列表扫描
│   │   ├── detail.py        # 详情判定
│   │   ├── participant.py   # 关注/回复/点赞
│   │   ├── captcha.py       # 验证码检测（只检测，不破解）
│   │   └── api.py           # 同源 fetch 封装
│   ├── corpus.py            # 回复语料
│   └── web/
│       ├── app.py           # FastAPI/Flask 面板
│       └── templates/
├── data/                    # SQLite 持久化
└── logs/                    # 日志
```

### 10.2 Docker Compose

```yaml
services:
  zf-lucky:
    build: .
    container_name: zf-lucky
    restart: unless-stopped
    env_file: .env
    environment:
      TZ: Asia/Shanghai
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    ports:
      # 仅回环，经 1Panel 反代暴露
      - "127.0.0.1:9997:8080"
    networks:
      - bili-net
    depends_on:
      - bili-selenium

networks:
  bili-net:
    external: true
```

### 10.3 反向代理

参照现有 `bili.vernon.de5.net` 模式（1Panel OpenResty），新增站点：

```
域名：zf.vernon.de5.net（待定）
后端：http://127.0.0.1:9997
```

配置模板（与 `panel-bili` 一致）：
```nginx
location ^~ / {
    proxy_pass http://127.0.0.1:9997;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 120s;
}
```

### 10.4 端口占用

| 端口 | 用途 | 状态 |
|---|---|---|
| 9997 | zf-lucky 面板 | **空闲，可用** |
| 9998 | bili-panel（已占用） | — |
| 9999 | bili-logs（已占用） | — |
| 7900 | noVNC（仅回环） | 已有 |

### 10.5 安全要求

- ❌ **绝不**暴露 Selenium 的 `4444` 端口
- ❌ **绝不**暴露 noVNC 的 `5900` 端口
- ✅ 面板仅绑 `127.0.0.1`，经反代访问
- ✅ `.env` 加入 `.gitignore`，不提交 Cookie
- ✅ Cookie 不写入日志（打印时脱敏）

---

## 11. 验收标准

### 11.1 功能验收

| 编号 | 标准 | 验证方式 |
|---|---|---|
| AC-1 | 能正确识别情报专栏中所有抽奖帖 | 与人工核对 10 条样本，全部正确 |
| AC-2 | 能正确识别已开奖帖并跳过 | 构造 `status != 1` 的样本验证 |
| AC-3 | 对未参与帖完成关注 + 回复 + 点赞 | 实测 1 条真实帖子 |
| AC-4 | 回复后能在服务端列表查到本人回复 | `flowReplyList` 回读 |
| AC-5 | 点赞后 `has_zan == 1` | 详情回读 |
| AC-6 | 重复运行不产生重复回复 | 连跑两次，第二次全部跳过 |
| AC-7 | 失败帖下轮自动重试 | 模拟失败验证 |
| AC-8 | 验证码出现时暂停并告警 | 手动触发 `triggerRateLimit` 验证 |
| AC-9 | 程序重启后状态不丢失 | 重启容器验证 |
| AC-10 | 扫描能按时间边界正确停止 | 对比扫描条数与预期 |

### 11.2 性能验收

| 编号 | 标准 |
|---|---|
| AC-11 | 日常单轮运行 < 3 分钟 |
| AC-12 | 首次建库 < 10 分钟 |
| AC-13 | 日常请求数 < 20 次 |

### 11.3 安全验收

| 编号 | 标准 |
|---|---|
| AC-14 | 代码中无任何验证码破解逻辑 |
| AC-15 | 日志中无明文 Cookie |
| AC-16 | Selenium 4444 端口未对外暴露 |

---

## 12. 待确认事项

| 编号 | 事项 | 影响 | 状态 |
|---|---|---|---|
| Q-1 | 面板域名（`zf.vernon.de5.net`？） | 部署 | 待定 |
| Q-2 | 回复语料最终清单 | 内容 | 已有默认值 |
| Q-3 | 运行时间（默认 `09:00`） | 调度 | 待确认 |
| Q-4 | 站点回复内容长度限制 | 实现 | 待实测 |
| Q-5 | 回复接口是否需要 `item_type` 参数 | 实现 | 待实测 |
| Q-6 | 已给出「强制重跑」按钮的必要性 | 面板 | 待确认 |

---

## 13. 实施计划（待批准后启动）

| 阶段 | 内容 | 预估 |
|---|---|---|
| P0-1 | 项目骨架 + 配置 + 数据库 | 0.5 天 |
| P0-2 | WebDriver 封装 + 会话 + 同源 fetch | 0.5 天 |
| P0-3 | 列表扫描 + 停止条件 | 0.5 天 |
| P0-4 | 详情判定 + 缓存 | 0.5 天 |
| P0-5 | 关注 + 回复 + 点赞 + 回读校验 | 1 天 |
| P0-6 | 调度 + 日志 + 错误处理 | 0.5 天 |
| P0-7 | Docker 化 + 部署 + 联调 | 0.5 天 |
| P1-1 | Web 面板 | 1 天 |

**总计约 5 天**（含面板）。

---

## 附录 A：调研过程记录

### A.1 已完成的验证

| 验证项 | 方法 | 结果 |
|---|---|---|
| Selenium 可驱动本站 | 创建 session 打开页面 | ✅ 标题正常返回 |
| 页面内 fetch 详情接口 | `execute/async` 调用 | ✅ `status=1` 正确 |
| 列表 API 可用性 | curl + 浏览器内 fetch | ❌ 均返回 `20001` |
| 列表 Vue 数据可取 | 遍历 `__vue__` | ✅ 26 字段全部可读 |
| 列表无抽奖标记 | 字段枚举 + DOM 查询 | ❌ 确认无 |
| 抽奖比例 | 逐条查 10 条详情 | ✅ 5/10 |
| 排序规律 | 滚动 60 条分析 | ✅ 近似倒序，最大倒挂 12.3h |
| 发帖速率 | 12 天数据统计 | ✅ 5.0 条/天 |
| 验证码类型 | 强制弹出 + 截图 | ✅ 滑块拼图 |
| 关注状态判据 | `/v2/user/detail` | ✅ `user.followed` |
| 回复列表接口 | `/api/circle/flowReplyList` | ✅ 匿名可读 |

### A.2 待登录后实测

| 项目 | 原因 |
|---|---|
| 回复接口真实调用 | 需登录态 |
| 关注接口真实调用 | 需登录态 |
| 点赞接口真实调用 | 需登录态 |
| `identity_pass` 实际值 | 需登录态 |
| `publish/rules` 返回结构 | 需登录态 |
| 登录后 Cookie 具体名称 | 需登录态 |

### A.3 调研中的错误更正

| 错误结论 | 实际 | 更正方式 |
|---|---|---|
| 「情报 10 条只有 1 条抽奖」 | 5 条 | 逐条查询详情后纠正 |
| 「列表可按 `is_lottery` 粗筛」 | 列表无此字段 | Vue 数据字段枚举后纠正 |
| 「Selenium 不会触发风控」 | 风控按接口+账号维度，与 UA 无关 | 浏览器内 fetch 仍被拦截后纠正 |

---

## 附录 B：关键代码位置参考

> 以下为**参考实现**，非本项目代码，来自用户既有项目 `BiliBili-Lucky-Draw`，其中的实战经验可直接借鉴。

| 经验 | 位置 | 借鉴点 |
|---|---|---|
| 稳健点击（防遮挡） | `share_one_dynamic.py::safe_click_element` | `elementFromPoint` 判断遮挡，JS 兜底标记低可信 |
| 点击后回读校验 | `share_one_dynamic.py::click_follow` | 点完必须读回状态，不能想当然 |
| 异步渲染陷阱 | `share_one_dynamic.py::ensure_follow_state_loaded` | 首帧状态不可信，需 refresh + 轮询 |
| 失败不退出进程 | `main.py::do_search/do_share` | 每个任务包 try/except，防重启循环 |
| 等待依赖就绪 | `scripts/entrypoint.py` | 启动前探测 Selenium/DB |
| 内容生成 | `utils/comment_gen.py` | 三级降级提取，避免固定语料 |

---

*文档结束。本文档为需求确认稿，经用户批准后方可进入实现阶段。*
