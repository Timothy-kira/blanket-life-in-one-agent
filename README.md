# Blanket Life in One Agent

> LongCat Backend — FastAPI 外壳 + Flask 遗留业务核心的混合架构。

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

    subgraph Legacy["遗留业务核心 — app/services/legacy_server.py"]
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
        A1["miroflow_search_agent.py\nMiroFlow Search SubAgent\nLLM 工具调用解析"]
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

## 分层说明

| 层级 | 关键文件 | 职责 |
|---|---|---|
| 入口层 | `server.py` | 读取环境变量，启动 Uvicorn |
| FastAPI 外壳 | `app/main.py` | 新接口实现 + 挂载遗留路由 + 静态文件 |
| 兼容层 | `app/compat/flaskish.py`<br>`app/compat/asgi.py` | 让旧 Flask 代码无需重写即可跑在 FastAPI 上 |
| 遗留业务核心 | `app/services/legacy_server.py` | 3.3 万行业务逻辑：记忆、文件转换、浏览器、Agent、搜索、旅行 |
| 运行时 | `app/runtime/agent_runtime.py`<br>`app/runtime/wasm_sandbox.py` | 会话级 Lane/Resource 调度、WASM 沙箱执行 |
| Agent | `app/services/miroflow_search_agent.py` | 搜索子 Agent、LLM 工具调用解析 |
