# astrbot_plugin_ai_image_detector

**@机器人 发「ai吗」，检测图片是否带有 OpenAI（ChatGPT）生成水印。**

插件把图片交给 OpenAI 官方的 Content Provenance API，检查其中是否包含 C2PA 内容凭证、
SynthID 水印等 OpenAI 来源信号，再把结论发回群里。

> API 文档：<https://developers.openai.com/api/docs/guides/content-provenance>

## 功能

### 触发方式

满足以下任一场景即触发检测：

| 场景 | 你需要做的 |
| --- | --- |
| 发送图片 | 在**同一条消息**里带上图片，@机器人 并配文「ai吗」 |
| 引用图片 | **引用**（回复）一条图片消息，在自己的消息里 @机器人 并配文「ai吗」 |

关键词大小写不敏感（`AI吗` 同样有效），只看**你这条消息自己的文字**——
不会因为被引用的旧消息里出现「ai吗」而误触发；「@全体成员」也不算 @机器人。

### 回复内容

| 情况 | 机器人回复 |
| --- | --- |
| 检测到 OpenAI 水印 | `检测到 OpenAI 水印，图片是 AI 生成（命中：C2PA 内容凭证）` + 换行 + `来源模型/签发方：gpt-image-1` |
| 未检测到 | `未检测到 OpenAI 水印，无法判断` |
| 消息里没有图片 | 提示把图片和「ai吗」放在同一条消息，或引用一条图片消息 |
| 接口限流 / 额度不足 / Key 无效 | 对应的中文提示，不会回显接口原始报错 |
| 其他调用失败 | `检测失败：<原因>` |

命中和未命中的文案都可以自定义，见 [自定义回复文案](#自定义回复文案)。
检测触发后，这条消息不再交给默认 LLM 回答，因此不会出现「结论 + 闲聊」两条回复。

## 安装

### 方式一：上传压缩包（推荐，无需 Git）

1. 从仓库的 Release 下载 `astrbot_plugin_ai_image_detector-v1.0.0.zip`；
2. 打开 AstrBot WebUI → `插件` → `安装插件` → 「上传压缩包」，选中该 zip；
3. 安装完成后插件会自动加载，接着到插件配置里填 OpenAI API Key 即可。

### 方式二：克隆仓库

```bash
cd AstrBot/data/plugins
git clone https://github.com/Hitori2136/astrbot_plugin_ai_image_detector
```

然后在 WebUI 的 `插件` 页面点「重载插件」，或重启 AstrBot。
插件会依据 `requirements.txt` 自动安装依赖。

## 配置

在 WebUI 的 `插件 → astrbot_plugin_ai_image_detector → 配置` 中填写：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `openai_api_key` | 空 | OpenAI API Key |
| `api_base` | `https://api.openai.com/v1` | 接口地址，可指向自建/代理网关 |
| `proxy` | 空 | 代理地址，例如 `http://127.0.0.1:7890`；留空时沿用系统代理 |
| `use_env_proxy` | `true` | 仅在 `proxy` 留空时生效：是否沿用系统代理环境变量 |
| `trigger_keywords` | `["ai吗"]` | 触发关键词，忽略大小写与首尾空白 |
| `require_at_bot` | `true` | 是否必须 @机器人 |
| `reply_with_at` | `false` | 回复时是否 @ 提问者 |
| `max_images_per_message` | `1` | 单条消息最多检测几张图（1–5） |
| `timeout_seconds` | `60` | 请求超时（5–300 秒） |
| `notify_on_start` | `false` | 请求前先发一条「正在检测…」 |
| `show_details` | `false` | 结论后追加 C2PA / SynthID 原始明细 |
| `reply_text_detected` | 见下 | 检测到时的回复文案 |
| `reply_text_not_detected` | `未检测到 OpenAI 水印，无法判断` | 未检测到时的回复文案 |

### 自定义回复文案

两项留空即使用默认文案，填了就用你写的文本。

`reply_text_detected` 支持两个占位符：`{signals}` 替换成命中的信号（如 `C2PA 内容凭证`），
`{models}` 替换成来源模型 / 签发方；多个用 `、` 连接，没有内容时替换为空。
括号和「命中：」这类前后缀自己写在文案里。默认文案是：

```
检测到 OpenAI 水印，图片是 AI 生成（命中：{signals}）
来源模型/签发方：{models}
```

限流、额度不足、Key 无效等提示不受这两项影响。

## 工作原理

1. 收到消息后判断两件事：本条消息是否 **@了机器人本人**，文字里是否包含**触发关键词**。
2. 收集待检测图片：优先取本条消息里的图片；本条消息没有图片时，取被引用消息里的图片。
3. 将图片上传到 `{api_base}/content_provenance_checks`，响应形如：

```jsonc
{
  "object": "content_provenance_check",
  "created_at": 1756512000,
  "results": [
    {
      "type": "c2pa",                // C2PA 内容凭证
      "outcome": "detected",          // detected | not_detected
      "validation_state": "trusted",  // trusted | valid | invalid | not_present
      "issuer": "OpenAI",
      "model": null,
      "generated_at": "2026-08-30T00:00:00Z"
    },
    {
      "type": "synthid",              // SynthID 水印
      "outcome": "not_detected",
      "model": null,
      "generated_at": null
    }
  ]
}
```

4. 只要有任意一条结果的 `outcome` 为 `detected`，就判定为**检测到 OpenAI 水印**。
   接口将来新增其他信号类型时，插件同样按 `outcome` 判定，不会漏检。

## 调用限制与错误处理

Content Provenance API 有速率与额度限制，插件对此做了处理，不会把接口原始报错甩给群里的用户：

| 情况 | 插件行为 |
| --- | --- |
| 频率限制（429） | 按接口返回的 `Retry-After` 冷却（缺省 60 秒），回复「接口当前触发调用频率限制，暂时不可用，约 N 后再试」 |
| 额度不足 / 计费异常 | 冷却 10 分钟，并提示检查账号额度 |
| API Key 无效或权限不足 | 提示检查 Key，且不进入冷却 |
| 服务端故障或网络异常 | 自动重试（最多 3 次，间隔递增），仍失败则提示服务端暂不可用并短暂冷却 |
| 冷却期内再次触发 | 直接回复剩余恢复时间，不再发起请求，避免继续消耗配额 |
| 单条消息多张图片 | 一旦某张触发限流，立即停止本批剩余图片 |

其他细节：

- 频率限制不会重试，避免把一次限流放大成多次调用。
- 冷却状态在插件重载后依然有效；冷却结束后下一次触发自动恢复，无需重启。
- 所有提示都会抹掉 Key 之类的敏感串，不会泄露密钥。

## 局限性

- **`not_detected` 不等于「不是 AI 生成的」**，只表示没有找到 OpenAI 支持的来源信号。
  元数据被平台压缩或二次保存抹掉、水印被削弱、图片来自更早的模型或非 OpenAI 的模型，
  都可能得到这个结果。
- 该接口只检测 **OpenAI 自家**的来源信号，无法判断图片是否由其他厂商的模型生成。
- 不同 IM 平台对引用消息的支持程度不同：部分平台需要开启引用消息解析，
  否则机器人拿不到被引用消息里的图片，此时会提示「没有识别到图片」；
  也有平台不把被引用消息内容下发给机器人，这属于平台限制。
- `api.openai.com` 在部分地区需要代理才能访问，可以用 `api_base` 指向自建网关。

## License

MIT，详见 [LICENSE](LICENSE)。
