# Blanket Life in One Agent

> 我们认为一款成功的 Agent 软件，必定具备以下三个特质：
>
> **1. Memory 与个性化**  
> 模型能力的进步日新月异，每一家都曾短暂霸榜。但无论是 Codex 还是 Claude Code，都很难真正做到用户量领先——原因是它们的目标用户高强度关注模型能力，而平台切换对用户几乎没有门槛。当某一家模型领先时，用户便可以快速迁移到另一个平台。真正能够留住用户的，是系统对用户长期习惯、偏好和上下文的持续积累。
>
> **2. GUI-first**  
> GUI 大幅降低了用户使用门槛。以 Coding Agent 为例，Codex 等平台在推出对应 App 后，用户量较只有 CLI 版本时激增。对普通用户而言，可视化的交互界面远比命令行更接近自然使用方式。
>
> **3. 尊重 Human-in-the-loop**  
> 本地生活推荐的准确性受多种复杂因素影响：天气、时间、用户喜好、当下心境等。目前大部分 Agent 直接代替用户做决定，仅把 Human-in-the-loop 放在敏感权限（如支付授权）上，这本身扼杀了用户探索的广度和兴趣。好的 Agent 应该在关键决策点邀请用户参与，而不是替用户关闭可能性。
>
> 出于此，我们打造了 Blanket。

## 架构图

```mermaid
flowchart TB
    subgraph Client["客户端 / 前端"]
        C1["Web UI / SPA"]
        C2["浏览器插件 / WS"]
    end

    subgraph Entry["入口层"]
        S["server.py\n兼容启动器"]
        U["Uvicorn\nASGI Server"]
    end

    subgraph FastAPI["FastAPI 新外壳 — app/main.py"]
        F1["/api/v1/health"]
        F2["/api/v1/longcat/chat\nLLM 代理"]
        F3["/api/v1/agent/search/stream\n搜索 Agent SSE"]
        F4["/api/v1/agent/native-chat\n原生 Agent 对话"]
        F5["/api/v1/agent/native-chat/resume\n断点续聊"]
        FS["静态文件\n/dist /static /icons"]
    end

    subgraph Compat["兼容适配层 — app/compat/"]
        CF["flaskish.py\n模拟 Flask API\nFlask / Blueprint / Sock /\nrequest / jsonify / Response"]
        CA["asgi.py\n路由转换 + 请求适配\nFlask Rule → Starlette Route\nWS 同步 → 异步桥接"]
    end

    subgraph Legacy["业务核心 — app/services/legacy_server.py"]
        L1["文件转换\nMarkItDown"]
        L2["群聊数据\n.longcat_groups.json"]
        L3["记忆系统\nSQLite + FTS5 + LanceDB"]
        L4["浏览器接管\nPreview / Harness / Bridge"]
        L5["搜索与旅行\nParallel Search / Travel Harness"]
        L6["MCP 客户端\nStdio 工具调用"]
        L7["原生 Agent 循环\nTool Calling / Checkpoints"]
    end

    subgraph Runtime["运行时层 — app/runtime/"]
        R1["agent_runtime.py\nSessionRuntime / LaneController\nfast · io · compute · plan · subagent"]
        R2["wasm_sandbox.py\nWasmPythonSandbox\nwasmtime + WASI"]
    end

    subgraph Agents["Agent 层 — app/services/"]
        A1["miroflow_search_agent.py\nMiroFlow Search SubAgent"]
    end

    subgraph External["外部依赖"]
        E1["LLM API\nLongCat Proxy"]
        E2["Playwright / browser-use"]
        E3["MCP Servers"]
        E4["向量模型\nBAAI/bge-small-zh-v1.5"]
    end

    C1 --> S
    C2 --> S
    S --> U
    U --> FastAPI

    F1 --> Legacy
    F2 --> L7
    F3 --> A1
    F4 --> L7
    F5 --> L7

    FastAPI -. "legacy_routes()\nlegacy_websocket_routes()" .-> Compat
    Compat --> Legacy

    Legacy --> Runtime
    L7 --> R1
    L7 --> R2
    A1 --> R1

    L4 --> E2
    L6 --> E3
    L3 --> E4
    F2 --> E1
    L7 --> E1
```

## 🟢 1. 记忆系统

对 Blanket 来说，记忆不是简单的"记住用户说过什么"，而是让用户感到被理解、被持续服务的关键资产。我们把它设计成三层递进结构： raw 素材层、structured 记忆层、semantic 表达层，并在每一层都加入了对相关性、多样性和隐私安全的保护。

### _RAW 素材层：保留可读取的原始上下文_

RAW 层保存每次对话的原始消息。在上下文窗口允许时，模型可以直接读取这些原消息，而不是只能看到被压缩后的摘要。这让 Agent 能够捕捉到语气、犹豫、临时修改等细腻信息。

当对话变长、接近上下文上限时，系统会启动分层保护策略：

- **优先保护头尾**：开头的 system 提示和前几条关键消息、以及最近的几轮对话会被完整保留
- **中间内容智能压缩**：较早的中间轮次会先被 LLM summarize 成一段简洁的「前文提要」，而不是简单截断丢弃
- **工具结果特殊处理**：旧的工具输出会先被替换成一句话摘要；重复的工具输出会被标记为「与近期内容相同」
- **保持工具对完整性**：压缩边界会避开 assistant 的 tool_calls 和对应的 tool 结果之间，防止出现「模型说调用了工具，但找不到返回结果」的断层。如果确实被截断，也会插入占位说明，而不是让模型困惑

结果是：长对话的尾部细节仍然完整可用，中间历史以摘要形式保留核心信息，系统不会因为长度而突然丢失上下文。

### _Structured 记忆层：从混沌中提炼事实_

系统会自动从对话中识别出值得保留的信息，并把它们整理成结构化的记忆条目：

- **你是谁** —— 名字、身份、职业等画像信息
- **你喜欢什么** —— 饮食偏好、沟通风格、关注领域
- **你讨厌什么 / 你的约束** —— 忌口、过敏、明确的边界
- **你正在做的事** —— 长期项目、待办目标、计划中的旅行
- **杂项笔记** —— 其他稳定但暂时无法归类的信息

这些记忆不是简单的原文摘抄，而是经过 LLM 理解和格式化的"事实"，带有置信度和来源标记。用户可以显式确认一条记忆，也可以让系统自动推断。

### _Semantic 表达层：让记忆真正可用_

结构化记忆要进一步变成模型好读、检索好找的形式：

- **原子事实**把每条记忆压缩成一句精确的陈述
- **叙事片段**把同一主题的多条记忆串成一段小 story
- **前瞻事项**识别出计划、 deadline、提醒等未来需要关注的内容
- **场景标签**自动把记忆归类到"旅行与本地生活""研究与论文""代码与工程"等主题桶

最终，它们会被统一成检索单元，供后续的混合搜索使用。

### _匹配与注入：只在对的时间出现对的记忆_

当用户发起一次新的 Agent 请求时，Blanket 会同时做几件事：

1. **理解意图** —— 把当前问题或近期对话变成检索查询
2. **多路召回** —— 同时走全文搜索、向量相似度、词袋匹配，避免任何一路漏掉关键记忆
3. **多样性筛选** —— 用 MMR 算法保证返回的记忆不会全部堆在同一个主题上，而是尽量覆盖不同维度
4. **分区配额** —— 沟通偏好、约束、项目、事实等类别各有进入 prompt 的上限，防止某一类记忆刷屏
5. **动态拼接** —— 把精选记忆以代码块形式拼到当前 user 消息末尾，模型在推理时自然可见

我们给模型定的规则很简单：**这些记忆是背景参考，如果和当前对话冲突，以当前对话为准。**

### _冲突与进化：记忆会自己长大，也会自己纠错_

人的偏好是会变的。今天说"回复简洁一点"，下周可能又说"这次帮我展开详细讲讲"。Blanket 会检测这种冲突：

- 如果新记忆是用户明确确认的，它会取代旧记忆
- 如果新旧记忆互相矛盾但证据都不够强，它们会被标记为"待决冲突"，等待更多证据
- 系统会周期性地整理记忆库，合并重复项、清理过期候选、刷新用户画像

这样，Blanket 对你的理解会随时间越来越准，而不是被早期的一两条片面信息锁死。

### _隐私安全：你的位置、密码、密钥不会被偷渡进上下文_

记忆注入前会经过安全检查：

- 精确经纬度不会作为普通记忆被召回，只在导航、 weather 等明确需要位置的工具中被单独解析
- API Key、密码、Token 等敏感内容会被识别并过滤
- Prompt Injection 攻击模式会被拦截
- 用户可以随时查看、编辑、删除自己的记忆

## 🟢 2. Agent 框架

核心原生 Agent 循环 `_native_create_agent_run` 实现了以下机制：

- **动态预算系统**（`_NativeIterationBudget`）：wall 时间上限、token 预算、软/硬轮次上限、卡死检测与自动降级
- **多轮工具调用**：模型自主决策 → 解析 tool calls → 并行/流式执行 → 结果回注 → 下一轮推理
- **DAG 计划拦截器**：当存在 `plan_task` 创建的活跃计划时，按 `depends_on` 依赖关系调度步骤执行
- **会话挂起与恢复**：`ask_user_clarification` 等工具可挂起会话；支持 checkpoint 断点恢复与 `native-chat/resume` 续跑
- **工具执行隔离**：根据工具类型自动选择进程隔离（process pool）、WASM 沙箱或同进程执行
- **流式预执行**：安全工具可在模型流式输出期间提前启动，减少等待延迟
- **工具重复策略**：自动检测低质量结果并决定重试、合并或丢弃
- **Lane 调度集成**：工具执行落入 `fast / io / compute / plan / subagent` 五车道，由 `agent_runtime.py` 统一管控并发与资源

## 🟢 3. 原生注册工具

| 类别 | 工具名 | 说明 |
|---|---|---|
| Agent 编排 | `spawn_agent` | 启动独立子 agent，支持后台运行、fork、WASM 隔离 |
| | `run_wasm_python` | 在 WASI WebAssembly 沙箱中执行纯 Python |
| 任务管理 | `task_create` / `task_get` / `task_list` / `task_stop` / `task_output` | 创建、查询、停止、读取 agent task |
| 团队协作 | `create_team` / `send_to_agent` | 创建 agent team 与 mailbox 消息 |
| 工具发现 | `tool_search` | 按需检索并动态加载工具 schema |
| 用户交互 | `ask_user_clarification` | 发起问卷并挂起会话等待用户确认 |
| 计划执行 | `plan_task` | 创建/更新 DAG 执行计划，支持 depends_on 依赖 |
| 信息查询 | `search_web` | 联网搜索（由搜索子代理完成抓取与验证） |
| | `search_memory` | 读取用户长期记忆与会话摘要 |
| | `resolve_location_anchor` | 解析用户保存的位置锚点（家/公司等） |
| 生活服务 | `search_merchants` / `select_merchant_cards` | 商户候选池查询与卡片筛选 |
| | `search_movies` | 电影卡片查询 |
| | `search_papers` | 学术论文查询（arxiv / pubmed 等） |
| | `get_weather` | 实时天气与预报 |
| | `get_time` | 当前时间/日期 |
| | `search_train_tickets` | 12306 火车票/高铁查询 |
| | `plan_navigation` | 路线规划与导航 |
| 展示 | `display_cards` | 选择并展示已生成的 artifact 卡片 |

## 分层说明

| 层级 | 关键文件 | 职责 |
|---|---|---|
| 入口层 | `server.py` | 读取环境变量，启动 Uvicorn |
| FastAPI 外壳 | `app/main.py` | 新接口实现 + 挂载遗留路由 + 静态文件 |
| 兼容层 | `app/compat/flaskish.py`<br>`app/compat/asgi.py` | 迁移兼容flask代码 |
| 业务核心 | `app/services/legacy_server.py` | 记忆、文件转换、浏览器、Agent、搜索、旅行等业务逻辑 |
| 运行时 | `app/runtime/agent_runtime.py`<br>`app/runtime/wasm_sandbox.py` | 会话级 Lane/Resource 调度、WASM 沙箱执行 |
