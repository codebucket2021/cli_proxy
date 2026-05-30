# Changelog

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
