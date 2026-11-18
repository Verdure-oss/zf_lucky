# zf_lucky

zFrontier（装备前线）「情报」专栏抽签福利帖的自动参与系统。

每天运行一次：扫描情报专栏 → 识别抽奖帖 → 对未参与的帖子完成
「关注作者 → 回复 → 点赞」→ 结果写入本地数据库并展示在面板上。

> **状态**：需求已确认，实施中。需求文档见 [REQUIREMENTS.md](REQUIREMENTS.md)。

---

## 它解决什么问题

情报专栏日更约 5 条，其中约一半是抽奖帖。人工每天刷一遍、逐帖判断并回复，
重复且容易漏。本项目把这套动作自动化，并保证**不重复、不遗漏**。

## 工作原理（简述）

```
打开情报专栏 → 从 Vue 数据读列表（不调列表 API）
     ↓ 按时间边界 + 安全边距停止滚动
用本地库过滤 → 只对「未判定」的帖子查详情
     ↓ POST /v2/flow/detail
是有效抽奖帖（lottery.status == 1）且本地未完成？
     ↓ 是
关注作者 → 回复 → 回读确认 → 点赞 → 回读确认 → 落库
```

**三个关键设计决定**，都有实测依据：

1. **列表无抽奖标记，必须逐帖查详情。**
   情报 tag 页的列表数据和 DOM 里都没有 `is_lottery`，无法从列表判断。

2. **扫描按时间停，不按条数停，但要留边距。**
   实测列表近似按时间倒序，但存在最大 12.3 小时的局部倒挂，
   所以「看到一条旧的立刻停」会漏帖。用「连续 10 条早于边界」才停。

3. **先回复、后点赞。**
   这样 `has_zan == 1` 在语义上蕴含「回复也成功了」，
   不会出现「赞了但没回复」的永久漏帖。

## 目录结构

```
zf_lucky/
├── REQUIREMENTS.md      # 需求文档（先读这个）
├── docker-compose.yml
├── Dockerfile
├── .env.example         # 配置模板，复制为 .env 后填写
├── app/
│   ├── main.py          # 入口 + 调度
│   ├── config.py        # 配置加载
│   ├── db.py            # SQLite
│   ├── zf/              # 站点交互
│   └── web/             # 面板
├── data/                # SQLite 持久化（不入库）
└── logs/                # 日志（不入库）
```

## 快速开始

```bash
cp .env.example .env
# 编辑 .env，至少填入 ZF_COOKIE（从浏览器开发者工具复制整段 Cookie）

docker compose up -d --build
docker compose logs -f
```

面板：`http://127.0.0.1:9997`（只绑回环，需经反向代理访问）

### 反向代理（1Panel OpenResty）

新建站点指向 `http://127.0.0.1:9997`：

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

### 面板能做什么

| 功能 | 说明 |
|---|---|
| 状态总览 | 中奖 / 未中奖 / 待开奖 / 已完成 / 待执行 / 失败 |
| 历史战绩 | 累计参与、中奖率、按天趋势图、中奖明细（可折叠） |
| 结果分区 | 按 待开奖 / 中奖 / 未中奖 / 失败 筛选，支持关键字搜索 |
| 帖子列表 | 标题、作者、开奖时间、奖品、状态、关注/回复/点赞时间 |
| 观看浏览器 | 常驻 noVNC 链接，点开即可实时看自动化操作 |
| 实时日志 | 独立日志页，支持级别过滤、关键字搜索、自动刷新 |
| 登录并获取 Cookie | 在浏览器里人工登录，程序自动导出写入 .env |
| 立即运行 | 写触发文件，由主循环串行执行 |
| 暂停 / 恢复 | 暂停期间定时与手动触发都不会执行 |
| 单帖重跑 | 清空该帖进度重新处理 |
| 异常告警 | 验证码待处理、登录失效、限流、异常中止 |

面板本身**不对站点发起任何操作**，唯一写动作是创建触发文件。

### 关于开奖结果

每轮运行末尾会自动检测已参与帖子的开奖情况：

- `lottery.status = 2` 表示已开奖
- `prizesGroup[].list[].rewardUser.id` 是中奖者的数字 uid
- 用 **uid 而非昵称**比对（昵称可改、可重名）
- 中奖率只对已开奖的记录计算，待开奖不计入分母

### 定时安排

本程序默认 **08:00** 运行。若与同一 Selenium 容器上的其他任务共用，
建议错开时间，例如：

| 时间 | 任务 |
|---|---|
| 08:00 | zf_lucky |
| 09:30 | 其他任务 |
| 15:42 / 15:45 | 其他任务 |

多个会话会共享同一个 X display，同时运行会导致 noVNC 里窗口重叠。
本程序会在窗口顶部注入一条 `ZF · zf_lucky` 标识条便于辨认。


## 前置条件

- 已绑手机号的 zFrontier 账号（站点要求绑定后才能回复，未绑会拒绝）
- 宿主机有可用的 Selenium 容器，且与本项目在同一 docker 网络
- Selenium 的 `4444` 端口**不要**对外暴露
- 出现滑块验证码时，通过 noVNC（`127.0.0.1:7900`）手动完成

## 运维

```bash
docker compose logs -f            # 看日志
docker compose restart            # 改了 .env 后重启生效
curl -X POST localhost:9997/api/run       # 手动触发一轮
curl -X POST localhost:9997/api/pause \
     -H 'Content-Type: application/json' \
     -d '{"enable":true}'          # 暂停
```

数据在 `./data/zf_lucky.db`（SQLite，WAL 模式），备份该目录即可。

## 自检

每个功能都有可执行自检，改完代码应全绿：

```bash
# 离线（无需 selenium，宿主机即可跑）
python3 scripts/selftest_p01.py     # 配置 + 数据库 + 跨线程
python3 scripts/selftest_p05.py     # 参与器（全用假 driver/API）
python3 scripts/selftest_p06.py     # 调度 + 编排 + 锁
python3 scripts/selftest_corpus.py  # 评论生成与相关性
python3 scripts/selftest_draw.py    # 开奖结果判定
python3 scripts/selftest_seen.py    # 扫描停止条件
python3 scripts/selftest_p03.py     # 扫描器（部分需 selenium）
python3 scripts/selftest_p04.py     # 抽奖判定（部分需 selenium）
python3 scripts/selftest_p1.py      # 面板（需 flask）
python3 scripts/selftest_p2.py      # VNC/登录/.env 写入（需 flask）

# 需在 bili-net 网络内（连真实 Selenium 与站点）
docker exec zf-lucky python scripts/selftest_p02.py
```

### 演示脚本（人工目视检查）

在真实浏览器里完整走一遍 关注 → 回复 → 点赞，用于确认动作是否正确：

```bash
# 建议用非抽奖帖，避免污染抽奖记录
docker exec zf-lucky python /app/scripts/demo_run.py <flow_hash> --slow 3

# 可选开关
--no-follow / --no-reply / --no-like   只做部分步骤
--slow N                              停顿放大 N 倍，便于观察
```

运行前先在面板点「观看浏览器」打开 noVNC，即可实时看到操作过程。



## 底线

- **不破解验证码。** 出现滑块即暂停，等人工在 noVNC 处理。
- **单账号。** 不做多账号。
- **低频。** 每天一次，请求间隔 2–5 秒随机。

## 免责声明

本项目仅用于个人账号的自动化操作。站点服务条款未明确允许此类行为，
使用可能导致限流或账号受限。请自行评估风险，保持低频、正常内容。
