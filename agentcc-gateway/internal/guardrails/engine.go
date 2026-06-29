package guardrails

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/futureagi/agentcc-gateway/internal/config"
	"github.com/futureagi/agentcc-gateway/internal/guardrails/policy"
)

// resolvedRule 是绑定了配置参数后的 guardrail。
type resolvedRule struct {
	guardrail Guardrail
	mode      string // "sync" 或 "async"
	action    Action
	threshold float64
	timeout   time.Duration
}

// maxAsyncGuardrails 限制异步 guardrail goroutine 的并发数量。
const maxAsyncGuardrails = 64

// Engine 负责管理 guardrail 的执行。
type Engine struct {
	preGuardrails  []resolvedRule
	postGuardrails []resolvedRule
	failOpen       bool
	defaultTimeout time.Duration
	asyncSem       chan struct{} // 限制异步 goroutine 数量
}

// NewEngine 根据配置和 guardrail 实现注册表创建执行引擎。
func NewEngine(cfg config.GuardrailsConfig, registry map[string]Guardrail) *Engine {
	e := &Engine{
		failOpen:       cfg.FailOpen,
		defaultTimeout: cfg.DefaultTimeout,
		asyncSem:       make(chan struct{}, maxAsyncGuardrails),
	}
	if e.defaultTimeout <= 0 {
		e.defaultTimeout = 30 * time.Second
	}

	for _, rule := range cfg.Rules {
		g, ok := registry[rule.Name]
		if !ok {
			slog.Warn("guardrail not found in registry, skipping",
				"name", rule.Name,
			)
			continue
		}

		timeout := rule.Timeout
		if timeout <= 0 {
			timeout = e.defaultTimeout
		}

		resolved := resolvedRule{
			guardrail: g,
			mode:      rule.Mode,
			action:    parseAction(rule.Action),
			threshold: rule.Threshold,
			timeout:   timeout,
		}

		switch rule.Stage {
		case "pre":
			e.preGuardrails = append(e.preGuardrails, resolved)
		case "post":
			e.postGuardrails = append(e.postGuardrails, resolved)
		default:
			slog.Warn("unknown guardrail stage, skipping",
				"name", rule.Name,
				"stage", rule.Stage,
			)
		}
	}

	return e
}

// RunPre 执行所有 pre 阶段 guardrail，并可叠加按 key 配置的策略。
func (e *Engine) RunPre(ctx context.Context, input *CheckInput, p *policy.Policy, rp policy.RequestPolicy) *PipelineResult {
	return e.run(ctx, e.preGuardrails, input, p, rp)
}

// RunPost 执行所有 post 阶段 guardrail，并可叠加按 key 配置的策略。
func (e *Engine) RunPost(ctx context.Context, input *CheckInput, p *policy.Policy, rp policy.RequestPolicy) *PipelineResult {
	return e.run(ctx, e.postGuardrails, input, p, rp)
}

// PreCount 返回 pre 阶段 guardrail 数量。
func (e *Engine) PreCount() int {
	if e == nil {
		return 0
	}
	return len(e.preGuardrails)
}

// PostCount 返回 post 阶段 guardrail 数量。
func (e *Engine) PostCount() int {
	if e == nil {
		return 0
	}
	return len(e.postGuardrails)
}

func (e *Engine) run(ctx context.Context, rules []resolvedRule, input *CheckInput, p *policy.Policy, rp policy.RequestPolicy) *PipelineResult {
	result := &PipelineResult{}

	// 请求级策略：disabled 会跳过所有 guardrail。
	if rp == policy.RequestPolicyDisabled {
		return result
	}

	for _, rule := range rules {
		// 应用按 key 配置的策略覆盖。
		effectiveAction := rule.action
		effectiveThreshold := rule.threshold

		if p != nil {
			if ov, ok := p.Overrides[rule.guardrail.Name()]; ok {
				if ov.Disabled {
					continue // 对当前 key 跳过这个 guardrail
				}
				if ov.Action != "" {
					effectiveAction = parseAction(ov.Action)
				}
				if ov.Threshold != nil {
					effectiveThreshold = *ov.Threshold
				}
			}
		}

		// 应用请求级策略覆盖；优先级高于按 key 策略。
		switch rp {
		case policy.RequestPolicyLogOnly:
			effectiveAction = ActionLog
		case policy.RequestPolicyStrict:
			effectiveAction = ActionBlock
			effectiveThreshold = 0
		}

		if rule.mode == "async" {
			e.runAsync(rule, input)
			continue
		}

		// 同步执行，并为每个 guardrail 设置独立超时。
		checkResult := e.runSync(ctx, rule, input)
		if checkResult == nil {
			continue
		}

		// 应用命中阈值。
		triggered := shouldTrigger(checkResult, effectiveThreshold)
		if !triggered {
			continue
		}

		tg := TriggeredGuardrail{
			Name:      rule.guardrail.Name(),
			Score:     checkResult.Score,
			Threshold: effectiveThreshold,
			Action:    effectiveAction,
			Message:   checkResult.Message,
		}
		result.Triggered = append(result.Triggered, tg)

		switch effectiveAction {
		case ActionBlock:
			result.Blocked = true
			return result // block 后短路返回。
		case ActionWarn:
			result.Warnings = append(result.Warnings, fmt.Sprintf("%s: %s", rule.guardrail.Name(), checkResult.Message))
		case ActionLog:
			slog.Info("guardrail triggered (log mode)",
				"guardrail", rule.guardrail.Name(),
				"score", checkResult.Score,
				"threshold", effectiveThreshold,
				"message", checkResult.Message,
			)
		}
	}

	return result
}

func (e *Engine) runSync(ctx context.Context, rule resolvedRule, input *CheckInput) *CheckResult {
	checkCtx, cancel := context.WithTimeout(ctx, rule.timeout)
	defer cancel()

	// 执行检查并恢复 panic，避免单个 guardrail 拖垮请求。
	type result struct {
		cr  *CheckResult
		err error
	}
	ch := make(chan result, 1)

	go func() {
		defer func() {
			if r := recover(); r != nil {
				slog.Error("guardrail panicked",
					"guardrail", rule.guardrail.Name(),
					"panic", r,
				)
				ch <- result{err: fmt.Errorf("guardrail panicked: %v", r)}
			}
		}()
		cr := rule.guardrail.Check(checkCtx, input)
		ch <- result{cr: cr}
	}()

	select {
	case res := <-ch:
		if res.err != nil {
			return e.handleError(rule, res.err)
		}
		return res.cr
	case <-checkCtx.Done():
		slog.Warn("guardrail timed out",
			"guardrail", rule.guardrail.Name(),
			"timeout", rule.timeout,
			"fail_open", e.failOpen,
		)
		if e.failOpen {
			return nil // fail-open 时放行
		}
		return &CheckResult{
			Pass:    false,
			Score:   1.0,
			Action:  rule.action,
			Message: fmt.Sprintf("guardrail %q timed out", rule.guardrail.Name()),
		}
	}
}

func (e *Engine) runAsync(rule resolvedRule, input *CheckInput) {
	// 获取信号量槽位以限制并发；满载时丢弃本次异步检查。
	select {
	case e.asyncSem <- struct{}{}:
	default:
		slog.Warn("async guardrail dropped, concurrency limit reached",
			"guardrail", rule.guardrail.Name(),
			"limit", maxAsyncGuardrails,
		)
		return
	}

	go func() {
		defer func() { <-e.asyncSem }()
		defer func() {
			if r := recover(); r != nil {
				slog.Error("async guardrail panicked",
					"guardrail", rule.guardrail.Name(),
					"panic", r,
				)
			}
		}()

		ctx, cancel := context.WithTimeout(context.Background(), rule.timeout)
		defer cancel()

		cr := rule.guardrail.Check(ctx, input)
		if cr != nil && !cr.Pass {
			slog.Info("async guardrail triggered",
				"guardrail", rule.guardrail.Name(),
				"score", cr.Score,
				"message", cr.Message,
			)
		}
	}()
}

func (e *Engine) handleError(rule resolvedRule, err error) *CheckResult {
	slog.Error("guardrail error",
		"guardrail", rule.guardrail.Name(),
		"error", err,
		"fail_open", e.failOpen,
	)
	if e.failOpen {
		return nil
	}
	return &CheckResult{
		Pass:    false,
		Score:   1.0,
		Action:  rule.action,
		Message: fmt.Sprintf("guardrail %q error: %v", rule.guardrail.Name(), err),
	}
}

func parseAction(s string) Action {
	switch s {
	case "block":
		return ActionBlock
	case "warn":
		return ActionWarn
	case "log":
		return ActionLog
	default:
		return ActionLog
	}
}

// HasRule returns true if the engine has a static rule with the given name.
func (e *Engine) HasRule(name string) bool {
	if e == nil {
		return false
	}
	for _, r := range e.preGuardrails {
		if r.guardrail.Name() == name {
			return true
		}
	}
	for _, r := range e.postGuardrails {
		if r.guardrail.Name() == name {
			return true
		}
	}
	return false
}

// FailOpen returns the fail-open setting.
func (e *Engine) FailOpen() bool {
	if e == nil {
		return false
	}
	return e.failOpen
}

// DefaultTimeout returns the default timeout for guardrail checks.
func (e *Engine) DefaultTimeout() time.Duration {
	if e == nil {
		return 30 * time.Second
	}
	return e.defaultTimeout
}
