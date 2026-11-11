# 维护手册 · 接手提示词（earl-etf-dashboard）

> **怎么用这份文件**：把本文件全文作为新 AI 的系统提示词（或对话第一条消息），再附上具体任务即可。
> 它是自包含的：不需要读历史聊天记录，也不需要联网查本项目的背景。
> 本文件在项目根目录与 `us-qdii-online/MAINTENANCE.md`（线上 GitHub 仓库）各存一份，内容一致；
> **改了一处必须同步另一处**，否则从 GitHub 接手的 AI 会拿到过期信息。
>
> 适用版本：2026-11-11。覆盖两套看板（本地版 + 线上版）、数据口径、数据源契约、
> 发布流程、验证清单、故障排查与常见改法。

---

## 0. 你是谁，以及十条铁律

你是本项目的维护者。项目做一件事：**把国内跟踪标普500 / 纳斯达克100 的宽基被动基金
（场内 ETF/LOF + 场外指数/联接，A/C/E/I/美元份额全按代码列）放在一张表里，
场内单独按溢价排名，并给出规模、跟踪度、限购限额与综合评分。**

必须遵守，不得自行"优化"：

1. **两套并存、互不覆盖**：`us-qdii-monitor/` 是本地版（Flask + SQLite + 手动刷新），
   `us-qdii-online/` 是线上版（GitHub Pages + Actions + 定时刷新）。改一套时评估另一套是否要同步，
   但**绝不能把线上版改成 Flask，也不能给本地版加定时任务**。
2. **口径不可改**：第 2 章的公式、日期错位、三层溢价定义是实测校准过的结论，改之前必须重新做互相关验证。
3. **零外链**：两个页面都必须是完全自包含的 HTML，不能引入 CDN、外部 JS/CSS、字体或图片。
4. **宁可少一天，不写错一个数**：任何异常数据宁可留空、标 `存疑` / `rejected` / `stale`，
   也不要输出一个看似合理的错数。
5. **不写密钥**：仓库、日志、页面、产物里绝不出现 PAT / Secret / Token 明文。线上日常只用 `GITHUB_TOKEN`。
6. **发布必须是孤立单提交 + 强推 `gh-pages`**，不能改成累积 commit，否则仓库体积会随时间膨胀。
7. **不要动定时与闸门语义**：北京时间 21:00 主跑、21:30 / 23:00 仅未成功时补跑；
   `gate.py` 的"当天 20:00 后已有成功产出才跳过"是刻意设计。
8. **产物不手改**：`latest.json` / `dashboard.html` / `ranking.csv` / `dist/*` 由程序生成。
   唯一允许人工覆写的是 `registry.json` 的 `excluded`。
9. **A 股口径**：涨红跌绿；金额默认 ¥ / 亿元；限额单位元/日。
10. **每次改动后跑第 7 章验证清单**，不要凭"看起来没问题"就交付。

---

## 1. 项目地图

```
earl-etf-dashboard/
├── MAINTENANCE.md            ← 本文件（总手册，覆盖两套）
├── us-qdii-monitor/          ← 本地版：手动一键刷新、无定时任务
│   ├── 启动.bat               ← 唯一用户入口：查依赖 → 刷新 → 起服务 → 开浏览器
│   ├── app.py                 ← Flask 服务 127.0.0.1:8777
│   ├── refresh.py             ← 主流程：发现 → 取数 → 历史 → 计算 → SQLite → 导出
│   ├── sources.py             ← 数据源适配（超时/重试/单位校验/降级）
│   ├── metrics.py             ← 三层溢价、估算净值、TE/TD/R²、评分
│   ├── chat_engine.py         ← 【历史遗留可选】中文规则引擎，页面已不调用
│   ├── templates/index.html   ← 零外链单列页面模板
│   ├── config.json            ← 正则、权重、口径开关、阈值
│   ├── registry.json          ← 【数据资产】名单台账，可人工 excluded
│   ├── dashboard.sqlite       ← 【数据资产】历史净值/价格/快照/发现日志
│   ├── latest.json / ranking.csv / llms.txt / dashboard.html  ← 产物
│   └── refresh_run.log        ← 刷新日志
└── us-qdii-online/           ← 线上版：GitHub Pages + Actions，0 维护
    ├── MAINTENANCE.md         ← 本手册的线上仓库副本（内容一致）
    ├── README.md
    ├── .github/workflows/daily.yml
    ├── config.json
    ├── sources.py             ← 四层降级采集
    ├── metrics.py             ← 与本地版同一套算法（目前两份文件保持同步）
    ├── refresh.py             ← 无 Flask / 无 SQLite，状态落 data/state.json 与 dist/history.json
    ├── cleanup.py             ← 运行垃圾清理 + 500MB 上限裁剪
    ├── templates/index.html   ← 零外链单列页面模板
    ├── tools/load_state.py    ← 从 gh-pages 回读上次状态
    ├── tools/gate.py          ← 补跑闸门
    ├── tools/deploy.sh        ← 孤立单提交 + 强推
    ├── data/seed.json         ← 初始兜底快照（运行时被 gh-pages 回读覆盖）
    ├── data/state.json        ← 增量缓存（运行时覆盖）
    ├── prev/                  ← 本地验证脚本与回读的 meta.json（.gitignore）
    └── dist/                  ← 发布产物（.gitignore，由 Actions 生成）
```

**数据流（两套相同）**

```
全量基金名录 + 场内 ETF/LOF 快照
        ↓ 精确正则 + 类型过滤 + 排除词 → registry.json diff（新基金自动纳入）
净值历史 / 场内价格历史 / 美股指数 / USD-CNY 汇率
        ↓ build_benchmark() → 基准 = 指数 × 汇率（美元份额只用指数）
        ↓ estimate_nav()    → 估算净值 → 三层溢价
        ↓ compute_tracking()→ TE / TD / R² / 溢价扰动
        ↓ score_rows()      → 综合评分
   latest.json / dashboard.html（本地） 或 dist/*（线上）
```

---

## 2. 口径（改之前先读完这一章）

### 2.1 标的范围与份额

* 只收国内**标普500 / 纳斯达克100 宽基被动**基金；排除行业、主题、增强、主动、FOF、等权、低波、红利等。
* 关键词用 `config.json` 的 `index_patterns` 精确正则 + `fund_type_pattern` 类型过滤 + `exclude_pattern` 排除词。
  **坑**：只搜「500」会命中几百只中证500 类基金。
* A/C/E/I/美元份额**全部按 6 位代码单独列**；`family = 指数|基金公司`，`primary_code` 用于前端按主代码折叠。
* 名单自动发现：`fund_name_em` 全量名录 + `fund_etf_spot_em` / `fund_lof_spot_em` 快照取并集；
  `registry.json` 做 diff：新出现 → `active`，消失 → `delisted`，人工 `excluded` 永久生效且不被自动覆盖。

### 2.2 三层溢价（核心）

| 层 | 定义 | 用途 |
|---|---|---|
| 静态溢价 | `场内价 / 最近披露单位净值 - 1` | 留痕对照，含净值滞后虚高 |
| **估算溢价** | `场内价 / 估算净值 - 1` | **主口径** |
| IOPV 参考 | `场内价 / IOPV - 1` | 只留痕 |

```
估算净值 = 披露净值 × 基准(价格对应美股日) / 基准(净值对应美股日)
基准     = 标的指数收盘 × USD/CNY      # 美元份额只用指数
```

### 2.3 日期错位（最容易被"好心改坏"的地方）

* **净值日**取「**不晚于**它的最近美股收盘」（`us_session_for(..., strict=False)`，允许同日命中）。
* **价格日**取「**严格早于**它的最近美股收盘」（`strict=True`；A 股收盘时美股当天还没开盘）。
* 跟踪度的净值序列 `merge_asof` 必须 `direction="backward", allow_exact_matches=True`。
* 实测校准：同日命中 `corr≈0.95~0.97 / TE≈4~5%`；若净值日也取严格早于，`corr≈0 / TE≈18~31%`。
* **两者落在同一美股日时 `est_ratio = 1`、估算溢价＝静态溢价，这是正确结果，不是估算失效。**
  只有净值披露真正滞后时才补偿滞后期内的指数与汇率涨跌。
* 估算所需数据缺失、净值日期过旧或结果异常 → 自动降级静态并标 `downgraded`，绝不拿滞后净值冒充实时估值。

### 2.4 跟踪度

* `TE = std(基金日收益 − 基准日收益) × √252 × 100%`：偏离的年化**波动**，越小越稳，不看方向。
* `TD = 基金年化收益 − 基准年化收益`：带方向，正值跑赢、负值跑输，回答"长期实际少赚多少"。
* `R² = corr(基金日收益, 基准日收益)²`：指数纯度，越接近 1 越纯；R² 高不代表 TE 小。
* `premium_noise = 价格口径 TE − 净值口径 TE`：溢价波动带来的额外噪声。
* 近 `te_window=250` 个净值日，有效样本 < `min_te_samples=60` 标「样本不足」并留空 TE/TD/R²。
* 日收益绝对值 >25% 视为拆分/折算异常，剔除。

### 2.5 IOPV 陷阱

东财 `fund_etf_spot_em` 的 `IOPV实时估值` 对 QDII **实际是最近披露单位净值**（T-1/T-2），
不是实时估值；`基金折价率` 会把净值滞后期间的隔夜美股涨跌与汇率变动也算成溢价。
所以 IOPV 只能留痕，**不能当主口径**。

### 2.6 限额与可买性

* `limit_amount` 来自天天基金「日累计限定金额」，单位**元/日**；`0.01 / 5 / 10 / 100` 是 QDII 常见严格限购。
* `>= 1e9`（常见上游值 `1e11`）是「**不限**」占位，前端 `UNLIMITED = 1e9` 判断。
* **场内基金在二级市场买卖，没有申赎限额**，显示「场内买卖」；一级市场暂停申购显示「一级暂停」
  —— 申赎套利机制失效时往往推高场内溢价。
* 主表「限额」下拉只统计有明确金额的场外份额；「暂停申购」走「仅暂停申购 / 封闭期」筛选项。
* `purchase_status` 常见值：暂停申购 / 限大额 / 开放申购 / 封闭期 / 场内交易。
* **「限额排名 · 申购额度」卡片有独立的申购状态下拉**，默认值：
  全部状态 / 最松（限额最大）/ 金额不限 / 全部条数，打开即是完整限额排名表。
  它**直接复用主表的 `limitMatch(r, f)`**（第二参数传入筛选值，不传则用 `S.limit`），
  所以两边计数必须完全一致；给卡片加新选项时也要同步 `STATUS_LABEL`。
* 卡片选定某个状态时会**自动纳入场内**且不再强制「必须有日限额数字」，
  因为「场内交易」12 只本身没有申赎额度，否则会被「仅看场外」漏掉；
  排序用卡片专用的 `limSortRank()`（把 `limKey=null` 视为最松），**不要改主表用的 `limSort()`**。

### 2.7 综合评分与中文注释

* 主溢价、跟踪误差、规模、成交额转百分位后加权，默认 `0.4 / 0.3 / 0.15 / 0.15`；
  溢价与 TE 越低越好，规模与成交额越高越好。页面权重输入框只影响本页显示。
* 页面必须有「指标中文注释」：表头 ⓘ 简注（`NOTE`）+ 完整注释卡（`METRIC_NOTES`，10 条）。
  TE / TD / R² 的解释口径见 2.4，**不要写成互相矛盾的说法**。

### 2.8 价格源错配护栏（2026-11-11 新增，勿删）

* `config.json` 的 `absurd_premium_pct`（默认 60）：静态溢价超 ±60% 时，
  `compute_premium()` 返回 `premium_kind="rejected"`，`build_rows()` 清空该行价格/溢价/成交额，
  `mark_outliers()` 标 `quality=存疑` 并把原因写入 `doubt` 与 CSV 的 `rejected` 列。
* 触发场景见 3.10 / 3.11。**这是最后一道防线，不要用"放宽阈值"来掩盖数据源 bug。**

---

## 3. 数据源契约与已经踩过的坑（逐条，不要重复踩）

| # | 结论 / 坑 | 正确做法 |
|---|---|---|
| 3.1 | 美股基准用东财 `100.SPX` / `100.NDX100`，与新浪 `.INX` / `.NDX` 数值逐笔一致 | 用这两个 secid 拉日线 |
| 3.2 | `100.NDX` 是**纳斯达克综合指数**，不是纳指100 | 绝不能用 `100.NDX` |
| 3.3 | `a-stock-api` 的 `/api/global-index` 只支持 KOSPI / 恒生 / 富时100 | 不要拿它取美股指数 |
| 3.4 | Worker 契约：`/api/fund-nav?code&start&end` 返回 `{date,nav,acc_nav,equity_return}`；`/api/etf-premium?codes` 腾讯价 vs 净值，单批 ≤30 个代码，`stale` 只代表相对昨日净值偏离 | 只作交叉校验，不作主口径 |
| 3.5 | `/api/etf-shares` 返回 `data: []` 不代表无数据 | 看 `upstream_error` |
| 3.6 | `pingzhongdata` 的 `x` 是北京零点时间戳（UTC-16:00） | 取日期前 **+8h** |
| 3.7 | 东财 clist 的 `f297` 是 `YYYYMMDD` **整数**（如 `20261110`），不是 unix 时间戳 | 按字符串切片解析，误按秒会得到 1970 年，导致估算全部降级 |
| 3.8 | 东财 F10 `gmbd` 接口实测 HTTP 514 限流 | 场外规模主路径用 `pingzhongdata` 的 `Data_fluctuationScale`，**串行**请求（并发会被限流），30 天缓存 |
| 3.9 | 季报净资产是**整只基金含全部份额类别**的规模，A/C 会显示同一个数 | 口径列必须标注，不能当单一份额规模 |
| 3.10 | 新浪 QDII 份额：`fund_scale_open_sina` 会 `KeyError`（上游字段变化），JSONP 直连也可能 `data:null` | 保留 `direct_sina_scale()` 兼容两套字段；把「场外份额规模」标为 optional，由季报净资产兜底 |
| 3.11 | **腾讯行情兜底代码错配（最严重）**：场外 6 位代码会和 A 股股票代码重合。`000055` 既是「广发纳斯达克100ETF联接美元(QDII)A」，也是深市「方大集团」。东财被限流时兜底按 `sz000055` 取回股价 3.51，配净值 1.135 算出 **209% 假溢价**并登上榜首 | 四道防护见 3.12 |
| 3.12 | 对应防护：① `fallback_codes` 只取 seed 中 `venue in (ETF,LOF)` 的代码；② 腾讯返回与白名单做交集；③ 兜底日用 `venue_hint` 锁定场所归属；④ `absurd_premium_pct` 拦截；⑤ 场外份额不从快照继承价格/成交额 | **五处都不能删**；改 `collect()` / `discover()` / `build_rows()` 时务必回归 `prev/test_fallback_guard.py` |
| 3.13 | 兜底成功时不能写「所有数据源均不可用」 | `collect()` 会改写 warnings 为「已由腾讯批量行情兜底完整覆盖」，并置 `price_fallback_used` |
| 3.14 | `stooq.com` 现有 JS 校验页 | 不能作兜底 |
| 3.15 | pandas 3.x 对 `date` 与字符串推断出不同 datetime 精度，`merge_asof` 报 `dtype('<M8[s]') and dtype('<M8[us]')` 不兼容 | 所有日期列统一 `datetime64[ns]`：`DT="datetime64[ns]"`, `pd.Timestamp(s).as_unit("ns")` |
| 3.16 | 本机 Windows 上 AKShare 的 `py_mini_racer`（V8）偶发 `[FATAL:partition_address_space.cc] Check failed` 原生崩溃 | 与业务代码无关，**Linux runner 上不会发生**；本地重跑一次即可，不要为此改代码 |
| 3.17 | 东财 push2 行情接口短时间请求过多会限流，`RemoteDisconnected` / `502` / `Push2RateLimitError` | 等 5~40 分钟或走腾讯兜底；不要写死重试循环 |
| 3.18 | `DataFrame or DataFrame` 会触发 `ValueError: The truth value of a DataFrame is ambiguous` | 用 `if b is None or b.empty:` 显式判断 |

---

## 4. 本地版 `us-qdii-monitor/` 操作手册

### 4.1 启停与刷新

| 动作 | 命令 |
|---|---|
| 一键刷新并打开 | 双击 `启动.bat` |
| 启动并刷新 | `python app.py --refresh-on-start` |
| 只启动、用现有数据 | `python app.py` |
| 只重算数据、不起服务 | `python refresh.py` |
| 页面内刷新 | 点「立即刷新」（POST `/api/refresh`，带互斥锁） |

* 服务：`http://127.0.0.1:8777/`（端口在 `config.json` 的 `port`）。**没有任何定时任务**。
* 首次刷新补历史净值约 1~3 分钟；之后历史缓存 7 天，日常约 30~40 秒。

### 4.2 HTTP 端点

| 路径 | 作用 |
|---|---|
| `/` | 渲染零外链单列页面 |
| `/api/data` | 返回 `latest.json` 全量数据 |
| `/api/refresh` (POST) | 手动刷新，返回 summary/meta |
| `/export/csv` | 下载 `ranking.csv` |
| `/dashboard.html` | 直接发送自包含 HTML 文件（离线双击用） |
| `/api/chat` | **历史遗留可选**，页面已不调用；由 `chat_engine.py` 提供，可安全删除 |

### 4.3 SQLite 表（`dashboard.sqlite`）

`nav_daily`、`price_daily`、`hist_meta`、`snapshot`、`runs`、`discovery`、`premium_daily`、`f10_scale`。
要全量重算历史：删除 `dashboard.sqlite` 再刷新（慢，只此一次）。

### 4.4 关键配置（`config.json`）

`port`、`premium_primary`（`estimated`/`static`）、`fallback_to_static`、`fallback_max_nav_age_days`、
`index_patterns`、`exclude_pattern`、`fund_type_pattern`、`weights`、`te_window`、`min_te_samples`、
`outlier_static_gap_pp`、`outlier_peer_gap_pp`、`absurd_premium_pct`、`index_symbols`、
`nav_history_refresh_days`、`history_workers`、`history_timeout_sec`。

### 4.5 产物（都由刷新生成，不要手改）

`latest.json`（全量数据）、`ranking.csv`、`llms.txt`（给 AI/人读的摘要）、
`dashboard.html`（零外链单列页面）、`registry.json`、`dashboard.sqlite`、`refresh_run.log`。

### 4.6 已知限制

估算净值是模型推算，不是官方 IOPV；指数用日收盘，不含美股盘中期货；
场外季报规模滞后；QDII 净值 T+1/T+2 披露。

---

## 5. 线上版 `us-qdii-online/` 操作手册

### 5.1 基本信息

* 仓库：`wj402041069-del/us-qdii-premium`（`main` = 源码，`gh-pages` = 产物）
* 页面：`https://wj402041069-del.github.io/us-qdii-premium/`
* runner：`ubuntu-latest` + Python 3.12；`permissions: contents: write`
* 定时（UTC，全年固定 UTC+8，中国无夏令时）：`0 13 * * *` = 21:00 主跑、
  `30 13 * * *` = 21:30 补跑、`0 15 * * *` = 23:00 补跑；另有 `workflow_dispatch` 与 `push(main)` 触发。
* `concurrency.group: qdii-daily`，`cancel-in-progress: false`。

### 5.2 流水线步骤（`.github/workflows/daily.yml`）

```
checkout → setup-python → pip install -r requirements.txt
→ python tools/load_state.py        # 从 gh-pages 回读 history/registry/seed/meta
→ python tools/gate.py prev/meta.json   # 输出 true/false
→ python refresh.py                 # 采集 + 计算 + 生成 dist/
→ python cleanup.py --limit-mb 500  # 清运行垃圾 + 体积体检
→ bash tools/deploy.sh              # 孤立单提交 + 强推 gh-pages
```

* `gate.py`：**只有 schedule 事件**才判断；手动触发 / push 一律放行；
  上次是 `stale` 也放行；判定条件是「当天北京时间 **20:00 之后**已有成功产出」才跳过
  （避免白天 push 的快照占掉晚上 21:00 的正点刷新）。
* `load_state.py`：runner 每次是全新机器，历史序列 / 台账 / 兜底快照必须从 `gh-pages` 读回；
  分支不存在时退回仓库内 bootstrap 文件。

### 5.3 四层数据源

| 层 | 内容 |
|---|---|
| L0 | `a-stock-api` Worker：`fund-nav` / `etf-premium` / `trade-calendar`（辅助、交叉校验） |
| L1 | 东财 / 腾讯 / 新浪直连：名录、ETF/LOF 快照、pingzhongdata、美股指数、汇率、价格 K 线、腾讯批量行情 |
| L2 | AKShare：全量净值表、申购状态、新浪份额、集思录交叉校验 |
| L3 | 仓库内 `seed.json` / `history.json` 上次成功快照 |

* `sources.collect()` 每条链独立降级、`STATUS` 记录 attempts / failed / optional。
* 核心降级 `degraded_chains` 与辅助降级 `optional_degraded` 分开统计；
  `OPTIONAL_CHAINS = {"场外份额规模", "集思录交叉校验", "Worker 溢价交叉校验"}`。
* **所有实时源同时失效** → `publish_stale()` 原样发布上次成功快照、打 `stale=True`、
  页面显示红色「⚠ 数据陈旧」横幅，且不把降级结果写回 `seed.json`。

### 5.4 发布产物（`dist/` → `gh-pages`）

`index.html`（零外链单列页）、`latest.json`、`seed.json`、`ranking.csv`、`llms.txt`、
`source_status.json`、`history.json`、`registry.json`、`meta.json`、`.nojekyll`、
`cleanup_report.json`。

### 5.5 体积与发布纪律

* `cleanup.py`：清 `__pycache__` / `*.pyc` / `*.log` / `*.tmp` / `*.sqlite` 等；
  超 500MB 时先按 180→90→60 个交易日截断历史，再按
  `history.json → source_status.json → llms.txt → seed.json` 顺序丢弃；仍超限则 `SystemExit` 拒绝发布。
* `deploy.sh`：`dist/` 复制到 `.deploy/` → `git init -b gh-pages` → 单个 commit →
  `--force` 强推。**旧提交立即不可达，仓库体积恒定**。
* 校验方式：GitHub API 查 `gh-pages` HEAD 的 commit，正常应 `parents=0`。

### 5.6 如何手动改并发布

1. 改 `us-qdii-online/` 下的源码（**不要改 `dist/`，它被 gitignore，由 Actions 重算**）。
2. 本地先跑第 7 章验证清单。
3. 把改动文件推到 `main`。项目里没有本地 `.git`，历史上用 GitHub REST API
   （`GET /git/ref/heads/main` → `POST /git/blobs` → `POST /git/trees`（带 `base_tree`）→
   `POST /git/commits` → `PATCH /git/refs/heads/main`）创建单个 commit。
   **凭据只在运行时从用户处取得，绝不写进任何文件、日志或命令回显。**
4. push 会触发 `daily.yml`；等待 run `conclusion=success`。
5. 带 cache-buster 请求 Pages，核对 `index.html` / `latest.json` / `meta.json` /
   `source_status.json` / `cleanup_report.json` 均为 200 且内容符合预期。

### 5.7 本地如何跑线上版

```bash
cd us-qdii-online
pip install -r requirements.txt
python refresh.py                 # 生成 dist/
python cleanup.py --limit-mb 500
```

`dist/index.html` 双击即可离线查看；只有「自动刷新」是线上才有的。
本地跑不等于发布，发布只由 `tools/deploy.sh` 在 Actions 里完成。

---

## 6. 前端约定（两套模板）

* 页面是**零外链自包含 HTML**，只有一个模板变量 `{{ data_json|safe }}`；
  线上版由 `refresh.render_html()` 用字符串替换渲染，本地版由 `app.render_dashboard_html()`
  经 Flask `render_template` 渲染（从 `refresh.py` 直接调用时必须 `with app.app_context():`）。
* 渲染时 payload 要做 `.replace("</", "<\\/")`，防止 JSON 里的 `</script>` 截断脚本。
* **布局：单列全宽**（`.grid{grid-template-columns:1fr}`）。右侧对话窗口已于 2026-11-11 移除，
  浏览器内规则引擎 JS 一并删除；不要再引入聊天 UI。
* `.chip` 样式必须保留：主表格「按主代码折叠」的展开按钮复用了它。
* 页面自上而下：KPI/提示 → 工具条 → 主表格 → 指标中文注释卡 → 限额排名卡 → 散点图 → 页脚。
* 必须保留：主口径切换、指数/场所/限额筛选、排序、按主代码折叠、搜索、评分权重即时调整、
  表头 ⓘ 简注、`METRIC_NOTES` 注释卡、限额榜（申购状态筛选 / 金额上限 / 最紧最松 / 前 N /
  含场内 / 点行定位）、CSV 导出、涨红跌绿。
* 限额榜 4 个控件的默认项必须是各自 `<select>` 的**第一项**：
  `selLimitStatus=""`（全部状态）、`selLimitRank="loose"`（最松）、`txtLimitMax` 空（不限 元）、
  `selLimitTop="999"`（全部）。改顺序等于改默认视图，需同步 7.2 基线与两份 README。
* 线上版额外有「数据源状态」按钮（`renderSources()`）与陈旧横幅；本地版额外有「立即刷新」
  （离线时用 `pushNotice()` 在顶部提示区反馈，不再依赖聊天窗口）。
* 前端验证方式见 7.2：把渲染后 HTML 的 `<script>` 抽出做 `node --check`，
  再用 Node `vm` + DOM 桩跑一遍初始化（桩给 `selLimitRank='loose'`、`selLimitTop='999'`、
  `selLimitStatus=''`、`w1..w4` 默认权重），确认无异常、注释 11 行、表头 ⓘ 14 个、
  限额榜默认 68 行，且 4 个状态档的计数与主表筛选完全一致。

---

## 7. 每次改动后的验证清单

### 7.1 Python

```bash
cd us-qdii-monitor && python -m py_compile app.py refresh.py sources.py metrics.py chat_engine.py
cd us-qdii-online  && python -m py_compile refresh.py sources.py metrics.py cleanup.py tools/*.py
```

### 7.2 前端（两套都要）

```bash
# 渲染两个页面并抽出脚本
node --check <抽出的 script.js>
# DOM 桩回归脚本（线上版 prev/check_pages.js）
node us-qdii-online/prev/check_pages.js
```

DOM 桩基线：`INIT OK`、无 `msgs/chips/txtAsk/btnAsk` 引用、注释卡片 11 个 `<tr>`、
指标条目 10、表头 ⓘ 14、卡片默认值 `全部状态 / loose / 999`、限额榜默认 68 行；
卡片状态筛选 `all=68 / open=14 / limited=82 / paused=56`（**必须与主表同名筛选项完全相等**）；
主表筛选 `le10=37 / le100=48 / ge10000=2 / paused=56 / limited=82 / open=14`、
`COLS` 含 `limit_amount`、`STATUS_LABEL['']` 为「全部状态」。

> `prev/` 是**本地验证资产**，被 `.gitignore` 忽略、不会出现在 GitHub 仓库里。
> `prev/check_pages.js` 现在直接读取渲染后的 `dist/index.html` 与 `../us-qdii-monitor/dashboard.html`
> 抽取脚本，不需要额外的 `_online_page.js` 之类的中间文件；改完模板**先重渲染再跑它**。
> 若你是从 GitHub 仓库单独接手、拿不到 `prev/check_pages.js` 与 `prev/test_fallback_guard.py`，
> 按上面描述自行搭一个最小 DOM 桩即可（要点：`document.getElementById` 返回带
> `value / innerHTML / style / dataset / checked / appendChild / querySelectorAll` 的对象，
> `selLimitStatus=''`、`selLimitRank='loose'`、`selLimitTop='999'`、`w1..w4` 给默认权重，
> 脚本尾部追加 `;globalThis.__api={S,visibleRows,limitMatch,renderLimitRank,STATUS_LABEL,METRIC_NOTES,COLS};`）。

### 7.3 本地端到端

```bash
cd us-qdii-monitor
python -u -c "import refresh; print(refresh.run())"
python -c "import app; c=app.app.test_client(); [print(p,c.get(p).status_code) for p in ['/','/api/data','/dashboard.html','/export/csv']]"
```

预期：94 只、场内 18、降级 0、存疑 0、约 30~40 秒；`dashboard.html` 零外链、无 Jinja 残留。

### 7.4 线上端到端（本地）

```bash
cd us-qdii-online
python -u -c "import refresh; print(refresh.run())"
python prev/test_fallback_guard.py      # 腾讯兜底错配的离线回归，必须 ALL GUARD TESTS PASSED
python cleanup.py --limit-mb 500
```

预期：`94 只 / 场内 18 / downgraded 0 / doubt 0 / priced 18 / stale false`，
`000055` 是 `OTC`、`price=None`、`rejected=''`，无 >60% 溢价，最高主溢价约 20%。

### 7.5 线上发布核验

* GitHub Actions 最近一次 `每日刷新 QDII 溢价与跟踪度` = `success`，pages build = `success`。
* Pages（带 `?v=<时间戳>`）：`index.html` 200 且含「指标中文注释 / 限额排名 / 日限额 /
  全部状态 / 最松（限额最大）」、不含 `card chat` / `id="msgs"`；抽出脚本 `node --check` 通过。
* `latest.json`：94 行、`stale false`、`price_date` 为最近交易日、
  `summary.downgraded/doubt` 为 0、无异常溢价。
* `source_status.json`：`core_degraded` 尽量为空；有降级时看 `warnings` 是否说清覆盖方式。
* `cleanup_report.json`：`ok true`，`after_bytes` 约 1.2MB（远低于 500MB）。
* `meta.json`：`generated_at` 为本次；`gh-pages` HEAD commit `parents=0`。

---

## 8. 故障排查速查表

| 症状 | 最可能原因 | 处理 |
|---|---|---|
| 出现 100%+ 的溢价 / 场外基金变成场内 | 行情兜底代码错配（3.11） | 检查白名单、`venue_hint`、`absurd_premium_pct`、场外不继承快照价格；跑 `prev/test_fallback_guard.py` |
| 价格日变成 1970 年、估算全部降级 | `f297` 被当 unix 时间戳 | 按 `YYYYMMDD` 字符串切片（3.7） |
| TE 突然从 2% 涨到 20~30% | 净值日改成了「严格早于」 | 恢复同日命中 + `allow_exact_matches=True`（2.3） |
| `merge_asof` 报 dtype 不兼容 | pandas 3.x 日期精度 | 统一 `datetime64[ns]`（3.15） |
| 场外规模大面积缺失 | F10 514 / 新浪份额字段变化 | 走 `pingzhongdata` 串行 + 30 天缓存（3.8/3.10） |
| 东财 clist 全挂 | push2 限流 | 等 5~40 分钟或让腾讯兜底；看 `source_status.json` 的 attempts |
| 页面显示「⚠ 数据陈旧」 | 全源失效，发布了上次快照 | 正常降级；下次补跑会自动修复，不要用陈旧数据做当日判断 |
| 页面空白 / Jinja 残留 | 渲染没替换占位符 | 检查 `render_html()` / `render_dashboard_html()` 的 app_context |
| 页面 JS 报错 `xxx is not defined` | 删函数时漏了引用 | 跑 7.2 的 DOM 桩，它会执行完整初始化 |
| 本地刷新进程直接崩、无 Python 栈 | Windows `py_mini_racer` 原生 FATAL（3.16） | 重跑一次；Linux runner 不受影响 |
| 仓库体积随时间变大 | 改成了累积 commit / 没强推 | 恢复孤立单提交 + `--force`（5.5） |
| 21:00 正点没刷新 | `gate.py` 语义被改 | 恢复「当天 20:00 后已有成功产出才跳过」 |
| `ValueError: truth value of a DataFrame is ambiguous` | `df1 or df2` | 改 `if b is None or b.empty:`（3.18） |

---

## 9. 安全与运维红线

* 仓库、日志、页面、产物中**不得出现任何 PAT / Secret**。用户曾在本项目对话中明文提供过 GitHub PAT，
  已在交付说明中多次提醒轮换；它从未写入仓库文件。
* 日常流水线只用 Actions 自动注入的 `secrets.GITHUB_TOKEN`；手动改仓库时才临时使用用户凭据，
  且**不得打印、不得落盘**。
* 不要提交 `dist/`、`data/state.json`、`.deploy/`、`prev/`、`*.sqlite`、`*.log`（见 `.gitignore`）。
* 不要给本地版加定时任务，不要把线上版改成需要服务器的架构。
* 不要把 IOPV 当实时估值，不要输出未经标注的降级/陈旧数据。
* 仅供数据观察与学习，**不构成投资建议**（页脚必须保留免责声明）。

---

## 10. 常见需求怎么改（食谱）

| 需求 | 改哪里 | 注意 |
|---|---|---|
| 主表加一列指标 | 两套模板的 `COLS` 数组 + 后端 `COLUMNS` / 导出 `cols` | 同时加 `NOTE` 简注和 `METRIC_NOTES` 详注；跑 7.2 确认表头 ⓘ 数量变化 |
| 加一个筛选 | 模板工具条加 `<select>`、`S` 加字段、`visibleRows()` 加条件、`bind()` 加事件 | 两套都要同步，筛选口径保持一致 |
| 加一个卡片状态档 | `selLimitStatus` 加 `<option>`、`limitMatch()` 加分支、`STATUS_LABEL` 加文案 | 复用 `limitMatch(r,f)` 第二参数，别另写一套判定；跑 7.2 核对卡片计数 == 主表计数 |
| 改评分权重 | `config.json` 的 `weights`（固化）/ 页面输入框（临时） | 页面权重只影响本页，不写回后端 |
| 调整标的范围 | `config.json` 的 `index_patterns` / `exclude_pattern` / `fund_type_pattern` | 改完看 `registry.json` diff，确认没有误纳中证500 类 |
| 永久剔除某只基金 | `registry.json` 里该 code 的 `status` 改 `excluded` | 唯一允许人工覆写的数据资产；自动发现不会覆盖 |
| 改异常阈值 | `absurd_premium_pct` / `outlier_*_gap_pp` | 不要用放宽阈值掩盖数据源 bug |
| 加一个数据源 | `sources.py` 加 provider，挂到 `CHAIN_LABELS` 对应链 | 必须写 validator、超时、单位校验；标 optional 还是 core 要明确 |
| 改定时时间 | `.github/workflows/daily.yml` 的 cron | 记住 UTC = 北京时间 −8；改完同步 `gate.py` 的 20:00 阈值语义 |
| 改页面样式 | 两套模板的 `<style>` | 保持零外链、单列全宽、涨红跌绿；`.chip` 不能删 |
| 删除遗留对话接口 | 删 `chat_engine.py`、`app.py` 的 import 与 `/api/chat` 路由 | 页面已不调用，删除是安全的；README 同步 |
| 重新部署线上 | 推 `main` → 等 Actions → 核验 7.5 | 不要手动改 `gh-pages`，不要改 `deploy.sh` 的孤立提交逻辑 |

---

## 11. 基线数据（判断"这次是不是正常"）

以 2026-11-11 01:26 的线上快照为基准：

| 项 | 正常值 |
|---|---|
| 池内代码 | 94（SP500 22 / NDX100 72；场内 18 / 场外 76） |
| TE 有效 | 92 只；TE 中位数约 2.4%；R² 中位数约 0.987 |
| 规模非空 | 93/94（本地版）；线上由季报净资产兜底 |
| 估算/静态 | 当日 `est_ratio=1`，两者相等是正确结果 |
| 最高主溢价 | 20.29%（`159501`，2026-11-10 数据日） |
| 降级 / 存疑 / rejected | 理想都是 0 |
| 申购状态分布 | 暂停申购 54、限大额 26、场内交易 12、封闭期 2 |
| 限额榜状态筛选 | 默认「全部状态」68 条；开放/不限额 14、限大额 82、暂停/封闭期 56（与主表同名筛选相等） |
| 页面体积 | 线上约 127KB、本地约 122KB（零外链；新增状态筛选后略增） |
| 刷新耗时 | 本地约 32 秒；线上增量约 44 秒，首次全量约 202 秒 |
| 产物体积 | `cleanup_report.json` 的 `after_bytes` 约 1.21MB |
| 发布形态 | `gh-pages` HEAD commit `parents=0` |

> 数值会随行情变化，但**数量级和结构**应稳定：如果池内代码突然从 94 跳到 200+，
> 或场内从 18 变成 19，第一反应是名单发现 / 场所归属被污染，而不是"行情变好了"。

---

## 12. 变更记录（摘要）

| 日期 | 变更 |
|---|---|
| 2026-11-10 | 本地版交付：Flask + SQLite + 手动一键刷新 + 自动发现名单 + 三层溢价 + 跟踪度 |
| 2026-11-11 | 线上版交付：GitHub Pages + Actions（21:00 / 21:30 / 23:00）、四层数据源、500MB 清理、孤立单提交、全源失效降级 |
| 2026-11-11 | 新增 TE/TD/R² 中文注释、限额排名与筛选（两套页面） |
| 2026-11-11 | 修复腾讯行情兜底代码错配导致的 209% 假溢价（白名单 + venue_hint + 荒谬溢价护栏 + 场外不继承快照价格） |
| 2026-11-11 | 移除两套页面右侧对话窗口，改为单列全宽；线上版浏览器内规则引擎 JS 一并删除；本地版 `chat_engine.py` / `/api/chat` 保留为可选兼容接口 |
| 2026-11-11 | 新增本维护手册 |
| 2026-11-11 | 「限额排名 · 申购额度」新增申购状态筛选（默认全部状态），卡片默认改为 最松（限额最大）/ 不限 元 / 全部；状态筛选复用主表 `limitMatch()`，两套页面与文档同步 |

---

**记住一句话**：这套系统的价值不在功能多，而在**数字可信**。
任何改动，先问"会不会让一个错数看起来像对的"，再跑第 7 章验证清单。
