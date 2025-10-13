# Deeplink URL Scanner

该项目用于扫描指定 GitHub 组织（默认 `Skyscanner`）下所有仓库，检测代码中对 deeplink URL 参数（例如 `utm_medium`, `associate_id` 等）的解析或读取行为，并生成汇总报告（Markdown / CSV / JSON）。

## 功能特性
- 获取组织下仓库列表（GraphQL 分页）
- 两种扫描模式：
  - `api`：GitHub Code Search（快速，依赖代码搜索能力）
  - `clone`：本地克隆 + 正则/上下文匹配（更精确，暂时基础实现）
- 可自定义参数关键词（`keywords.txt`）
- 并发控制与超时
- 输出报告：
  - `reports/parameters_report.md`
  - `reports/parameters_report.csv`
  - `reports/raw_result.json`
- 语言特征匹配（JS/TS、Python、Java/Kotlin、Swift、Go 的常见 query param 获取模式）
- GitHub Actions 支持（手动 & 定时任务）

## 快速开始

### 环境要求
- Python 3.11+
- (可选) ripgrep (`rg`) 用于 clone 模式加速
- Git（clone 模式需要）

### 安装
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 准备访问令牌
设置 `GITHUB_TOKEN` (PAT 或 GitHub Actions 默认 token)。如果需要访问私有仓库，请确保 token 具备相应权限：
```bash
export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxx
```

### 运行（API 模式）
```bash
python -m org_scanner.main \
  --org Skyscanner \
  --mode api \
  --keywords-file keywords.txt \
  --output-dir reports \
  --concurrency 8
```

### 可选参数
| 参数 | 说明 | 默认 |
|------|------|------|
| --org | 要扫描的组织 | Skyscanner |
| --mode | api 或 clone | api |
| --keywords-file | 额外关键词文件 | (无) |
| --output-dir | 报告输出目录 | reports |
| --concurrency | 并发协程数 | 6 |
| --max-repos | 限制仓库数量（调试） | 0 (不限) |
| --include-archived | 包含 archived 仓库 | 否 |
| --timeout | 单请求超时秒数 | 15 |

### 结果文件
| 文件 | 描述 |
|------|------|
| reports/parameters_report.md | 汇总表（仓库 -> 发现参数） |
| reports/parameters_report.csv | 行式展开（便于数据处理） |
| reports/raw_result.json | 结构化全部命中详情 |

### 示例 Markdown 报告行
```
| repository | parameters_detected | count | sample_files |
| flights-lib | utm_source, utm_medium, associate_id | 3 | src/lib/deeplink.js, backend/parser.py |
```

## 关键词扩展
默认内置参数（`src/org_scanner/main.py` 中 BUILTIN_KEYWORDS），可在 `keywords.txt` 新增：
```
referrer
partner_code
distribution_id
campaign_id
```

## GitHub Actions
工作流文件： `.github/workflows/scan.yml`
- 支持手动触发（workflow_dispatch）
- 支持每日定时（可调整 cron）
- 产出报告存入 artifact
- 可选：提交扫描结果到 `gh-pages` 或新分支（当前未启用，留注释）

## Clone 模式注意
当前 clone 模式示例实现为单线程遍历（可后续增强为并发）。适合更精��分析与本地离线环境。

## 后续改进建议
1. API 模式中追加文件内容请求，定位上下文行。
2. Clone 模式加入 asyncio 并发与缓存（增量扫描）。
3. 增加参数出现次数统计与 TOP 排序。
4. 增加 HTML Dashboard / 可视化。
5. 增加语言解析（AST）提升准确率。
6. 支持排除目录、文件白名单配置（YAML）。

## 贡献
欢迎提交 PR / Issue。

## License
MIT
