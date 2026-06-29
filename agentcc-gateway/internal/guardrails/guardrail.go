package guardrails

import (
	"context"

	"github.com/futureagi/agentcc-gateway/internal/models"
)

// Stage 表示 guardrail 在请求链路中的运行阶段。
type Stage int

const (
	// StagePre 在调用模型提供商之前运行。
	StagePre Stage = iota
	// StagePost 在模型提供商返回之后运行。
	StagePost
)

// Action 定义 guardrail 命中后的处理方式。
type Action int

const (
	// ActionBlock 拒绝请求并返回 403。
	ActionBlock Action = iota
	// ActionWarn 添加告警信息，但继续处理请求。
	ActionWarn
	// ActionLog 只记录结果，不影响请求执行。
	ActionLog
)

// CheckInput 是单次 guardrail 检查的输入。
type CheckInput struct {
	Request  *models.ChatCompletionRequest
	Response *models.ChatCompletionResponse // pre 阶段为 nil
	Metadata map[string]string
}

// CheckResult 是单次 guardrail 检查的输出。
type CheckResult struct {
	Pass    bool                   // true = 安全，false = 已命中
	Score   float64                // 0.0 = 安全，1.0 = 最高违规程度
	Action  Action                 // 命中后采取的动作
	Message string                 // 面向人的解释信息
	Details map[string]interface{} // guardrail 专属元数据
}

// Guardrail 是所有 guardrail 实现必须满足的接口。
type Guardrail interface {
	// Name 返回 guardrail 标识符。
	Name() string
	// Stage 返回该 guardrail 的运行阶段。
	Stage() Stage
	// Check 评估输入并返回检查结果。
	Check(ctx context.Context, input *CheckInput) *CheckResult
}

// TriggeredGuardrail 记录一次执行过程中被命中的 guardrail。
type TriggeredGuardrail struct {
	Name      string  `json:"name"`
	Score     float64 `json:"score"`
	Threshold float64 `json:"threshold"`
	Action    Action  `json:"action"`
	Message   string  `json:"message"`
}

// PipelineResult 是同一阶段所有 guardrail 的聚合执行结果。
type PipelineResult struct {
	Blocked   bool
	Warnings  []string
	Triggered []TriggeredGuardrail
}

// shouldTrigger 将配置阈值应用到基于分数的 guardrail。
// 未返回有效分数但 Pass=false 的 guardrail 仍按硬失败处理。
func shouldTrigger(result *CheckResult, threshold float64) bool {
	if result == nil {
		return false
	}
	if result.Score > threshold {
		return true
	}
	return !result.Pass && result.Score <= 0
}
