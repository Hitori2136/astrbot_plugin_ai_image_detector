# Changelog

## [0.1.0] - 2026-11-10

### 功能

- 指令 `/AI吗`（大小写不敏感）触发。
  在同一条消息里带图片，或引用一条图片消息再发送指令。
- 把图片上传到 OpenAI Content Provenance API，检测 C2PA 内容凭证与 SynthID 水印，
  任一信号的 `outcome` 为 `detected` 即判定检测到。
- 回复两段：第一段结论（来源 + 模型名，取不到时写「AI 制作」），
  第二段检测明细（来源、状态、模型、签发方、校验），由「附上检测明细」控制。

### 行为

- 触发后该消息不再交给默认 LLM 回答。
- 接口 429 按 `Retry-After` 冷却（缺省 60 秒），额度耗尽冷却 10 分钟，
  Key 无效不冷却，服务端故障自动重试 3 次后短暂冷却。
- 单条消息多图时，一旦某张触发限流，立即停止本批剩余图片。

### 配置

| 配置项 | 默认值 |
| --- | --- |
| `openai_api_key` | 空 |
| `api_base` | `https://api.openai.com/v1` |
| `proxy` | 空 |
| `use_env_proxy` | 开启 |
| `quote_reply` | 开启 |
| `reply_with_at` | 关闭 |
| `max_images_per_message` | `1` |
| `timeout_seconds` | `60` |
| `notify_on_start` | 关闭 |
| `show_details` | 关闭 |
