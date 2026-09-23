# 碳治 · 智评 —— ESG 碳治理能力画像系统

> 面向企业与金融机构的碳治理能力评估系统。
> **DeepSeek 提取 42 项指标 → Python 确定性算分 → LLM 智能体根因分析 → 前端可视化画像**

用户上传一份 ESG 报告（PDF / Word）后，系统自动完成"提取 → 评分 → 根因诊断 → 改进建议 → 画像报告"全链路，全程无需人工录入。

- 覆盖 **4 大维度 × 11 个 L4 子领域 × 42 项 L4 指标**
- 权重体系来自 **94 家上市公司 ESG 报告词频统计 + AHP 层次分析法**
- 算术层与推理层**职责严格分离**：分数 100% 可复现，建议可溯源

---

## 目录

- [核心特性](#核心特性)
- [系统架构](#系统架构)
- [快速启动](#快速启动)
- [复现与 AI 辅助部署](#复现与-ai-辅助部署)
- [目录结构](#目录结构)
- [环境变量](#环境变量)
- [API 一览](#api-一览)
- [评分方法论](#评分方法论)
- [版本沿革](#版本沿革)
- [许可与溯源](#许可与溯源)

---

## 核心特性

| 模块 | 能力 |
| --- | --- |
| **提取** | PDF / DOCX 双通道解析；**COS 直传**（≥6MB 走对象存储，实测 98MB ≈ 37s）与 1MB 分片上传双链路（上限 200MB，任一异常自动回落旧链路）；扫描件 RapidOCR 兜底（离线可用）；60K 字符智能截断 |
| **评分** | 完整度（0–3）/ 质量（0–3）/ 基础分 / 对标分（0–4）/ L4 得分 / 维度得分 / 总分 / 障碍度 Top10，公式 1–10 全确定性实现 |
| **对标** | 10 个行业基准库 + 全市场中位数；上市 / 非上市双轨权重；样本不足自动降级到全市场均值 |
| **诊断** | 智能体二输出「诊断摘要 + Top10 短板根因 + 改进建议」，采用元认知因果推导（障碍度逆向推演） |
| **可视化** | 雷达图、评分圆环、三色评分点、维度 Tab、多企业对比、S/A/B/C 评级 |
| **报告** | 服务端 WeasyPrint 精美 PDF（中文字体内嵌）+ 客户端 pdfmake 兜底；Word / PPT 导出 |
| **账户** | Supabase 邮箱认证 + 分析历史（PostgreSQL + RLS 行级安全）；**游客模式下无需登录即可跑完整分析** |
| **多模型** | one-api 网关统一路由 DeepSeek / GLM / Kimi / MiniMax，前端下拉切换 |
| **部署** | 四容器 Docker Compose 编排；公网入口走 Cloudflare 代理 + SSL，源站 nginx 直跑 80（免证书运维）；**SSH 管理通道与网站完全解耦**，服务器重启后网站 / 管理通道 / 定时任务全部自愈 |

---

## 系统架构

![系统三层架构](docs/architecture/图4-1_系统三层架构图.png)

**三层结构**：用户交互层（前端 SPA）→ 服务层（四容器 Docker 编排）→ 模型与数据层（DeepSeek API / Supabase 云服务）。

### 双层职责边界（核心设计原则）

这是本项目最关键的一条架构约束，也是答辩口径：

| 层次 | 承担者 | 职责 | 性质 |
| --- | --- | --- | --- |
| **算术层** | Python（`weights_logic.py`） | 完整度 / 质量 / 基础分 / 对标分 / L4 / 维度 / 总分 / 障碍度 | 确定性、可复现、可审计，**无 LLM 参与** |
| **推理层** | LLM（智能体一 / 智能体二） | 报告文本 → 结构化指标；失分根源分析 + 改进建议 | 语义理解、生成式，**不作数值计算** |

> 一句话：**能用公式算的绝不用模型猜，需要理解的绝不写死规则。**

完整的架构说明（数据流水线、容器编排、评分算法、工程决策）见 **[ARCHITECTURE.md](ARCHITECTURE.md)**。

---

## 快速启动

### 前置条件

- Docker Desktop（含 Docker Compose）
- 一个 DeepSeek API Key（<https://platform.deepseek.com>）
- 可选：Supabase 项目（仅登录 / 历史功能需要）

### 步骤

```bash
# 1) 配置环境变量
cp .env.example .env
#    编辑 .env，至少填入 DEEPSEEK_API_KEY

# 2) 创建容器网络（首次执行一次即可）
docker network create --subnet 172.23.0.0/16 ai-network

# 3) 构建并启动（四个容器）
docker-compose up -d --build

# 4) 访问
#    前端：  http://localhost
#    健康检查：http://localhost/api/health
```

> ⚠️ **必须使用 `docker-compose`（中间带连字符）**，`docker compose`（空格子命令）在部分环境下会报 `unknown shorthand flag`。

### 验证

```bash
curl http://localhost/api/health
# {"status":"ok","service":"esg-extractor",...}

curl -X POST -F "file=@/path/to/esg_report.pdf" http://localhost/api/extract
```

### 日常运维

```bash
docker-compose ps                      # 容器体检
docker-compose logs -f esg-extractor   # 提取服务日志
docker-compose down                    # 停机
docker-compose up -d                   # 启动
```

---

## 复现与 AI 辅助部署

本仓库**不含任何真实密钥与服务器凭据**（`.env` 已由 `.gitignore` 排除），也**不随仓库下发运维脚本**——克隆即得全部可复现代码。

**仓库地址**：<https://github.com/24203230230/esg-carbon-system>

部署过程可直接交给 AI 编程助手完成。以下工具均可用于本仓库（把仓库地址与目标环境丢给它即可）：

| 工具 | 说明 |
| --- | --- |
| **WorkBuddy** | 办公智能体，可读写本地文件并执行终端命令 |
| **Codex** | OpenAI 编程智能体 |
| **Claude Code** | Anthropic 终端编程智能体 |
| **DeepSeek Harness** | DeepSeek 官方智能体运行框架 |

可直接复制的提示词：

```text
仓库地址：https://github.com/24203230230/esg-carbon-system
目标环境：Ubuntu 24.04 + Docker 29（最低 2 核 2G）

请先阅读 README.md 与 ARCHITECTURE.md，然后：
1) 按 .env.example 生成 .env（密钥我会自己提供，绝不要写进仓库或提交）；
2) 创建容器网络：docker network create --subnet 172.23.0.0/16 ai-network；
3) 用 docker-compose up -d 启动四个容器（frontend / extractor / agent2 / one-api）；
4) 用 curl http://localhost/api/health 验证返回 build=v114。

遇到报错请说明根因与修复步骤，不要跳过验证步骤。
```

> 判断部署是否成功，看 `/api/health` 的 `build` 字段是否等于 `v114`——这是唯一可靠的信号，比页面能否打开更准。

---

## 目录结构

```
esg-carbon-system/
├── docker-compose.yml          # 四容器编排（extractor / frontend / agent2 / one-api）
├── Dockerfile                  # 提取 + 评分服务镜像
├── extract_service.py          # Flask 提取服务（提取 / 分片上传 / COS 直传 / OCR 兜底 / WeasyPrint PDF）
├── weights_logic.py            # ★ 评分引擎：公式 1–10 全实现（确定性算术层）
├── weights.json                # ★ 42 指标权重 + 10 行业基准库 + 全市场中位数 + 评分规则
├── models.py                   # LLM 网关配置与支持模型清单
├── extraction_prompt.md        # 智能体一 · 提取提示词
├── agent2/                     # 智能体二 · 障碍度根因分析服务
│   ├── suggest_service.py      #   /api/suggest（双后端 deepseek | adp）
│   ├── system_prompt.md        #   根因分析提示词 v3.0（元认知因果推导）
│   ├── user_prompt.md
│   ├── peer_retrieval.py       #   检索层：离线同行对标库检索（倒排索引）
│   ├── peer_index.json         #   102 家同行索引（构建产物，随镜像发布，运行时零网络依赖）
│   ├── web_search.py           #   企业公开信息检索（东方财富 / 巨潮资讯，免 Key）
│   └── materiality.py          #   实质性分析（纯 Python 确定性计算）
├── tools/build_peer_index.py   # 重建 peer_index.json 的构建脚本
├── frontend/                   # 前端 SPA（原生 JS + Chart.js，无构建步骤）
│   ├── index.html
│   ├── js/app.bundle.js        #   业务主逻辑
│   ├── js/charts.js            #   雷达图 / 圆环 / 三色评分点
│   ├── vendor/                 #   第三方库本地化（离线可用）
│   └── nginx.conf              #   静态资源 + API 反代 + gzip + 长缓存策略
├── templates/                  # 服务端 PDF 模板（Jinja2）+ 中文字体（思源黑体 CJKsc）
├── supabase_schema.sql         # 认证与历史表结构 + RLS 策略
├── CHANGELOG.md                # 版本沿革
├── deploy/                     # 部署与公网入口说明
├── docs/
│   ├── architecture/           # 架构图（论文图 4-1 / 4-2 / 4-3）
│   └── ESG碳画像系统排障复盘.docx
└── 智能体二*.md                # 根因分析提示词定稿（v2.0 / v3.0）
```

---

## 环境变量

完整注释见 [`.env.example`](.env.example)。关键项：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | ✅ | 提取与根因分析共用 |
| `DEEPSEEK_MODEL` | | 默认 `deepseek-v4-flash` |
| `ADP_BACKEND` | | `deepseek`（默认，自主可控）/ `adp`（比赛展示） |
| `ADP_APP_KEY` | | 仅 `ADP_BACKEND=adp` 时需要 |
| `LLM_GATEWAY_URL` / `LLM_GATEWAY_KEY` | | one-api 多模型网关地址与分发 Token |
| `COS_ENABLED` | | `1` 启用 COS 直传；不配 = 自动走原分片链路（功能不受影响，仅慢） |
| `COS_SECRET_ID` / `COS_SECRET_KEY` | | 对象存储密钥，**建议用最小权限子账号**（只需对象读写删） |
| `COS_BUCKET` / `COS_REGION` | | 存储桶与地域，如 `your-bucket-1250000000` / `ap-guangzhou` |
| `COS_UPLOAD_ENDPOINT` | | 上传端点；开启全球加速时填 `cos.accelerate.myqcloud.com` |
| `EXTRACT_PORT` | | 提取服务端口，默认 `8082` |

---

## API 一览

### `esg-extractor`（Flask · :8082）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查（依赖、Key、提示词加载状态） |
| POST | `/api/extract` | 上传报告 → 提取 42 指标 → 确定性评分 |
| POST | `/api/extract_text` | 直接提交文本提取 |
| POST | `/api/extract_async` | 异步提取（返回 `task_id`） |
| GET | `/api/extract_status/<task_id>` | 查询异步任务进度 |
| POST | `/api/upload_chunk` | 大文件分片上传（单片） |
| POST | `/api/upload_complete` | 分片合并并触发提取 |
| POST | `/api/upload_abort` | 放弃上传并清理临时分片 |
| POST | `/api/cos_upload_prepare` | 申请 COS 预签名 PUT URL（浏览器直传对象存储，不经服务器中转） |
| POST | `/api/cos_complete` | 直传完成后取件（三级回退下载）并触发提取 |
| POST | `/api/extract_score` | 提取 + 落盘共享目录（供外部读取） |
| POST | `/api/report` | WeasyPrint 生成精美 PDF 报告 |

### `esg-agent2`（Flask · :8083）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查（含当前后端模式） |
| POST | `/api/suggest` | 输入增强 JSON → 诊断摘要 + Top10 根因 + 改进建议 |

### `esg-one-api`（LLM 网关 · :3000，宿主 3001）

标准 OpenAI 兼容协议 `/v1/chat/completions`，由 one-api 转发至各渠道。

### `esg-frontend`（Nginx · :80）

静态资源 + 反向代理；`/api/` → extractor，`/api/suggest` → agent2，`/api/report` → extractor。

---

## 评分方法论

**权重来源**：94 家上市公司 ESG 报告 → 指标披露词频统计 → 频次映射（1/3/5/7/9）→ AHP 层次分析 → 归一化为 42 项全局权重。

**单指标评分链**（`weights_logic.score_indicator`）：

```
完整度 (0–3)          该指标下有几个子问题被回答
   ↓
质量分 (0–3)          表述是否量化、是否有数据支撑
   ↓
基础分 = 完整度 + 质量 (0–6)
   ↓
对标分 (0–4)          与行业基准 / 全市场中位数比较后分档
   ↓
L4 得分 = (基础分 + 对标分) × 权重
```

**对标降级容错**：行业不在 10 个基准库内，或该行业样本量 < 10 家 → 自动降级到全市场 93 家均值，避免小样本导致的评分失真。

**障碍度**（`calc_obstacle`）：以「权重 × 失分率」衡量该指标对总分的拖累程度，排序取 Top10 交给智能体二做逆向根因推导。

**等级映射**：总分 → S / A / B / C 四档。

---

## 版本沿革

系统版本以 `/api/health` 返回的 `build` 字段为准；前端各静态资源另带 `?v=NN` 缓存戳，**按资源独立维护**（只在该资源内容变化时才 bump，避免无谓地让用户重下全部资源）。

| 阶段 | 版本 | 里程碑 |
| --- | --- | --- |
| 起步 | v=67 – v=69 | 前端骨架、42 指标提取链路打通、库本地化、完整 Markdown + pdfmake PDF |
| 账户体系 | v=70 – v=77 | Supabase 认证 + 分析历史 + RLS、用户下拉卡片、密码逻辑加固 |
| 多模型 | v=78 – v=87 | one-api 多模型网关、模型映射修复、DeepSeek V4 Vision |
| 报告与性能 | v=88 – v=93 | PDF 模板商业风升级、**WeasyPrint 服务端 PDF**、性能基线 v=92（gzip + 长缓存 + PDF 库懒加载）、RapidOCR 扫描件兜底 |
| 证据链 | v=94 – v=102 | 报告可视化图表与诊断摘要入模板；Agent2 企业画像；检索层（离线同行对标索引）与联网企业检索组成三段式证据链 |
| 稳健性 | v=103 – v=104 | 建议载荷统一解析（修复报告整屏印出 JSON 源码）；大文件**断点续传**（只补缺片）与瞬断类错误重试策略 |
| 视角与口径 | v=105 – v=110 | 评估视角由「披露审计」转为「**碳治理能力诊断**」；同行覆盖率修正；行业均值附带样本量标注 |
| 报告与图表 | v=111 – v=113 | PDF 中文字体修复（OTTO/CFF → glyf/TrueType）；图表口径错配修复；「未披露」两情形区分 |
| **当前** | **v=114** | **COS 直传**：大文件改由浏览器凭预签名 URL 直传对象存储，98MB 报告上传由 4 分半降到约 37 秒；原分片链路完整保留作回落 |

> 当前生产基线：**v=114**。

---

## 许可与溯源

- **许可**：[LICENSE](LICENSE) —— 自定义「非商业使用」许可，商业用途需单独书面授权。
- **排障复盘**：[`docs/ESG碳画像系统排障复盘-20260816.docx`](docs/) —— 15 个 Bug、4 类根源、排查清单、溯源索引。
- **架构图**：[`docs/architecture/`](docs/architecture/) —— 论文图 4-1 / 4-2 / 4-3。
- **部署说明**：[`deploy/`](deploy/) —— 公网入口与容器编排的部署要点。

> 本系统生成的评分与建议仅供学术研究与决策参考，不构成投资建议或合规承诺。
