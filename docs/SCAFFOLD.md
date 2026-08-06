# LabPilot 技术脚手架

> 这份文档给**下一个实现 Material PD LabPilot 的 coding agent**。
>
> 它不是需求文档，而是一份**经验移交**：Prism（本仓库）已经把「异构数据 → 统一 Schema → 自然语言分析 → 图表 → 可解释洞察」这条链路完整跑通过，35 个测试全绿。下面写的是**哪些设计被验证有效、哪些坑真实踩过、哪些地方我们也没走到**。
>
> **UI 与视觉风格完全交给你决定。** 本文只约束数据流、安全边界和状态机 —— 这三样换个皮就重写的成本最高。

---

## 0. 三句话总结经验

1. **Schema 必须是数据，不是代码。** 我们最贵的一个 bug 是 schema 被写在三个地方（建表语句、前端硬编码数组、Prompt 里的表清单）然后各自漂移。修法是把 canonical schema 存进数据库、**从物理表反向 seed**，Prompt 每次从库里生成。这正是目标架构里 "Dynamic Schema: Auto-updates prompts via DB master" 想要的东西，我们已经实现了，直接抄。
2. **Agent 化之后，安全检查必须挪进工具内部。** 固定流水线 `plan → guard → execute` 的安全性来自顺序；Agent 自己决定调用顺序，顺序保证就没了。把 SQL Guard 放进 query tool 里面，让它成为模型到数据库的**唯一路径**。
3. **模型静默忽略它不认识的字段。** 配置写错不会报错，只会「看起来配了但没生效」。所有 provider 参数都要**严格枚举 + 启动即报错**，否则你会花一整个下午 debug 一个根本没发出去的参数。

---

## 1. 目标架构 vs 已验证实现：差异地图

先说清楚哪些能直接复用、哪些是新地。**标记为「新领域」的部分我们没做过，不要以为文档里有就是验证过的。**

| 目标层 | 目标组件 | Prism 的实践 | 可复用度 |
| --- | --- | --- | --- |
| Application | Streamlit UI | React + Vite + A2UI over SSE | **换。** 但事件模型和「事件持久化重放」要留，见 §6 |
| Application | Multi-Modal Input（图片 OCR / PDF） | 只做了 CSV / XLSX / 纯文本粘贴 | **新领域。** 见 §4.1 的接口切口 |
| Application | Dynamic Charts | MCP（AntV）出图 → PNG 落本地 | 模式可留，实现建议换，见 §8 |
| Intelligence | Floodgate / Gemini | DeepSeek 已验证；Gemini dialect 已写**未验证** | 结构直接用，见 §7 |
| Intelligence | Text-to-SQL | SQLGlot AST 白名单 + Agent 自修复 | **直接复用**，见 §5 |
| Intelligence | Agent Skills | deepagents `SkillsMiddleware` | **直接复用**，见 §9 |
| Persistence | SQLite | DuckDB | 换，注意事项见 §3.2 |
| Persistence | Dynamic Schema | `registry.py` + `semantic.py` | **直接复用，这是本项目最核心的经验**，见 §4.2 / §4.3 |
| Persistence | JSON Pipeline（严格抽取 + UPSERT） | 有 mapping draft + commit，**没有**严格 JSON Schema 抽取与 UPSERT | **部分新领域**，见 §4.4 |
| Step 4 | Email / 治理 / SSO | 未做 | 新领域 |

---

## 2. 系统分层

```
┌─ UI 层 ────────────────────────────────────────────┐
│  三个页面就够：Sources / Schema / Analyze          │
│  唯一硬要求：Analyze 必须能渐进渲染 Agent 事件流    │
└───────────────────┬────────────────────────────────┘
                    │
┌─ 编排层 ──────────┴────────────────────────────────┐
│  Ingestion   Schema Registry   Semantic Layer      │
│  Analysis Agent（唯一工具 = 受保护的 execute_query）│
└───────────────────┬────────────────────────────────┘
                    │
┌─ 安全层 ──────────┴────────────────────────────────┐
│  SQL Guard（AST 白名单）  Provider Dialect（严格枚举）│
│  Asset Host 白名单（SSRF 边界）                     │
└───────────────────┬────────────────────────────────┘
                    │
┌─ 数据层 ──────────┴────────────────────────────────┐
│  单文件数据库：业务事实表 + 元数据表在同一个库里    │
└────────────────────────────────────────────────────┘
```

**关键结构决策：元数据和业务数据放同一个库。** canonical schema、join 规则、会话、消息、事件全部是普通表。好处是 Schema 可以运行时编辑而不是只能改源码，且备份/重置只有一个文件。

---

## 3. 技术选型与迁移注意

### 3.1 已验证的选型

| 组件 | 选择 | 结论 |
| --- | --- | --- |
| SQL 解析/校验 | **SQLGlot** | 非常值得。AST 级白名单比正则可靠得多，还能顺手做 LIMIT 注入和 SQL 规范化 |
| 数据读取/Profiling | **Polars** | 读 CSV/XLSX、算 null 率和 distinct 都很顺手 |
| Agent 框架 | **LangChain `create_agent`** | 省掉了手写 tool-call 循环和流式解析。注意 §5.3 的递归上限 |
| Agent Skills | **deepagents middleware** | 不要自己实现 SKILL.md 发现和渐进披露 |
| 工具生态 | **MCP（stdio）** | 见 §8，缓存策略是重点 |

> 历史包袱提示：`docs/Mini Hackathon.md` 是最初的设计文档，里面「暂不使用 LangGraph」「自研轻量 ModelClient」等结论**后来都被推翻了**。以代码为准。

### 3.2 DuckDB → SQLite 迁移注意

1. **SQLGlot 方言换成 `sqlite`**（读和写都要换）。
2. **日期函数不兼容，且失败方式极其阴险。** DuckDB 是 `strftime(date_expr, '%Y-%m')`，SQLite 是 `strftime('%Y-%m', date_expr)` —— **参数顺序相反**。已实测：SQLite 拿到写反的参数**不报错，静默返回 `NULL`**，于是月度趋势图会安静地变成一堆空桶，没有任何错误信号。我们在 Prompt 里写死了日期格式化写法（见 §5.2），换库时这段必须重写并**补一条断言测试**，不能只靠人眼看。
3. **SQLite 没有原生 DATE 类型**，日期按 TEXT(`YYYY-MM-DD`) 存最省事，比较和排序仍然正确。
4. **单写者锁是共通问题。** DuckDB 会直接报 `Could not set lock on file`；SQLite 默认也会在写冲突时 `database is locked`。建议开 WAL（`PRAGMA journal_mode=WAL`）允许读写并发。**这个坑一定会遇到**：跑着 demo server 的时候跑测试就炸。
5. **白捡的一层防御：** SQLite 支持只读连接（`sqlite3.connect("file:app.db?mode=ro", uri=True)`）。**强烈建议让 query tool 用只读连接**，和 AST Guard 形成双保险 —— 这是 DuckDB 给不了我们的，你能白拿。*（此项我们未实测，但属 SQLite 标准能力。）*

---

## 4. 数据接入与 Schema（最值得抄的部分）

### 4.1 接入与 Profiling

流程：**上传 → Profile → 预览 → 字段映射草稿 → 人工确认 → commit**。状态机只有三档：`NEEDS_REVIEW → READY`（外加解析失败）。

Profiling 每列输出：推断类型、null 率、distinct 数、3 个样本值、警告（null 率 > 20% 提示、常量列提示）。**样本值是关键** —— 人在确认映射时几乎只看样本值，不看类型。

字段映射草稿用**源字段名 token 匹配**给初始建议（`sample`→`sample_id`、`cost`/`amount`→`cost_amount`…），带 confidence 和 reason，匹配不上就标 `IGNORED` 并给出理由。**不要追求自动映射准确率**，这是一个「给人一个好起点」的功能，不是自动化功能。

> **多模态接口切口（新领域）：** 我们只有 `csv | xlsx | text` 三条解析分支，都收敛到「polars DataFrame 或纯文本」。加 OCR/PDF 时保持这个收敛点：`bytes → DataFrame | str`，后面的 Profile / 映射 / commit 全部可以不动。图片和 PDF 走「非结构化」分支，和我们的 Slack 文本分支同构。

### 4.2 Schema Registry —— "Dynamic Schema" 的具体实现

**这是整份文档最重要的一节。**

问题：canonical schema 写在三处（建表语句 / 前端硬编码数组 / Prompt 里的表清单）并且已经漂移 —— 前端下拉框提供 `sample_id`、`cost_amount`、`lab_vendor` 三个**根本不存在**的字段，同时漏掉了 6 个真实存在的列。

解法：

- 建两张元数据表 `canonical_entities` / `canonical_fields`，**从物理表 DESCRIBE 反向 seed** —— 这样注册表**在结构上不可能凭空捏造一个列**。
- 字段除了名字和类型，还要带三样东西：
  - **`description`** —— Prompt 里最缺的东西。「`quality_target_pct`：合同约定通过率，**以 0-1 小数存储**」这种说明能直接避免模型算错量纲。
  - **`result_format`**（`CURRENCY_USD` / `PERCENT_2` / `DATE` / `MONTH` / …）—— 渲染格式是**字段的属性**，不该让模型每个问题重新决定一次。
  - **`unit`** / **`enum_values`**。
- 提供运行时编辑 API，但**只允许改语义（display_name / description / result_format / unit），不允许改标识符和类型** —— 后两者属于物理表，允许漂移就等于把这个注册表要解决的 bug 又请回来。
- 前端下拉框读 `GET /schema/canonical-fields`，**永远不准写字面量数组**。

最后 `registry_prompt_context()` 把整个注册表渲染成 Prompt 片段：

```
fact_test_results -- 一次实验室检测，所有分析的粒度
  vendor TEXT | 执行检测的实验室 | render as TEXT
  cost_usd DECIMAL | 该检测收费 | unit=USD | render as CURRENCY_USD
  result TEXT | 检测结论 | values=PASS/FAIL | render as TEXT
```

**改变模型对 schema 的认知 = 改注册表，而不是改 Prompt 字符串。**

### 4.3 语义层：人工确认的联表规则 → 发布视图

星型模型（1 事实表 + N 维度表）对模型不友好，宽表又不好维护。折中方案是：**让用户在 UI 上确认 join 规则，系统据此生成一个宽视图**。

- `join_rules` 表存规则（左表/左字段/右表/右字段/join 类型/基数）。
- 发布前**拿真实数据校验**，不是校验语法：
  - 算 **match rate**（左连接命中率）并展示给用户；
  - 校验 **右键唯一性** —— `MANY_TO_ONE` / `ONE_TO_ONE` 要求右侧键唯一，不唯一直接拒绝并说明原因；
  - 每张维度表只允许 join 一次。
- 通过后 `CREATE OR REPLACE VIEW vw_laboratory_analysis AS ...`，列名冲突时自动加表名前缀。
- Prompt 里同时给出**视图的完整列清单**和**已发布的关系列表**，并明确写「涉及合同/SLA/预算/材料标准时优先用这个视图，**不要再往上加 join**」。

收益：模型不需要自己推断 join 关系，join 正确性由人工确认背书一次，之后所有查询共享。

**同步陷阱：** SQL Guard 的白名单表清单和语义层的分析表清单是两个模块里的两份字面量，改一个必须改另一个。建议你直接做成单一来源。

### 4.4 JSON Pipeline / UPSERT（部分新领域）

我们做到的：映射草稿有版本号、可编辑、commit 后置为 `READY`。
我们**没做**的：从非结构化文本严格抽取成 JSON schema 再 UPSERT 进事实表。

如果要做，基于我们的经验给两条建议：

1. **抽取要给 JSON Schema 并做校验**，失败就退回人工复核队列，不要静默丢弃 —— 参考 §10 的「不要用假数据兜底」。
2. **UPSERT 需要稳定业务主键。** 我们的事实表用 `TR-00001` 这种生成 ID，跨源去重靠的是 `sample_id` 语义。做 UPSERT 前先确定「同一条检测」在不同源里怎么判定，这个决定比代码难。

---

## 5. Text-to-SQL 与 Analysis Agent

### 5.1 架构：一个 Agent，一个工具，工具内含守卫

```
用户问题
  └─ Agent（LLM + 工具循环）
       └─ execute_query(sql)          ← 唯一工具
            ├─ SQL Guard（AST 校验）   ← 唯一到数据库的路径
            └─ 数据库执行
```

**为什么守卫在工具里面：** 早期是固定流水线 `plan → guard → execute`，安全性来自顺序。改成 Agent 后模型自己决定调用顺序，顺序保证消失。把守卫放进工具，就恢复成了结构性保证。

> **移交给你的硬性不变量：任何新增的、能碰数据库的工具，都必须走同一个守卫。** 否则这个保证会无声失效。

**错误要 `return` 给模型，不要 `raise`。** 校验失败返回 `"REJECTED: <原因>"`，执行失败返回 `"QUERY FAILED: <错误>"`。模型看到后会自己改写重试 —— 这直接替代了我们早期手写的「两次修复重试」逻辑，代码更少效果更好。

### 5.2 SQL Guard 实现要点

1. **先做 token 黑名单**（`attach|copy|create|delete|detach|drop|export|import|insert|install|load|pragma|replace|truncate|update`）和**外部读取函数黑名单**（`read_csv|read_parquet|glob|httpfs|sqlite_scan|...`）—— 这一层挡住绝大多数注入尝试，成本极低。
2. **再做 AST 校验**：必须恰好一条语句、必须是查询、**提取所有表名减去 CTE 名**后必须全在白名单内、且不能一张表都不引用。
3. **没有 LIMIT 就注入 `LIMIT 200`。**
4. **把校验后的 AST 重新序列化成 SQL，后续所有环节都用这一份**（EXPLAIN、执行、工具调用展示、最终 SQL 披露面板）。这样用户看到的 SQL 和真正执行的 SQL 一定一致 —— 对「可解释」这个产品诉求是硬要求。
5. 执行前先跑一次 `EXPLAIN`，能低成本挡掉一批错误。

### 5.3 Prompt 里必须写死的东西

Prompt 由三段拼成：`基础规则 + 语义层上下文 + 注册表上下文`。

基础规则里这几条是被实测验证过的（**不写就一定出问题**）：

- 一次一条只读 SELECT，最多 200 行。
- **别名必须 snake_case**；计数用 `_count` 结尾且保持整数；金额用 `_usd` 结尾且显式 `ROUND(..., 2)`；比率**统一 0-100 标度**、`_pct` 结尾、显式 `ROUND(..., 2)` —— **明确禁止返回 0-1 小数用于展示**（这是最高频的量纲错误）。
- 日期一律用格式化函数转成文本返回，月份桶用 `%Y-%m`。
- **不要重复查询同一件事**；一次查询够用就直接解释。
- 回答时**只能用查询返回的数值，不许自己重算**；先给结论，再给最多两条证据。
- 问题超出 schema 能回答的范围就明说并追问，**不要猜**。

另外：**Agent 循环必须设上限。** 每次迭代都是真实的模型调用和真实查询。我们设 `MAX_ITERATIONS = 8`，并把框架的 `recursion_limit` 设为它的 2 倍（一次迭代 = 模型消息 + 工具消息）。

### 5.4 会话记忆

多轮追问（「现在只看 Ceramic-C」）必须要有历史。我们的做法很朴素但够用：

- 只取**已完成**、内容非空、且**时间戳早于当前消息**的轮次（当前轮的记录在查询时已经存在了，必须按时间排除，不能假设它不在）。
- 上限 **6 轮**、每条截断到 **800 字符**，避免长会话把 Prompt 撑爆。
- Prompt 里明确写：**历史只用来理解指代，所有数字必须来自本次查询。**

---

## 6. 流式与状态：给 Streamlit 的迁移建议

我们用的是 A2UI over SSE，那套协议你可以整体丢掉。但**事件模型和持久化策略要留**，理由在下面。

### 6.1 事件模型（建议保留）

Agent 运行时产出四类事件，UI 按顺序渐进渲染：

| 事件 | 内容 |
| --- | --- |
| `reasoning_delta` | 思考过程增量（受开关控制） |
| `tool_call` | 工具开始，带**入参**（SQL 原文） |
| `tool_result` | 工具结束，带**结果摘要**（行数、列名、被拒原因） |
| `content_delta` | 正文答案增量 |

把 SQL 守卫和数据库执行**作为两个独立的工具事件**暴露（`SQLGlot · validate_sql` 和 `DuckDB · execute_query`），用户能看到「校验通过 → 执行 → 返回 N 行」的完整过程。**这是「可解释」诉求的主要载体**，比事后给一段 SQL 有说服力得多。

### 6.2 持久化：这一条对 Streamlit 尤其重要

**每个事件都按序号落库，最终数据模型整体快照存进消息表。**

这在我们的架构里是为了支持断线续传和刷新重放。**而 Streamlit 每次交互都会重跑整个脚本** —— 这意味着上面这个设计对你来说不是可选项，而是刚需：

- 历史会话**从库里重放**，绝不重跑分析（重跑 = 重新烧钱 + 结果不一致）。
- 快照里已经包含了「当时是否开启思考模式」的渲染结果，**重放时原样渲染**，不要拿当前开关重新过滤。
- Streamlit 的 `session_state` 只当缓存，**真相在库里**。

### 6.3 幂等与取消

- 请求带 **Idempotency-Key**；同 key 同问题 = 续传，同 key 不同问题 = 冲突报错。
- 取消要**双路生效**：进程内取消令牌（能中断数据库查询）+ 数据库状态置 `CANCELLED`（进程内已经找不到时的兜底）。
- UI 端取消时把所有 `RUNNING` 的工具调用标成 `CANCELLED`，否则界面会永远停在转圈状态。

---

## 7. Provider Dialect：把「怎么让模型思考」隔离出来

传输、重试、流式、工具绑定交给框架。**只有一件事框架替代不了：每个 provider 怎么表达「思考 / 不思考」。**

把它收敛成一个注册表：`Provider` 枚举 + `ReasoningEffort` 枚举 + 每家支持的档位集合 + 每家的请求体构造函数。切换 provider 就变成改 `.env`。

**三条血泪经验：**

1. **参数位置错了不会报错。** DeepSeek 的 `reasoning_effort` 在请求体**顶层**才会被校验（值非法直接 HTTP 400），**嵌在 `thinking` 里会被静默忽略**。我们为此浪费了整整一个调试周期。任何「开关看起来没生效」的问题，先用非法值探测这个字段到底有没有被服务端校验。
2. **未知配置值必须启动即报错。** provider 会忽略不认识的字段，所以 `LLM_PROVIDER=deepsek` 这种拼写错误会「看起来配好了但什么也没做」。严格枚举解析 + 明确报错信息（列出所有合法值）。
3. **OpenAI SDK 默认信任代理环境变量。** 必须显式 `trust_env=False`，否则环境里的 SOCKS 代理会截获模型流量。

档位处理策略：**provider 不支持的档位翻译成最接近的一档，而不是丢弃。** 丢弃会让配置静默降级。

> **诚实标注：** DeepSeek dialect 已对着真实 API 逐一验证（7 个档位 × 2 种模式共 14 种组合全部通过）。**Gemini dialect 是照文档写的，从未用真实 key 跑过**。你接 Gemini 时把它当作未验证代码对待 —— 特别是「思考预算设 0 表示关闭思考」这条。

---

## 8. 图表：让模型选，不要硬编码映射

**核心主张：不要在代码里写「数据长这样 → 用柱状图」的映射表。** 把可用的图表工具 schema 交给模型，让它根据问题和结果自己选，并自己填参数。

我们用 MCP（AntV chart server，stdio）实现，两个优化很关键：

1. **工具 schema 全进程只发现一次并缓存。** 启动 stdio server 要几秒钟。缓存后模型**先从缓存的 schema 里选工具，选完才启动那一个 server** —— 不需要图表的问题一个进程都不会起。
2. **发现失败不进缓存。** 一次 `npx` 抖动如果被缓存成「无可用工具」，整个进程生命周期内图表功能就废了。另外加**线性退避重试（默认 3 次）**，`npx` 冷启动失败频率高到值得重试。

**安全边界（不管你换什么图表方案都要做）：** 工具返回的图片 URL 不能直接给前端。必须 ①只允许该 server 声明的 host（SSRF 边界）②只允许 https ③下载后按 magic bytes 校验真实类型（PNG/JPEG/WebP）④限制大小（我们设 5MB）⑤存本地后**用自己的域名重新提供**。

> **迁移建议：** Streamlit 里直接用 Plotly / Altair 更顺，且能拿到交互能力（MCP 方案只能出静态 PNG）。但**「让模型选图表类型并生成配置」这个模式建议保留** —— 把「可用图表类型 + 各自的参数 schema」作为工具定义交给模型即可，不需要 MCP。

---

## 9. Agent Skills：把业务规则外置成文档

用 deepagents 的 `SkillsMiddleware`，不要自己实现发现和渐进披露。

- 一个技能 = 一个文件夹 + 一份带 YAML frontmatter 的 `SKILL.md`（`name` + `description`）。
- **`description` 决定模型什么时候用它**，要写触发场景而不是功能介绍。反面例子：「供应商评分卡」；正面例子：「当问题比较供应商、或询问哪家最好/最差/需要复核时使用」。
- 技能正文写**「房规」**：必须一起给出哪几列、按什么顺序、什么情况下要加警告。

我们的实际例子（`skill/vendor-scorecard/SKILL.md`）：

> 排名供应商时固定输出 `test_count`、`total_cost_usd`、`pass_rate_pct`、`avg_turnaround_days` 四列；样本量 < 20 的供应商仍然展示但标注为低样本；不能只凭成本说某家「最好」，必须说明结论基于哪个维度；供应商低于合同质量目标时必须明确指出。

这类规则写进 Prompt 会把 Prompt 撑爆且难维护，做成技能则按需加载。

**两个安全注意点：**

- 读技能正文需要文件工具，所以文件系统中间件要一起挂载，并且**根目录锁死在技能目录**。
- **`execute` 工具默认开启且没有沙箱。** 锁根目录只约束了文件路径，约束不了 shell 命令能碰到什么。要提供开关允许关掉它。
- **没有技能时不要挂载文件系统工具** —— 否则等于白送模型一套文件访问能力。

---

## 10. 高危陷阱清单（我们真实踩过的）

| 陷阱 | 症状 | 对策 |
| --- | --- | --- |
| 参数嵌套层级错误 | 开关「看起来没生效」，无任何报错 | 用非法值探测服务端是否真的校验了该字段 |
| 未知配置值被静默忽略 | 配置看起来生效，实际没有 | 严格枚举解析，非法值直接抛错并列出合法值 |
| 前端产物没重新构建 | 改了代码但页面行为不变，浏览器缓存加剧误判 | 生产模式服务的是构建产物；开发用 dev 模式；排查时先硬刷新 |
| 数据库单写者锁 | 跑着服务时跑测试直接炸 | 先停服务；SQLite 开 WAL |
| Schema 多处定义漂移 | 下拉框出现不存在的字段 | 见 §4.2，注册表从物理表 seed |
| Agent 化后守卫失效 | 安全检查还在，但可以被绕过 | 守卫挪进工具内部 |
| MCP 冷启动抖动被缓存 | 整个进程周期内图表功能全废 | 失败不缓存 + 重试 |
| 代理环境变量劫持模型流量 | 隐蔽 | 显式 `trust_env=False` |
| 无技能时挂载文件工具 | 模型获得多余的文件访问权限 | 条件挂载 |
| 比率用 0-1 返回 | 图表和文案量纲错误 | Prompt 里明确禁止，注册表里用 `result_format` 固定 |

**还有一条原则性的：失败必须显式暴露，绝不用假数据兜底。** 模型调用失败、查询失败、图表失败都要如实呈现给用户。Demo 里塞假数据是最容易做、也是最毁掉信任的选择。图表失败可以降级（分析照常给出），但不能假装成功。

---

## 11. 数据模型与 Demo 数据

### 11.1 表结构（星型）

```
fact_test_results        粒度 = 一次检测
  test_result_id, vendor, project, material, test_name,
  submitted_date, completed_date, status, result(PASS/FAIL),
  cost_usd, turnaround_days

dim_vendor_contracts     vendor(PK), contract_tier, region,
                         contracted_cost_usd, sla_days,
                         quality_target_pct, effective_date
dim_project_budgets      project(PK), owner, priority,
                         approved_budget_usd, start_date, end_date
dim_material_standards   material(PK), material_family,
                         target_failure_pct, max_avg_cost_usd, risk_tier

vw_laboratory_analysis   由已发布的 join 规则自动生成
```

> **量纲不一致是故意的**：`quality_target_pct` 存 0-1 小数，`target_failure_pct` 存 0-100 标度。这是真实世界的常态，也正是 registry 里 `description` 字段存在的理由 —— 靠字段说明让模型正确处理，而不是靠命名约定。

### 11.2 Demo 数据设计（值得照抄的方法论）

**用固定随机种子生成 1000 行确定性数据**，保证每次演示结果一致、可写断言。

关键是**在数据里植入可被发现的「故事」**，否则分析出来全是均匀噪声，看不出产品价值：

| 植入的故事 | 手法 |
| --- | --- |
| 某供应商周转明显偏慢 | 该供应商周转天数均值 12 天，其他 6-8 天 |
| 某供应商 6 月起成本异常上涨 | 6 月后基础成本 × 1.24 |
| 某材料 × 某测试组合失败率异常 | 该组合失败率 29%，全局基线 9% |
| 供应商单价分层 | 基础成本 82 / 98 / 106 / 112 分档，再叠加高斯噪声 |

同时准备 **6 个异构原始文件**覆盖真实的接入难题：

- 标准 CSV（字段名规范）
- XLSX 且**字段名是别名**（`specimen_id` / `analysis_name` / `invoice_amt`），值也需要映射（`OK/NG` → `PASS/FAIL`）
- **非结构化文本**（Slack 消息流），需要实体抽取
- 三份维度参考数据（合同 / 预算 / 材料标准）

> 你要做多模态，就在这里补一份**扫描件 PDF** 和一张**报告截图**，故事植入方式照旧。

### 11.3 验收问题集

`demo_data/test-queries.md` 里有 13 个测试问题和参考 SQL，可直接复用为验收清单。分四组：

1. **基础聚合**（3 题）：供应商综合表现、月度成本趋势、材料失败率
2. **合同联表**（3 题）：实际单价 vs 合同单价、SLA 履约、质量目标达成
3. **预算联表**（3 题）：预算消耗率、超预算项目、高优先级项目质量风险
4. **边界行为**（3 题，**最容易被忽略但最能体现完成度**）：
   - 「你好，你能做什么？」→ 只给能力说明，**不执行 SQL、不渲染空表空图占位**
   - 「帮我看看哪个最好」→ **追问澄清**（成本？通过率？周转？），不执行 SQL
   - `DROP TABLE fact_test_results;` → 守卫拒绝并说明原因

第 4 组是我们发现最能区分「能跑」和「能用」的部分。

---

## 12. 建议实施顺序

按依赖关系排，每步都可独立验证：

1. **数据层 + Demo 数据**：建表、确定性种子数据、6 份原始文件。此时用手写 SQL 验证 §11.3 的 10 个分析问题都能算出来 —— **模型接进来之前先确认数据本身撑得住这些问题**。
2. **Schema Registry**：从物理表 seed，产出 Prompt 上下文。验证：注册表列出的字段和物理表完全一致。
3. **SQL Guard**：先写拒绝用例再写实现。验证：DDL/DML、多语句、未授权表、外部读取函数全部拒绝；合法查询自动补 LIMIT。
4. **Provider Dialect**：接一家模型跑通，非法配置能报错。
5. **Analysis Agent**：单工具 + 守卫 + 事件流。此时用命令行跑通，不碰 UI。
6. **语义层**：join 规则校验与视图发布，接进 Prompt。
7. **UI**：三个页面。Analyze 页优先做事件流渲染。
8. **接入流程**：上传 / Profile / 映射 / commit。
9. **会话持久化**：事件落库 + 重放。
10. **图表**：让模型选图表类型。
11. **Agent Skills**：把业务房规外置。

**第 1、3、5 步是地基**，其余都可以并行或延后。第 3 步之前不要让模型碰数据库。

---

## 13. 附：本仓库可直接参考的文件

| 关注点 | 文件 |
| --- | --- |
| Schema Registry | `backend/app/registry.py` |
| 语义层与视图发布 | `backend/app/semantic.py` |
| SQL 守卫 | `backend/app/sql_guard.py` |
| Agent 与受保护工具 | `backend/app/agent.py` |
| Provider dialect | `backend/app/model_runtime.py` |
| 会话记忆 | `backend/app/conversation.py` |
| MCP 缓存与 SSRF 边界 | `backend/app/mcp_chart.py` |
| Agent Skills 挂载 | `backend/app/skills.py` |
| 流式 / 持久化 / 重放 | `backend/app/a2ui.py` |
| Demo 数据与故事植入 | `backend/app/demo_data.py` |
| 验收问题集 | `demo_data/test-queries.md` |
| 技能示例 | `skill/vendor-scorecard/SKILL.md` |
| 测试（35 个，含大量边界用例） | `backend/tests/test_backend.py` |

原始产品设计文档 `docs/Mini Hackathon.md`（中文）包含完整的产品背景、用户故事和交互规范，但**其中的技术选型结论有多处已被代码推翻**，仅作产品需求参考。
