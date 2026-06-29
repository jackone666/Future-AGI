package guardrails

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/futureagi/agentcc-gateway/internal/guardrails/policy"
	"github.com/futureagi/agentcc-gateway/internal/models"
	"github.com/futureagi/agentcc-gateway/internal/pipeline"
	"github.com/futureagi/agentcc-gateway/internal/tenant"
)

// DynamicFactory 根据名称和配置 map 创建 Guardrail。
// 当 org config 中的动态 guardrail（FI Eval、webhook 等）不在静态注册表中时使用。
type DynamicFactory func(name string, cfg map[string]interface{}) Guardrail

// GuardrailPlugin 将 guardrail 引擎包装成 pipeline 插件。
type GuardrailPlugin struct {
	engine         *Engine
	registry       map[string]Guardrail // 所有已注册的 guardrail 实现
	dynamicFactory DynamicFactory       // 创建注册表中不存在的 guardrail
	policyStore    *policy.Store
	tenantStore    *tenant.Store // nil-safe；按 org 覆盖 guardrail 配置
	failOpen       bool
	defaultTimeout time.Duration

	// 动态创建的 guardrail 缓存，例如带 org 级 API key 的 FI Eval。
	dynamicMu    sync.RWMutex
	dynamicCache map[string]map[string]Guardrail // org_id -> check_name -> guardrail
}

// NewPlugin 创建新的 guardrail pipeline 插件。
func NewPlugin(engine *Engine, registry map[string]Guardrail, dynamicFactory DynamicFactory, policyStore *policy.Store, tenantStore *tenant.Store) *GuardrailPlugin {
	timeout := 30 * time.Second
	failOpen := false
	if engine != nil {
		timeout = engine.DefaultTimeout()
		failOpen = engine.FailOpen()
	}
	return &GuardrailPlugin{
		engine:         engine,
		registry:       registry,
		dynamicFactory: dynamicFactory,
		policyStore:    policyStore,
		tenantStore:    tenantStore,
		failOpen:       failOpen,
		defaultTimeout: timeout,
		dynamicCache:   make(map[string]map[string]Guardrail),
	}
}

func (p *GuardrailPlugin) Name() string  { return "guardrails" }
func (p *GuardrailPlugin) Priority() int { return 50 } // 在 auth(20)、RBAC(30)、budget(40) 之后，在 cache(200) 之前。

// ProcessRequest 执行 pre 阶段 guardrail。
func (p *GuardrailPlugin) ProcessRequest(ctx context.Context, rc *models.RequestContext) pipeline.PluginResult {
	// 1. 执行静态引擎规则（自托管 / config.yaml 规则）。
	if p.engine != nil && p.engine.PreCount() > 0 {
		keyPolicy, reqPolicy, err := p.resolvePolicy(rc)
		if err != nil {
			return pipeline.ResultError(err)
		}

		input := &CheckInput{
			Request:  rc.Request,
			Metadata: rc.Metadata,
		}

		result := p.engine.RunPre(ctx, input, keyPolicy, reqPolicy)
		pluginResult := p.processResult(rc, result)
		if pluginResult.Action != pipeline.Continue {
			return pluginResult
		}
	}

	// 2. 执行 org config 中的动态 guardrail（托管模式）。
	return p.runOrgGuardrails(ctx, rc, StagePre)
}

// ProcessResponse 执行 post 阶段 guardrail。
func (p *GuardrailPlugin) ProcessResponse(ctx context.Context, rc *models.RequestContext) pipeline.PluginResult {
	// 1. 执行静态引擎规则。
	if p.engine != nil && p.engine.PostCount() > 0 {
		keyPolicy, reqPolicy, err := p.resolvePolicy(rc)
		if err != nil {
			return pipeline.ResultError(err)
		}

		input := &CheckInput{
			Request:  rc.Request,
			Response: rc.Response,
			Metadata: rc.Metadata,
		}

		result := p.engine.RunPost(ctx, input, keyPolicy, reqPolicy)

		if result.Blocked {
			rc.Response = nil
			rc.Flags.GuardrailTriggered = true
			if len(result.Triggered) > 0 {
				tg := result.Triggered[0]
				rc.Metadata["guardrail_name"] = tg.Name
				rc.Metadata["guardrail_action"] = "blocked"
			}
			storeGuardrailResults(rc, result)
			return pipeline.ResultError(models.ErrGuardrailBlocked(
				"content_blocked",
				p.buildBlockMessage(result),
			))
		}

		p.applyMetadata(rc, result)
	}

	// 2. 执行 org config 中的动态 guardrail（托管模式）。
	return p.runOrgGuardrails(ctx, rc, StagePost)
}

// runOrgGuardrails 执行 org 的 tenant config 中定义的 guardrail。
// 这支持托管模式：规则来自控制平面，而不是本地 config.yaml。
func (p *GuardrailPlugin) runOrgGuardrails(ctx context.Context, rc *models.RequestContext, stage Stage) pipeline.PluginResult {
	if p.tenantStore == nil {
		return pipeline.ResultContinue()
	}

	orgID := rc.Metadata["org_id"]
	if orgID == "" {
		return pipeline.ResultContinue()
	}

	orgCfg := p.tenantStore.Get(orgID)
	if orgCfg == nil || orgCfg.Guardrails == nil || len(orgCfg.Guardrails.Checks) == 0 {
		return pipeline.ResultContinue()
	}

	result := &PipelineResult{}

	for name, check := range orgCfg.Guardrails.Checks {
		if !check.Enabled {
			continue
		}

		// 如果静态引擎规则已经处理过，则跳过。
		if p.engine != nil && p.engine.HasRule(name) {
			continue
		}

		// 获取或创建 guardrail 实现。
		g := p.getGuardrail(orgID, name, check)
		if g == nil {
			slog.Debug("guardrail implementation not found",
				"name", name,
				"org_id", orgID,
			)
			continue
		}

		// 检查运行阶段是否匹配。
		if g.Stage() != stage {
			continue
		}

		// 确定超时时间。
		timeout := p.defaultTimeout
		if orgCfg.Guardrails.TimeoutMs > 0 {
			timeout = time.Duration(orgCfg.Guardrails.TimeoutMs) * time.Millisecond
		}

		// 构造检查输入。
		input := &CheckInput{
			Request:  rc.Request,
			Metadata: rc.Metadata,
		}
		if stage == StagePost {
			input.Response = rc.Response
		}

		// 带超时和 panic 恢复地运行。
		cr := p.runGuardrailSafe(ctx, g, input, timeout)
		if cr == nil {
			continue
		}

		// 应用命中阈值。
		threshold := check.ConfidenceThreshold
		triggered := shouldTrigger(cr, threshold)
		if !triggered {
			continue
		}

		action := parseAction(check.Action)

		tg := TriggeredGuardrail{
			Name:      name,
			Score:     cr.Score,
			Threshold: threshold,
			Action:    action,
			Message:   cr.Message,
		}
		result.Triggered = append(result.Triggered, tg)

		switch action {
		case ActionBlock:
			result.Blocked = true
			storeGuardrailResults(rc, result)
			rc.Flags.GuardrailTriggered = true
			rc.Metadata["guardrail_name"] = name
			rc.Metadata["guardrail_action"] = "blocked"
			return pipeline.ResultError(models.ErrGuardrailBlocked(
				"content_blocked",
				p.buildBlockMessage(result),
			))
		case ActionWarn:
			result.Warnings = append(result.Warnings, fmt.Sprintf("%s: %s", name, cr.Message))
		case ActionLog:
			slog.Info("guardrail triggered (log mode)",
				"guardrail", name,
				"org_id", orgID,
				"score", cr.Score,
				"threshold", threshold,
				"message", cr.Message,
			)
		}
	}

	// 对非阻断命中写入元数据。
	if len(result.Triggered) > 0 {
		p.applyMetadata(rc, result)
	}

	return pipeline.ResultContinue()
}

// getGuardrail 按名称解析 guardrail 实现。
// 先查静态注册表，再查动态缓存，最后尝试用检查配置创建新的动态 guardrail。
func (p *GuardrailPlugin) getGuardrail(orgID, name string, check *tenant.GuardrailCheck) Guardrail {
	preferDynamic := check != nil && len(check.Config) > 0 && p.dynamicFactory != nil

	if preferDynamic {
		if g := p.getCachedDynamicGuardrail(orgID, name); g != nil {
			return g
		}

		if g := p.createDynamicGuardrail(orgID, name, check.Config); g != nil {
			return g
		}
	}

	// 1. 尝试静态注册表（内置 guardrail，如 pii-detection、prompt-injection 等）。
	if p.registry != nil {
		if g, ok := p.registry[name]; ok {
			return g
		}
	}

	// 2. 尝试动态缓存（之前已经从 org config 创建过的外部 guardrail）。
	if g := p.getCachedDynamicGuardrail(orgID, name); g != nil {
		return g
	}

	// 3. 根据 check config 创建动态 guardrail（FI Eval、webhook 等）。
	if check.Config == nil || p.dynamicFactory == nil {
		return nil
	}

	g := p.createDynamicGuardrail(orgID, name, check.Config)
	if g == nil {
		return nil
	}

	return g
}

func (p *GuardrailPlugin) getCachedDynamicGuardrail(orgID, name string) Guardrail {
	p.dynamicMu.RLock()
	defer p.dynamicMu.RUnlock()

	if orgCache, ok := p.dynamicCache[orgID]; ok {
		if g, ok := orgCache[name]; ok {
			return g
		}
	}

	return nil
}

func (p *GuardrailPlugin) createDynamicGuardrail(orgID, name string, cfg map[string]interface{}) Guardrail {
	if cfg == nil || p.dynamicFactory == nil {
		return nil
	}

	g := p.dynamicFactory(name, cfg)
	if g == nil {
		return nil
	}

		// 创建成功后写入缓存，后续同 org、同 name 的请求可以复用实例。
	p.dynamicMu.Lock()
	if _, ok := p.dynamicCache[orgID]; !ok {
		p.dynamicCache[orgID] = make(map[string]Guardrail)
	}
	p.dynamicCache[orgID][name] = g
	p.dynamicMu.Unlock()

	slog.Info("created dynamic guardrail from org config",
		"name", name,
		"org_id", orgID,
	)

	return g
}

// runGuardrailSafe 用超时和 panic 恢复包住动态 guardrail。
// 动态 guardrail 可能来自外部配置或第三方服务，不能让它拖垮整个网关请求。
func (p *GuardrailPlugin) runGuardrailSafe(ctx context.Context, g Guardrail, input *CheckInput, timeout time.Duration) *CheckResult {
	checkCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	type result struct {
		cr  *CheckResult
		err error
	}
	ch := make(chan result, 1)

	go func() {
		defer func() {
			if r := recover(); r != nil {
				slog.Error("dynamic guardrail panicked",
					"guardrail", g.Name(),
					"panic", r,
				)
				ch <- result{err: fmt.Errorf("guardrail panicked: %v", r)}
			}
		}()
		cr := g.Check(checkCtx, input)
		ch <- result{cr: cr}
	}()

	select {
	case res := <-ch:
		if res.err != nil {
			slog.Error("dynamic guardrail error",
				"guardrail", g.Name(),
				"error", res.err,
				"fail_open", p.failOpen,
			)
			if p.failOpen {
				return nil
			}
			return &CheckResult{
				Pass:    false,
				Score:   1.0,
				Message: fmt.Sprintf("guardrail %q error: %v", g.Name(), res.err),
			}
		}
		return res.cr
	case <-checkCtx.Done():
		slog.Warn("dynamic guardrail timed out",
			"guardrail", g.Name(),
			"timeout", timeout,
			"fail_open", p.failOpen,
		)
		if p.failOpen {
			return nil
		}
		return &CheckResult{
			Pass:    false,
			Score:   1.0,
			Message: fmt.Sprintf("guardrail %q timed out", g.Name()),
		}
	}
}

// resolvePolicy 查找 org、key 和单次请求级别的 guardrail 策略覆盖。
// 优先级从高到低：per-request > per-key > per-org > global default。
func (p *GuardrailPlugin) resolvePolicy(rc *models.RequestContext) (*policy.Policy, policy.RequestPolicy, *models.APIError) {
	// 1. 从 tenant store 查找 org 级 guardrail policy。
	var orgPolicy *policy.Policy
	if orgID := rc.Metadata["org_id"]; orgID != "" && p.tenantStore != nil {
		if orgCfg := p.tenantStore.Get(orgID); orgCfg != nil {
			orgPolicy = orgCfg.GuardrailPolicy()
		}
	}

	// 2. 查找 API key 级 policy。
	var keyPolicy *policy.Policy
	if keyID := rc.Metadata["auth_key_id"]; keyID != "" && p.policyStore != nil {
		keyPolicy = p.policyStore.Get(keyID)
	}

	// 3. 合并：org policy 作为基础，key policy 覆盖同名规则。
	merged := mergePolicy(orgPolicy, keyPolicy)

	// 4. 检查单次请求 header 覆盖。
	reqPolicyStr := rc.Metadata["x-guardrail-policy"]
	if reqPolicyStr == "" {
		return merged, policy.RequestPolicyNone, nil
	}

	// 只有显式允许覆盖的 key 才能使用 X-Guardrail-Policy。
	if rc.Metadata["key_allow_policy_override"] != "true" {
		return nil, policy.RequestPolicyNone, models.ErrForbidden(
			"API key does not allow guardrail policy overrides. Set metadata.allow_policy_override: true",
		)
	}

	rp, valid := policy.ParseRequestPolicy(reqPolicyStr)
	if !valid {
		return nil, policy.RequestPolicyNone, models.ErrBadRequest(
			"invalid_policy",
			fmt.Sprintf("invalid X-Guardrail-Policy value: %q (valid: log-only, disabled, strict)", reqPolicyStr),
		)
	}

	return merged, rp, nil
}

// mergePolicy 合并 org 级和 key 级 policy。
// key 级覆盖优先于 org 级覆盖；两者都为空时返回 nil。
func mergePolicy(org, key *policy.Policy) *policy.Policy {
	if org == nil {
		return key
	}
	if key == nil {
		return org
	}

	// 两者都存在时复制一份新 map，避免修改原始缓存对象。
	merged := &policy.Policy{
		Overrides: make(map[string]policy.Override, len(org.Overrides)+len(key.Overrides)),
	}

	// 先写入 org 级覆盖。
	for name, ov := range org.Overrides {
		merged.Overrides[name] = ov
	}

	// 再写入 key 级覆盖，同名规则会替换 org 级配置。
	for name, ov := range key.Overrides {
		merged.Overrides[name] = ov
	}

	return merged
}

func (p *GuardrailPlugin) processResult(rc *models.RequestContext, result *PipelineResult) pipeline.PluginResult {
	if result.Blocked {
		rc.Flags.GuardrailTriggered = true
		if len(result.Triggered) > 0 {
			tg := result.Triggered[0]
			rc.Metadata["guardrail_name"] = tg.Name
			rc.Metadata["guardrail_action"] = "blocked"
		}
			// 即使被 block，也要存结构化结果，方便日志和 trace 记录命中的规则。
		storeGuardrailResults(rc, result)
		return pipeline.ResultError(models.ErrGuardrailBlocked(
			"content_blocked",
			p.buildBlockMessage(result),
		))
	}

	p.applyMetadata(rc, result)
	return pipeline.ResultContinue()
}

func (p *GuardrailPlugin) applyMetadata(rc *models.RequestContext, result *PipelineResult) {
	if len(result.Triggered) > 0 {
		rc.Flags.GuardrailTriggered = true
		names := make([]string, len(result.Triggered))
		for i, tg := range result.Triggered {
			names[i] = tg.Name
		}
		rc.Metadata["guardrail_name"] = strings.Join(names, ",")

		if len(result.Warnings) > 0 {
			rc.Metadata["guardrail_action"] = "warned"
			rc.Metadata["guardrail_warnings"] = strings.Join(result.Warnings, "; ")
		} else {
			rc.Metadata["guardrail_action"] = "logged"
		}

			// 将结构化 guardrail 结果交给下游 logging 插件写入 trace。
		storeGuardrailResults(rc, result)
	}
}

// storeGuardrailResults 将命中的 guardrail 明细复制到 RequestContext。
// logging 插件会读取这些结果，并把它们写入 trace 记录。
func storeGuardrailResults(rc *models.RequestContext, result *PipelineResult) {
	for _, tg := range result.Triggered {
		rc.GuardrailResults = append(rc.GuardrailResults, models.GuardrailResult{
			Name:      tg.Name,
			Score:     tg.Score,
			Threshold: tg.Threshold,
			Action:    actionString(tg.Action),
			Message:   tg.Message,
		})
	}
}

func actionString(a Action) string {
	switch a {
	case ActionBlock:
		return "block"
	case ActionWarn:
		return "warn"
	case ActionLog:
		return "log"
	default:
		return "unknown"
	}
}

func (p *GuardrailPlugin) buildBlockMessage(result *PipelineResult) string {
	if len(result.Triggered) > 0 {
		tg := result.Triggered[0]
		return fmt.Sprintf("Request blocked by guardrail: %s — %s", tg.Name, tg.Message)
	}
	return "Request blocked by guardrail"
}

// InvalidateDynamicCache 清除某个 org 的动态 guardrail 缓存。
// 当 org 的 guardrail config 发生变化时调用，避免继续使用旧规则实例。
func (p *GuardrailPlugin) InvalidateDynamicCache(orgID string) {
	p.dynamicMu.Lock()
	delete(p.dynamicCache, orgID)
	p.dynamicMu.Unlock()
}
