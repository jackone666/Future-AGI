# Backend 注释翻译与中文学习文档 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 guardrails、agentic_eval、simulate、tracer 相关后端代码中的有学习价值英文注释翻译为中文，并生成中文学习文档。

**Architecture:** 先扫描目标目录的注释分布，筛选解释性注释进行中文化；再按模块阅读 README、核心代码和关键模型/服务，输出面向源码学习的主题文档。保持代码行为不变，只改注释和 Markdown 文档。

**Tech Stack:** Go、Python、Django、Markdown、shell/rg。

## Global Constraints

- 默认使用简体中文。
- 不翻译前端 JSX、demo dataset、依赖、构建产物、lock 文件。
- 不翻译整段废弃代码注释，除非注释本身解释关键逻辑。
- 不改变运行逻辑、API、配置键、测试断言和字符串常量。
- 当前目录不是 Git 仓库，不能提交。

---

### Task 1: 扫描目标模块

**Files:**
- Inspect: `agentcc-gateway/internal/guardrails/**`
- Inspect: `futureagi/agentcc/**guardrail*`
- Inspect: `futureagi/agentic_eval/**`
- Inspect: `futureagi/simulate/**`
- Inspect: `futureagi/tracer/**`

- [ ] 用 `rg` 统计英文注释和关键文件。
- [ ] 排除迁移、测试快照、JSON 数据和无价值旧代码注释。

### Task 2: 翻译后端代码注释

**Files:**
- Modify: 目标模块内 Go/Python 后端源码文件。

- [ ] 翻译解释架构、算法、数据流、边界条件、配置含义的英文注释。
- [ ] 保留技术名词、配置键、协议字段、OpenTelemetry 属性名和函数名。
- [ ] 确认语法不变。

### Task 3: 生成中文学习文档

**Files:**
- Create: `docs/zh/backend-learning-map.md`
- Create: `docs/zh/guardrails.md`
- Create: `docs/zh/agentic-eval.md`
- Create: `docs/zh/simulate.md`
- Create: `docs/zh/tracer.md`

- [ ] 每篇包含模块定位、核心目录、关键类/函数、数据流、扩展点、阅读顺序。
- [ ] 用源码路径引用关键文件。

### Task 4: 验证

- [ ] 运行语法/格式轻量检查。
- [ ] 用 `rg` 抽查目标模块英文注释残留。
- [ ] 汇总修改文件和未覆盖原因。

