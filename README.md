# ESG 碳治理能力画像系统

面向企业与金融机构的碳治理能力评估系统 —— **DeepSeek 提取 42 指标 → Python 确定性算分 → ADP 智能体根因分析 → 前端可视化画像**。

## 架构

```
用户上传 ESG 报告 PDF
   ↓
[backend] DeepSeek v4-flash 提取 42 个 L4 指标（thinking disabled）
   ↓
[backend] weights_logic.py 确定性算分（公式 1-10）
   │        完整度/质量/基础/对标/L4/维度/总分/障碍度 Top10
   ↓ 增强 JSON（含「评分增强数据」）
[ADP 工作流] 元认知因果推导法则 → Top10 短板根因分析 + 改进建议
   ↓
[frontend] 两段式渲染：分数先填（Python 已算好）→ ADP 建议后填
            雷达图 / 评分圆环 / 完整 Markdown 报告 / pdfmake PDF 导出
```

**双层职责边界**（答辩口径）：
- **确定性计算层**（Python）：完整度/质量/对标/总分/障碍度 —— 100% 可复现、可审计
- **智能体推理层**（ADP）：失分根源分析/建议生成 —— 语义理解，元认知因果推导法则

## 目录结构

```
├── backend/          # 后端容器（Flask 提取服务 + 评分引擎 + ADP 代理）
│   ├── extract_service.py     # /api/extract + /api/suggest + SSE 解析
│   ├── weights_logic.py       # 评分公式 1-10 全实现
│   ├── weights.json           # 42 指标权重 + 10 行业均值 + 全市场中位数
│   ├── extraction_prompt.md   # 智能体一提取提示词
│   ├── frontend/              # 容器内前端副本（nginx COPY 构建）
│   └── docker-compose.yml     # esg-extractor + esg-frontend
├── frontend/         # 前端源（Desktop/ESG-Carbon-System）
│   ├── index.html / js/ / css/
│   └── vendor/                # 本地化第三方库（离线可用）
├── prompts/          # ADP 智能体提示词
│   ├── 智能体二障碍度根因分析提示词-v3.0.md
│   └── 智能体二评分提示词-ADP短提示词-v2.0.md
└── docs/             # 复盘/排障文档
```

## 快速启动

```bash
# 1. 配置密钥
cp .env.example .env   # 填入 DEEPSEEK_API_KEY + ADP_APP_KEY

# 2. 构建启动
cd backend
docker-compose up -d --build

# 3. 访问
# 前端: http://localhost （esg-frontend nginx 反代）
# 提取API: http://localhost/api/extract
# ADP代理: http://localhost/api/suggest
```

## 环境依赖

| 依赖 | 用途 | 获取 |
|------|------|------|
| DeepSeek API | 42 指标提取 | platform.deepseek.com |
| 腾讯云 ADP | 根因分析工作流 | cloud.tencent.com ADP 控制台 |
| weights.json | 权重/行业基准/中位数 | 由 94 家公司 xlsx 生成（权威源 v3.2） |

## 关键版本

- 前端 v=69（库本地化 + 完整 Markdown + pdfmake PDF + 画像页动态化）
- 智能体二提示词 v3.0（障碍度根因分析专家，防误导占位符版）

## 溯源

- 排障复盘：`docs/ESG碳画像系统排障复盘-20260816.docx`（15 个 Bug + 4 类根源 + 排查清单）
- 工作日志：`.workbuddy/memory/`（本地，不入库）
