# cli-agent Architecture

Current implementation status: M15 explicit Runtime resource ownership is
implemented. The next milestone is M14, which will replace direct MCP worker
connections with bounded IPC bindings.

```mermaid
flowchart TB
    subgraph HOST["Host 层 · Reference CLI"]
        direction LR

        CLI["cli.py<br/>argparse 入口<br/>退出码"]
        CONFIG["config.py<br/>CliConfig<br/>provider 构建"]
        RUNNER["runner.py<br/>one-shot / 交互循环<br/>Runtime 生命周期"]
        PRES["presentation.py<br/>stdout / stderr<br/>事件与诊断渲染"]
        APPROVER["Terminal Approver<br/>ASK → allow once / deny"]

        CLI --> CONFIG --> RUNNER
        RUNNER --> PRES
    end

    subgraph RUNTIME["Agent Runtime · cli_agent.runtime"]
        direction TB

        RT["runtime.py<br/>AgentRuntime<br/>open / close · Session registry<br/>owns one resource aggregate"]
        RES["_resources.py<br/>_RuntimeResources<br/>Workspace-lifetime 所有权边界"]
        DIAG["diagnostic.py<br/>RuntimeDiagnostic<br/>结构化、非阻塞 Host 通知"]

        subgraph MODEL_PLANE["Model / Conversation seam"]
            direction LR

            LOOP["_agent_loop.py<br/>AgentLoop<br/>会话历史 · 对话循环<br/>Tool Call 分派"]
            MODEL["model.py<br/>消息 / 事件<br/>ModelProvider 协议"]
            SYSCALLS["_syscalls.py<br/>固定模型面<br/>exec · output · kill"]
            SYSMSG["_system_message.py<br/>Runtime system message<br/>Tools + Skills 紧凑广告"]
            PROVIDER["providers/<br/>OpenAI-compatible / Scripted<br/>httpx 流式 Provider"]

            LOOP -->|generate request| PROVIDER
            PROVIDER -->|ModelEvent 流| LOOP
            MODEL -.->|定义协议| PROVIDER
            SYSCALLS -->|默认 syscall schemas| MODEL
            SYSMSG -->|注入初始历史| LOOP
        end

        subgraph CAP["Capability 域 · Runtime-open / shared"]
            direction TB

            WS["workspace.py<br/>.workspace 引导<br/>dotenv 环境快照"]
            VIEW["view.py<br/>Capability View<br/>tools / skills / library / _mcp<br/>symlink · copy-up · whiteout"]
            PARSER["command_parser.py<br/>POSIX shlex<br/>语法与重定向事实"]

            subgraph TOOLS["tools/"]
                direction LR

                T_FACTS["facts.py<br/>ToolEntry · ToolCommand"]
                T_CAT["catalog.py<br/>可信 Tool Catalog<br/>生成 tools/index.md"]
                T_GRAM["grammar.py<br/>tools list / info / run<br/>命令分类"]
                T_ENV["environment.py<br/>Workspace-private venv<br/>uv pip sync + mcp base dep"]
                T_WORKER["worker.py<br/>stdlib JSON worker"]
            end

            subgraph SKILLS["skills/ · M8"]
                direction LR

                S_FACTS["facts.py<br/>SkillEntry"]
                S_PARSE["parser.py<br/>strictyaml frontmatter<br/>结构校验"]
                S_CAT["catalog.py<br/>Skill Catalog<br/>生成 skills/index.md"]
            end

            subgraph MCP["mcp/ · M13"]
                direction LR

                M_FACTS["facts.py<br/>MCPServerConfig<br/>config.json 校验"]
                M_CAT["catalog.py<br/>full-rebuild reconcile<br/>stdio / http → list_tools<br/>失败重试与诊断"]
                M_STUB[".workspace/tools/mcp_*.py<br/>生成的 MCP Tool stubs"]
            end

            WS --> VIEW
            VIEW --> PARSER

            T_FACTS --> T_CAT
            VIEW --> T_CAT
            T_CAT --> T_GRAM
            VIEW --> T_ENV
            T_ENV --> T_WORKER

            S_FACTS --> S_CAT
            VIEW --> S_PARSE --> S_CAT

            M_FACTS --> M_CAT
            VIEW --> M_CAT
            M_CAT -->|生成 / 覆盖| M_STUB
            M_STUB -->|进入普通 Tool Catalog| T_CAT
        end

        subgraph SESSION["每个活跃 Session · lazy-created"]
            direction TB

            SESSION_NODE["Session<br/>恰好拥有一个 AgentLoop<br/>和一个 EnvironmentKernel"]

            subgraph ENV["Environment 域 · Session-scoped"]
                direction TB

                KERNEL["kernel.py<br/>EnvironmentKernel<br/>dispatch_batch · state · close"]

                PROTO["protocol.py<br/>exec / output / kill<br/>参数校验与 Execution snapshot"]
                POLICY["policy.py<br/>ALLOW / DENY / ASK<br/>Host-owned ExecutablePolicy"]
                DECISION["ExecutionDecision<br/>最终、不可变、allow-only 授权边界"]
                ROUTER["routing.py<br/>CUSTOM / SHELL / TOOL<br/>lane + scheduling class"]
                SCHED["scheduler.py<br/>有界准入<br/>顺序 admission + 并行批次"]
                SUPV["supervisor.py<br/>执行监督<br/>等待 · 取消 · 清理"]
                EXEC["execution.py<br/>_ExecutionState<br/>bounded output · Cursor · lifecycle"]
                CMDS["commands.py<br/>Runtime Custom handlers<br/>cd / export"]

                subgraph DRIVERS["drivers/"]
                    direction LR

                    DRBASE["base.py<br/>ExecutionDriver contract"]
                    SHELL_DRV["shell.py<br/>Shell Driver"]
                    TOOL_DRV["tool.py<br/>Tool Driver"]
                    PROC["executions.py<br/>process group<br/>SIGTERM → KILL"]
                end

                KERNEL --> PROTO --> PARSER --> POLICY
                POLICY -->|ALLOW| DECISION
                POLICY -->|DENY| DENIED["policy_denied<br/>不创建 Execution / 不占用队列"]
                POLICY -->|ASK| APPROVER
                APPROVER -->|allow once| DECISION
                APPROVER -->|deny / timeout / fail closed| DENIED

                DECISION --> ROUTER
                ROUTER -.->|查找 Custom registry| CMDS
                ROUTER -->|CUSTOM / SHELL / TOOL| SCHED
                SCHED --> SUPV --> DRBASE
                DRBASE --> SHELL_DRV
                DRBASE --> TOOL_DRV
                SHELL_DRV --> PROC
                SUPV --> EXEC
                TOOL_DRV -->|启动 JSON worker| T_WORKER
            end

            SESSION_NODE --> LOOP
            SESSION_NODE --> KERNEL
            LOOP -->|dispatch batch ToolCall| KERNEL
            KERNEL -->|有序 ToolResult| LOOP
        end

        RT -->|owns one aggregate| RES
        RES -->|Runtime-open 一次| WS
        RES --> VIEW
        RES --> T_CAT
        RES --> T_ENV
        RES --> S_CAT
        RT -->|首次 run_turn 创建| SESSION_NODE
        SESSION_NODE -.->|borrows explicit objects| RES
        M_CAT -->|发现失败 / 配置无效| DIAG
        DIAG -->|on_diagnostic callback| PRES
    end

    CONFIG -->|build_provider| PROVIDER
    RUNNER -->|provider + approver + callbacks| RT

    classDef host fill:#e8f3ff,stroke:#3978b9,stroke-width:1.5px
    classDef runtime fill:#fff4cf,stroke:#b88a00,stroke-width:1.5px
    classDef capability fill:#e9f7e9,stroke:#4b9b50,stroke-width:1.5px
    classDef skill fill:#f1e8ff,stroke:#7950b8,stroke-width:1.5px
    classDef mcp fill:#ffe9f2,stroke:#c45183,stroke-width:1.5px
    classDef environment fill:#ffe9e7,stroke:#c2554d,stroke-width:1.5px
    classDef adapter fill:#fff0dc,stroke:#bd7a22,stroke-width:1.5px
    classDef boundary fill:#f2f4f7,stroke:#667085,stroke-width:2px
    classDef artifact fill:#f7f7f7,stroke:#8c8c8c,stroke-dasharray:5 3

    class CLI,CONFIG,RUNNER,PRES,APPROVER host
    class RT,DIAG,SYSMSG,SYSCALLS,MODEL runtime
    class PROVIDER adapter
    class WS,VIEW,PARSER,T_FACTS,T_CAT,T_GRAM,T_ENV,T_WORKER capability
    class S_FACTS,S_PARSE,S_CAT skill
    class M_FACTS,M_CAT mcp
    class M_STUB artifact
    class SESSION_NODE,RES boundary
    class KERNEL,PROTO,POLICY,DECISION,DENIED,ROUTER,SCHED,SUPV,EXEC,CMDS,DRBASE,SHELL_DRV,TOOL_DRV,PROC environment
```
