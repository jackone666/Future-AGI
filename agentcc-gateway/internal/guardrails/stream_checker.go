package guardrails

import (
	"context"
	"encoding/json"
	"log/slog"
	"strings"

	"github.com/futureagi/agentcc-gateway/internal/config"
	"github.com/futureagi/agentcc-gateway/internal/guardrails/policy"
	"github.com/futureagi/agentcc-gateway/internal/models"
)

// StreamGuardrailChecker 检查流式响应片段，并按配置间隔对累积文本运行 post 阶段 guardrail。
type StreamGuardrailChecker struct {
	engine        *Engine
	keyPolicy     *policy.Policy
	requestPolicy policy.RequestPolicy
	request       *models.ChatCompletionRequest
	metadata      map[string]string
	checkInterval int    // 两次检查之间的字符数（0 = 只在结束时检查）
	failureAction string // "stop" 或 "disclaimer"
	accumulated   strings.Builder
	lastCheckLen  int
}

// NewStreamChecker 创建新的流式 guardrail 检查器。
func NewStreamChecker(
	engine *Engine,
	cfg config.StreamingGuardrailConfig,
	keyPolicy *policy.Policy,
	requestPolicy policy.RequestPolicy,
	request *models.ChatCompletionRequest,
	metadata map[string]string,
) *StreamGuardrailChecker {
	interval := cfg.CheckInterval
	if interval <= 0 {
		interval = 100 // 默认每 100 个字符检查一次
	}
	action := cfg.FailureAction
	if action == "" {
		action = "stop"
	}
	return &StreamGuardrailChecker{
		engine:        engine,
		keyPolicy:     keyPolicy,
		requestPolicy: requestPolicy,
		request:       request,
		metadata:      metadata,
		checkInterval: interval,
		failureAction: action,
	}
}

// StreamCheckResult 表示处理一个流式片段后的结果。
type StreamCheckResult struct {
	// Blocked 为 true 表示 guardrail 命中且流应停止。
	Blocked bool
	// Message 是阻断时的 guardrail 信息。
	Message string
	// Disclaimer 在 failureAction 为 "disclaimer" 且结束检查命中时设置。
	Disclaimer string
}

// ProcessChunk 累积流式片段中的文本；当新增内容足够多时运行 guardrail。
// 返回值用于指示流是否应继续。
func (sc *StreamGuardrailChecker) ProcessChunk(ctx context.Context, chunk models.StreamChunk) *StreamCheckResult {
	// 提取增量内容。
	for _, choice := range chunk.Choices {
		if choice.Delta.Content != nil {
			sc.accumulated.WriteString(*choice.Delta.Content)
		}
	}

	// 检查是否已累积足够的新内容来运行 guardrail。
	currentLen := sc.accumulated.Len()
	if currentLen-sc.lastCheckLen < sc.checkInterval {
		return &StreamCheckResult{} // 新内容还不够
	}

	return sc.runCheck(ctx)
}

// Finish 对完整累积文本运行最后一次 guardrail 检查。
// 应在流结束时调用。
func (sc *StreamGuardrailChecker) Finish(ctx context.Context) *StreamCheckResult {
	if sc.accumulated.Len() == 0 {
		return &StreamCheckResult{}
	}
	// 如果当前长度刚检查过，则不重复运行。
	if sc.accumulated.Len() == sc.lastCheckLen {
		return &StreamCheckResult{}
	}
	return sc.runCheck(ctx)
}

// AccumulatedText 返回完整的累积响应文本。
func (sc *StreamGuardrailChecker) AccumulatedText() string {
	return sc.accumulated.String()
}

func (sc *StreamGuardrailChecker) runCheck(ctx context.Context) *StreamCheckResult {
	sc.lastCheckLen = sc.accumulated.Len()

	if sc.engine == nil || sc.engine.PostCount() == 0 {
		return &StreamCheckResult{}
	}

	// 为 guardrail 构造一个合成响应。
	text := sc.accumulated.String()
	contentJSON, _ := json.Marshal(text)

	input := &CheckInput{
		Request: sc.request,
		Response: &models.ChatCompletionResponse{
			Choices: []models.Choice{
				{
					Message: models.Message{
						Role:    "assistant",
						Content: contentJSON,
					},
				},
			},
		},
		Metadata: sc.metadata,
	}

	result := sc.engine.RunPost(ctx, input, sc.keyPolicy, sc.requestPolicy)
	if !result.Blocked {
		return &StreamCheckResult{}
	}

	msg := "Content blocked by streaming guardrail"
	if len(result.Triggered) > 0 {
		msg = result.Triggered[0].Message
	}

	slog.Info("streaming guardrail triggered",
		"action", sc.failureAction,
		"message", msg,
		"accumulated_chars", sc.accumulated.Len(),
	)

	if sc.failureAction == "disclaimer" {
		return &StreamCheckResult{
			Disclaimer: "\n\n[Content warning: " + msg + "]",
		}
	}

	return &StreamCheckResult{
		Blocked: true,
		Message: msg,
	}
}
