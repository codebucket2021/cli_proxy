# Changelog

## [1.15.0] - 2026-07-21

### Added

- 组级上游兼容配置新增 `tools_pad`（默认关闭，仅在组上显式开启时生效）：给客户端工具数不足 8 个的请求补足 Claude Code 真实工具名的极简假工具（~1.3KB，空 schema + "不可调用"描述），并在原请求完全无工具且未指定 `tool_choice` 时追加 `tool_choice:{"type":"none"}` 防止模型误调。适配 anyrouter 2026-07-21 起收紧的请求指纹校验——要求 thinking 开启（配合 `force_thinking`）**且**带足量真名客户端工具 schema，否则 429 Service Unavailable，导致 WebFetch / WebSearch / 标题生成等辅助请求全挂（实测：失败体 + adaptive + 7 个真名工具 → 200，3 个 → 429，改名 → 429，schema 内容不校验）。

## [1.14.0] - 2026-06-27

### Added

- 组级上游兼容配置，用于适配 anyrouter 等对请求形态有特殊限制的中转。**三项均默认关闭，仅在组上显式开启时对该组生效，不影响其它组**：
  - `force_thinking`：设为 `"adaptive"` 时，把该组 opus/sonnet 请求中 `thinking:{"type":"disabled"}` 或缺失的 thinking 改写为 `{"type":"adaptive"}`，haiku 改写为 `{"type":"enabled","budget_tokens":1024}`。修复部分中转拒绝 `thinking:disabled` 的 opus 请求（表现为 `429 Service Unavailable`）导致 subagent / WebFetch 等"关思考的子请求"不可用的问题。
  - `web_tool_choice_fix`：设为 `true` 时，移除带 `web_search`/`web_fetch` 服务端工具的请求中的强制 `tool_choice`（`{"type":"tool",...}`），退回 auto。修复部分中转拒绝"强制调用服务端工具"导致 WebSearch 不可用的问题（实测同一上游：cc 2.1.185 用强制 tool_choice 调 web_search → 429，cc 2.1.154 用 auto → 200）。
  - `strip_beta`：字符串数组，从该组请求的 `anthropic-beta` 头中移除指定 beta 标志，用于剥离上游不识别的新 beta。

## [1.13.5] - 2026-06-08

### Added

- 组级 key 重试策略支持配置 `max_consecutive_fatal_failures`、`max_consecutive_quota_failures`、`max_key_retries_per_request`、`quota_status_codes`、`quota_error_types`、`disable_quota_until_reset`。
- `429 + usage_limit_reached` 单独走 quota 分支，默认最多连续尝试 30 个 quota key、单请求最多尝试 50 个 fatal key，避免大号池 3 个 quota 429 后过早返回客户端。
- quota 429 会按上游 `resets_at` 写入 key 的 `disabled_until`，到期后自动恢复，避免把可恢复额度错误永久禁用。

### Changed

- 普通 fatal 错误仍默认连续 3 次停止重试，用于保留 Cloudflare/WAF、网络、认证链路异常的全局保护。

## [1.13.4] - 2026-05-31

### Changed

- 请求日志写入改为后台单队列串行追加，普通请求完成时不再读取并重写整个 `proxy_requests.jsonl`，降低多人共享时的磁盘 IO、JSON 解析开销和并发覆盖风险。
- 日志裁剪改为超过 `logLimit` 缓冲阈值后后台批量执行，仍会把被裁剪日志的 usage 聚合到 `history_usage.json`。
- 请求体日志默认只保留前 256KB，并记录 `*_bytes`、`*_logged_bytes`、`*_truncated` 元信息，避免大上下文请求把单条日志撑到数 MB。
- 当 `filtered_body` 与 `original_body` 完全一致时只保存一份内容，并通过 `filtered_body_same_as_original` 复用展示，减少重复存储。

## [1.13.3] - 2026-05-31

### Fixed

- 自动重试仅覆盖 30 秒内快速暴露的上游协议/连接错误；超过该窗口才断开的请求视为上游慢处理失败，不再重试，避免大上下文失败从约 180 秒拖到约 360 秒。
- 上游请求日志的 `upstream_attempts` / `upstream_error` 新增 `elapsed_ms` 和 `retry_skipped_reason`，可直接看出是否因 `late_upstream_disconnect` 跳过重试。

## [1.13.2] - 2026-05-31

### Fixed

- 上游协议级断流（如 `RemoteProtocolError` / `incomplete chunked read`）在安全窗口内自动重试一次，降低上游瞬时断连导致的 500。
- 发生可重试的上游协议错误时，为后续请求切换到新的 httpx 连接池，避免坏 keep-alive 连接放大连续失败。
- 流式响应在首个 chunk 前中断时会重建连接池并重发一次；首个 chunk 已发出后不透明重试，只注入 SSE error 并记录为上游流中断。
- 请求日志新增 `upstream_error`、`upstream_attempts`、`upstream_retry_count` 字段，便于区分上游断流、发送阶段失败和流式 body 中途失败。

## [1.13.1] - 2026-05-21

### Fixed

- 修复 `modalityTargets` 模态检测对 Codex（OpenAI Responses API）和 OpenAI Chat Completions 风格请求体不生效的问题：
  - 现同时扫描顶层 `messages[]` 和 `input[]` 两种容器
  - 图片 block 类型识别扩展为 `image` / `input_image` / `image_url` 三种形态
  - 影响：之前 Codex 渠道即便配置了 `modalityTargets.image`，因检测不到图片而始终走默认 `target`

## [1.13.0] - 2026-05-21

### Added

- 模型路由规则支持按内容模态（modality）选择目标模型：mapping 内新增可选字段 `modalityTargets`，命中规则后优先按请求内容模态（`image`/`audio`/`document`）匹配对应目标模型，无命中则回退到 `target`
  - 用途：同一渠道下不同能力模型并存的场景，例如 MiMo 渠道默认走 `mimo-v2.5-pro`（纯文本/Coding 旗舰），带图请求自动切到 `mimo-v2.5`（全模态），避免上游因模型不支持图像输入而返回 `404 No endpoints found that support image input`
  - 示例：
    ```jsonc
    {
      "source": "MiMo",
      "source_type": "config",
      "target": "mimo-v2.5-pro",
      "modalityTargets": { "image": "mimo-v2.5" }
    }
    ```
  - 完全向后兼容：未配置 `modalityTargets` 时行为与之前版本一致

### Changed

- UI 保存路由配置改为"保留式合并"：前端不感知的扩展字段（如 `modalityTargets`）按 `(source_type, source)` 匹配后会从旧配置自动合并回新配置，避免被前端整体 POST 覆盖丢失

## [1.12.4] - 2026-04-01

### Fixed

- 修复 key 自动禁用重试时全局性错误（如 Cloudflare/WAF 拦截返回 HTML）会遍历禁用所有 key 的问题
- 新增连续失败上限保护（默认 3 次），连续相同状态码超限则判定为非 key 级别错误，停止重试且不禁用 key
- 新增 HTML 响应检测，非 API 错误直接跳过禁用逻辑

## [1.12.3] - 2026-03-27

### Fixed

- 修复 Web UI 切换编辑模式时丢失组中额外配置字段（如 `fatal_status_codes`）的问题

## [1.12.2] - 2026-03-27

### Changed

- Key 自动禁用功能改为按组配置：组内新增可选字段 `fatal_status_codes`，未配置则不自动禁用任何 key
- 移除全局硬编码的 401/402/403 自动禁用逻辑，改由各组自行决定

## [1.10.3] - 2026-03-01

### Fixed

- 修复上游返回 `200` 但 body 为空时客户端报 `null is not an object` 的问题。上游偶尔返回 `status=200 + content-length=0` 的空响应，代理原样转发导致客户端解析失败。现在拦截此类异常响应，返回 `502` 和标准错误格式。
- 流式传输中断时（连接断开、解压错误等），注入 Anthropic 格式的 SSE error 事件，让客户端能正确识别错误而非静默失败。

## [1.10.2] - 2026-02-27

### Fixed

- 修复偶发的 `null is not an object (evaluating 'd$.content')` 错误。当代理无法连接上游 API（超时、断连等）时，返回的错误响应格式不符合 Anthropic API 规范，导致客户端无法正确解析。现在错误响应统一使用 Anthropic 标准格式 `{"type": "error", "error": {"type": "...", "message": "..."}}`。

## [1.10.1] - 2026-02-27

### Fixed

- 修复偶发的 `ZlibError` 错误。当上游 API 返回 gzip/deflate 压缩响应时，httpx 的 `aiter_bytes()` 会自动解压响应体，但代理原封不动透传了 `Content-Encoding` 头给客户端，导致客户端对已解压的数据尝试二次解压而报错。现在在响应头中剔除 `content-encoding`、`content-length`、`transfer-encoding` 三个头，避免客户端与代理之间的编码不一致。

## [1.10.0] - 2026-02-25

### Changed

- 基于 [guojinpeng/cli_proxy](https://github.com/guojinpeng/cli_proxy) v1.9.5 改造
- 规范化项目结构，采用标准 Python 打包方式（pyproject.toml）
- 适配最新版本 Claude Code，修复原项目兼容性问题
- 新增 Docker 部署支持
- 新增 Codex 代理支持
- 完善文档和使用说明
