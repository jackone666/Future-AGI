# Guardrails 后端源码学习手册

Guardrails 是 Future AGI 的实时安全防护层。它可以在模型调用前检查输入，也可以在模型返回后检查输出，并把命中的规则写入网关上下文、日志和 trace。学习这个模块时，要把它理解成两层：Go 网关里的实时执行层，以及 Python 后端里的控制平面配置层。

## 1. 模块定位

Guardrails 解决的是“请求已经进入生产链路时，如何实时保护用户和系统”的问题。它和离线评测不同：离线评测可以慢一些、可以批量跑；Guardrail 必须在请求链路里做出 block/warn/log 决策。

核心职责：

- 在请求前检查 prompt、用户输入、metadata。
- 在响应后检查模型输出、工具结果、流式输出。
- 支持静态本地配置，也支持 org 级动态配置。
- 支持内置 scanner 和外部 vendor adapter。
- 把命中结果写入 `RequestContext`，交给 logging/tracer 后续记录。

## 2. 源码入口地图

| 代码 | 作用 |
|---|---|
| [guardrail.go](../../agentcc-gateway/internal/guardrails/guardrail.go#L48) | 定义 `Guardrail` 接口、阶段、动作、检查输入和输出。 |
| [engine.go](../../agentcc-gateway/internal/guardrails/engine.go#L35) | 本地静态 guardrail 执行引擎，负责超时、阈值、同步/异步和 fail-open。 |
| [plugin.go](../../agentcc-gateway/internal/guardrails/plugin.go#L60) | 把 guardrail 接入 gateway pipeline，处理 pre/post 阶段。 |
| [plugin.go runOrgGuardrails](../../agentcc-gateway/internal/guardrails/plugin.go#L125) | 执行 org tenant config 中的动态 guardrail。 |
| [plugin.go getGuardrail](../../agentcc-gateway/internal/guardrails/plugin.go#L240) | 按名称解析静态或动态 guardrail 实现。 |
| [plugin.go resolvePolicy](../../agentcc-gateway/internal/guardrails/plugin.go#L380) | 合并 org/key/request 级策略覆盖。 |
| [stream_checker.go](../../agentcc-gateway/internal/guardrails/stream_checker.go#L15) | 流式响应检查器，按字符间隔检查累积输出。 |
| [guardrail_policy.py](../../futureagi/agentcc/models/guardrail_policy.py) | Python 后端的 guardrail policy 模型。 |
| [guardrail_sync.py](../../futureagi/agentcc/services/guardrail_sync.py) | 将控制平面策略同步到 gateway tenant config。 |
| [guardrail_config.py](../../futureagi/agentcc/views/guardrail_config.py) | 给配置页面提供 guardrail 参考数据和校验能力。 |

## 3. 核心抽象

最重要的接口是 [Guardrail](../../agentcc-gateway/internal/guardrails/guardrail.go#L48)。每个 guardrail 都必须回答几个问题：

- `Name()`：规则名称，例如 `pii-detection`、`prompt-injection`。
- `Stage()`：运行在 pre 还是 post。
- `Check(ctx, input)`：真正执行检查。

`CheckInput` 是输入容器，里面有 request、response 和 metadata。pre 阶段通常只看 request，post 阶段通常会看 response。

`CheckResult` 是输出容器，关键字段是：

- `Pass`：是否通过。
- `Score`：风险分数或置信度。
- `Action`：命中后建议动作。
- `Message`：给日志/UI/阻断响应展示的解释。
- `Details`：scanner 自己扩展的结构化信息。

阈值判断集中在 [shouldTrigger](../../agentcc-gateway/internal/guardrails/guardrail.go#L75)。这点很重要：scanner 只负责给出结果，引擎负责把结果和配置阈值结合，最终判断是否命中。

## 4. 请求执行链路

一次普通非流式请求大致这样走：

1. Gateway 请求进入 pipeline。
2. [ProcessRequest](../../agentcc-gateway/internal/guardrails/plugin.go#L60) 解析策略，执行 pre-stage 静态规则。
3. 如果静态规则没有阻断，再执行 [runOrgGuardrails](../../agentcc-gateway/internal/guardrails/plugin.go#L125) 中 org config 的动态规则。
4. 如果某条规则 action 是 block，直接返回 guardrail error。
5. 请求继续到 provider。
6. provider 返回后进入 [ProcessResponse](../../agentcc-gateway/internal/guardrails/plugin.go#L85)，重复 post-stage 检查。
7. 命中的规则通过 [storeGuardrailResults](../../agentcc-gateway/internal/guardrails/plugin.go#L493) 写入 `RequestContext.GuardrailResults`。
8. logging 插件读取这些结构化结果，后续可以进入 trace。

可以把它记成：

```text
request
  -> pipeline
  -> guardrails pre
  -> provider
  -> guardrails post
  -> logging/tracer
```

## 5. 静态规则和动态规则

静态规则来自本地配置和启动时注册表。核心代码在 [NewEngine](../../agentcc-gateway/internal/guardrails/engine.go#L35)。它会根据配置把规则分成 pre/post 两组，并在 [run](../../agentcc-gateway/internal/guardrails/engine.go#L109) 里统一执行。

动态规则来自 org tenant config，主要服务托管模式。入口是 [runOrgGuardrails](../../agentcc-gateway/internal/guardrails/plugin.go#L125)。它会：

- 从 `rc.Metadata["org_id"]` 找组织。
- 从 tenant store 读取 org config。
- 跳过已经被静态引擎处理过的同名规则。
- 通过 [getGuardrail](../../agentcc-gateway/internal/guardrails/plugin.go#L240) 找实现。
- 如果是 FI Eval、webhook 这类动态配置，通过 dynamic factory 创建实例。
- 通过 [runGuardrailSafe](../../agentcc-gateway/internal/guardrails/plugin.go#L319) 加超时和 panic 恢复。

这个设计把“规则来自哪里”和“规则怎么执行”拆开了。新增 scanner 时不应该直接改 pipeline，而应该实现 `Guardrail` 接口并注册。

## 6. 策略优先级

策略覆盖逻辑在 [resolvePolicy](../../agentcc-gateway/internal/guardrails/plugin.go#L380)。优先级是：

```text
per-request header
  > per-key policy
  > per-org policy
  > global default
```

这里有一个安全边界：不是所有 API key 都允许请求级覆盖。只有 metadata 里明确允许 `key_allow_policy_override=true` 时，`X-Guardrail-Policy` 才能生效。否则用户可以通过 header 绕过防护，这是不允许的。

## 7. 流式响应检查

流式输出不能等完整响应结束才检查，否则用户可能已经看到了危险内容。流式检查器在 [StreamGuardrailChecker](../../agentcc-gateway/internal/guardrails/stream_checker.go#L15)。

核心方法是 [ProcessChunk](../../agentcc-gateway/internal/guardrails/stream_checker.go#L67)：

- 从 chunk 中抽取增量文本。
- 追加到累积 buffer。
- 当新增字符数超过配置间隔时，构造合成 response。
- 运行 post-stage guardrail。
- 如果 block，通知调用方停止继续流式输出。

最后 [Finish](../../agentcc-gateway/internal/guardrails/stream_checker.go#L86) 会对完整文本再跑一次检查，避免间隔检查漏掉跨 chunk 的风险。

## 8. 和 Tracer 的关系

Guardrail 自己不负责持久化 trace。它只是把命中信息写入 `RequestContext`：

- [applyMetadata](../../agentcc-gateway/internal/guardrails/plugin.go#L470) 写入轻量 metadata。
- [storeGuardrailResults](../../agentcc-gateway/internal/guardrails/plugin.go#L493) 写入结构化结果。

后续 logging/tracer 链路会把这些信息变成 trace 或 span 的属性。Tracer 侧已经有 `guardrail` observation type，见 [ObservationType](../../futureagi/tracer/models/observation_span.py#L34)。

## 9. 新增一个 Guardrail 怎么做

建议按这个顺序：

1. 在 `agentcc-gateway/internal/guardrails/<name>/` 新建 package。
2. 实现 [Guardrail 接口](../../agentcc-gateway/internal/guardrails/guardrail.go#L48)。
3. 明确 `Stage()` 是 pre 还是 post。
4. 在 `Check()` 中只做 scanner 自己的判断，不要直接改 pipeline。
5. 返回 `CheckResult`，让引擎通过 [shouldTrigger](../../agentcc-gateway/internal/guardrails/guardrail.go#L75) 统一应用阈值。
6. 在启动注册表里注册名称。
7. 如果支持控制平面动态配置，接入 dynamic factory。
8. 加测试覆盖通过、命中、阈值、block/warn/log、超时、panic/fail-open。

## 10. 阅读顺序

第一次读建议这样走：

1. [guardrail.go](../../agentcc-gateway/internal/guardrails/guardrail.go#L48)：先建立接口模型。
2. [engine.go NewEngine](../../agentcc-gateway/internal/guardrails/engine.go#L35)：理解静态配置如何变成执行规则。
3. [engine.go run](../../agentcc-gateway/internal/guardrails/engine.go#L109)：理解阈值、动作、同步/异步执行。
4. [plugin.go ProcessRequest](../../agentcc-gateway/internal/guardrails/plugin.go#L60)：看它如何进入 gateway pipeline。
5. [plugin.go runOrgGuardrails](../../agentcc-gateway/internal/guardrails/plugin.go#L125)：看动态 org config 如何执行。
6. [stream_checker.go](../../agentcc-gateway/internal/guardrails/stream_checker.go#L15)：理解流式输出检查。
7. 挑一个内置 scanner 看实现，例如 `blocklist`、`secrets` 或 `pii`。
8. 再看一个 external adapter，对比内置 scanner 和外部服务 scanner。

## 11. 常见排错方向

如果 guardrail 没命中：

- 先确认 stage 是否匹配，pre 规则不会在 response 阶段执行。
- 看配置里的 `enabled` 是否为 true。
- 看 `ConfidenceThreshold` 是否高于 scanner 返回的 `Score`。
- 确认同名规则是否被静态引擎处理后导致动态规则跳过。
- 确认 org_id 是否写入 `RequestContext.Metadata`。

如果命中了但 trace 看不到：

- 看 [storeGuardrailResults](../../agentcc-gateway/internal/guardrails/plugin.go#L493) 是否被调用。
- 看下游 logging 插件是否读取了 `RequestContext.GuardrailResults`。
- 看 Tracer 侧是否把 guardrail 属性映射到了 span 或 trace。

如果请求异常变慢：

- 看 [runGuardrailSafe](../../agentcc-gateway/internal/guardrails/plugin.go#L319) 的 timeout。
- 看外部 vendor adapter 是否阻塞。
- 看是否开启了过多同步规则。
- 对流式响应，检查 `StreamGuardrailChecker` 的触发间隔是否太小。
