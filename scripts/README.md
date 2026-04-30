# Windsurf 账号转换与验证脚本

将 any-auto-register 注册的 Windsurf 账号转换为 WindsurfAPI-2.0.42 可用的格式，并验证远程反代 API。

## 背景

any-auto-register 存储的是 Windsurf 网站的 `session_token`，而 WindsurfAPI 需要的是 Codeium 的 `apiKey`，两者属于不同的认证体系，无法直接导出使用。这些脚本负责桥接转换。

## 脚本一览

| 脚本 | 用途 | 需要网络 | 需要远程服务 |
|---|---|---|---|
| `api_convert.py` | 从 DB 读取 session_token → 获取 apiKey → 推送到远程 WindsurfAPI | 是 | 是 |
| `convert_to_windsurfapi.py` | 通用转换工具，支持 api/batch 两种模式 | api 模式需要 | batch+POST 需要 |
| `test_api.py` | 远程测试：health/models/probe/check/batch/chat | 是 | 是 |

---

## api_convert.py（推荐）

**最靠谱的方式**。从本地数据库读取 `session_token`，通过 Windsurf OTT 接口获取 Codeium `apiKey`，然后逐个推送到远程 WindsurfAPI。

### 转换流程

```
session_token → GetOneTimeAuthToken → Codeium register_user → apiKey → 推送到远程
```

三级回退策略：
1. `session_token` → OTT → Codeium 注册
2. `auth_token` → WindsurfPostAuth → session → OTT → Codeium 注册
3. `email+password` → Auth1 登录 → OTT → Codeium 注册

### 使用方式

编辑脚本顶部的配置后运行：

```bash
python scripts/api_convert.py
```

### 实测结果

60 个 Windsurf 账号全部成功（60/60），零失败。之前 batch-import 方式因 Firebase App Check 限制只有 10/60 成功。

---

## convert_to_windsurfapi.py

通用命令行工具，支持两种模式。

### api 模式

从数据库或 JSON 文件读取账号，获取 Codeium `apiKey`，生成 `accounts.json`。

```bash
# 从数据库转换
python scripts/convert_to_windsurfapi.py --mode api --db account_manager.db -o accounts.json

# 从导出的 JSON 转换
python scripts/convert_to_windsurfapi.py --mode api --json export.json -o accounts.json

# 使用代理
python scripts/convert_to_windsurfapi.py --mode api --db account_manager.db --proxy http://127.0.0.1:7890
```

生成的 `accounts.json` 可手动上传到 WindsurfAPI 的数据目录。

### batch 模式

导出 `email password` 文本，供 WindsurfAPI 的 batch-import 端点使用。

```bash
# 仅导出文件
python scripts/convert_to_windsurfapi.py --mode batch --db account_manager.db -o import.txt

# 直接 POST 到远程 WindsurfAPI
python scripts/convert_to_windsurfapi.py --mode batch --db account_manager.db \
    --api-url http://your-server:3003 --api-password yourpassword
```

> ⚠️ **注意**：batch 模式依赖 WindsurfAPI 服务端的登录流程，部分账号可能因 Firebase App Check 限制而失败。建议优先使用 `api_convert.py`。

### 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--mode` | `api` | `api`：获取 apiKey 生成 JSON；`batch`：导出 email+password |
| `--db` | `account_manager.db` | SQLite 数据库路径 |
| `--json` | - | 导出的 JSON 文件路径（替代 --db） |
| `-o` | 自动 | 输出文件路径 |
| `--proxy` | - | HTTP/SOCKS 代理 URL |
| `--api-url` | - | WindsurfAPI 地址（batch 模式 POST 用） |
| `--api-password` | - | WindsurfAPI Dashboard 密码 |

---

## test_api.py

远程 WindsurfAPI 测试工具，集成所有验证功能。

```bash
# 查看服务健康状态
python scripts/test_api.py health

# 列出模型目录（仅名称，不测试可用性）
python scripts/test_api.py models

# 查看账号列表和 tier 分布
python scripts/test_api.py list

# Probe 前 N 个账号（获取 tier 和 capabilities）
python scripts/test_api.py probe 5

# 查看账号 capabilities 详情
python scripts/test_api.py dump <account_id>

# 快速检测可用模型（测试 15 个代表性模型，约 30 秒）
python scripts/test_api.py check

# 逐个测试全部 119 个模型（约 5-10 分钟）
python scripts/test_api.py batch

# 测试单个模型对话
python scripts/test_api.py chat glm-4.7

# 测试流式对话
python scripts/test_api.py stream glm-4.7
```

### free 账号模型可用性

经实际 chat 测试，free tier 账号当前可用模型：

| 状态 | 模型 |
|---|---|
| ✅ 可用 | `gemini-2.5-flash`, `gemini-3.0-flash-low`, `glm-4.7`, `glm-5.1`, `gpt-5.2-low`, `minimax-m2.5`, `swe-1.5` |
| ❌ 不可用 | `claude-*`, `gpt-4o/5/5.1`, `o3/o4-mini`, `grok-3`, `kimi-k2` 等 |

> 可用模型列表可能随 Windsurf 上游策略变化，用 `check` 或 `batch` 命令实时检测。
> pro/trial 账号可使用全部模型。

---

## 依赖

- `requests`：`pip install requests`
- `sqlmodel`、`core.db`、`core.account_graph` 等：any-auto-register 自身模块（从项目根目录运行即可）

## 注意事项

- **`import.txt`** 包含账号密码，**绝对不要提交到 git**，已在 `.gitignore` 中排除
- 从项目根目录运行：`python scripts/xxx.py`
