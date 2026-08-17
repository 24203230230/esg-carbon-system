# ESG碳信息提取服务（Web API沙箱）

> 项目：[DATA_ROOT] · ESG碳治理能力画像系统
> 模块：智能体一提取服务（前端 → 提取服务 → 评分智能体）

## 功能

接收 PDF/Word ESG 报告 → 调用 DeepSeek API → 输出结构化 JSON（4维度×12子域×42指标）

## 目录结构

```
esg_extractor/
├── extract_service.py      # Flask Web API 服务
├── extraction_prompt.md    # 智能体一提取提示词（完整版）
├── Dockerfile              # 镜像构建
├── docker-compose.yml      # 容器编排（接入 ai-network）
├── requirements.txt        # Python 依赖
├── .env.example            # 环境变量模板
├── output/                 # 共享输出（供 ADP 读取）
└── README.md
```

## 快速开始

### 1. 配置 API Key

```bash
cp .env.example .env
# 编辑 .env 填入 DEEPSEEK_API_KEY
```

### 2. 构建并启动

```bash
docker-compose up -d --build
```

### 3. 验证服务

```bash
# 健康检查
curl http://localhost:8082/api/health

# 提取测试（上传一份PDF）
curl -X POST -F "file=@/path/to/report.pdf" http://localhost:8082/api/extract
```

## API 接口

### GET /api/health

健康检查，返回服务状态、依赖、API Key 配置情况。

**响应示例**：
```json
{
  "status": "ok",
  "service": "esg-extractor",
  "version": "1.0.0",
  "model": "deepseek-chat",
  "pdf_support": true,
  "docx_support": true,
  "prompt_loaded": true,
  "deepseek_key_set": true
}
```

### POST /api/extract

提取 ESG 报告为结构化 JSON。

**请求**：
- `file`: PDF/Word 文件（multipart/form-data）
- `user_prompt`: 可选，附加用户提示词

**响应**：
```json
{
  "success": true,
  "filename": "xxx.pdf",
  "file_size_kb": 1024.0,
  "text_chars": 45000,
  "duration_sec": 12.5,
  "model": "deepseek-chat",
  "tokens_used": 3240,
  "extracted": {
    "报告元数据": {
      "企业名称": "五粮液",
      "报告年份": "2025"
    },
    "指标提取结果": {
      "企业内部碳管理": { ... },
      "供应链碳管理": { ... },
      "透明度": { ... },
      "金融化情况": { ... }
    }
  }
}
```

### POST /api/extract_score

提取 + 一站式（评分交给 ADP）。当前版本将提取结果保存到共享目录，供 ADP 评分容器读取。

**响应**：同 `/api/extract`，额外返回 `output_path` 字段。

## 架构

```
┌──────────────┐  上传PDF/Word
│  前端(队友)  │ ─────────────┐
└──────────────┘              ↓
                    ┌─────────────────────┐
                    │  extract_service    │
                    │  (Flask, 8082)      │
                    │  - pdfplumber       │
                    │  - DeepSeek API     │
                    │  - 智能体一提示词   │
                    └─────────────────────┘
                              ↓ JSON
                    ┌─────────────────────┐
                    │  output/ 共享目录   │
                    └─────────────────────┘
                              ↓ (后续 ADP 读取)
                    ┌─────────────────────┐
                    │  ADP 评分智能体二   │
                    └─────────────────────┘
```

## 注意事项

1. **PDF 扫描件处理**：当前版本仅支持文字型 PDF，扫描件需先 OCR（待优化）
2. **文本长度限制**：超过 6 万字符自动截断，避免超出 DeepSeek token 限制
3. **API 限流**：DeepSeek 官方有 QPS 限制（默认 60次/分钟），并发高时建议加令牌桶
4. **大文件**：单文件上限 50MB（Flask `MAX_CONTENT_LENGTH`）

## 故障排查

```bash
# 查看容器日志
docker logs -f esg-extractor

# 进入容器调试
docker exec -it esg-extractor bash

# 重新构建
docker-compose down && docker-compose up -d --build
```

## 后续优化

- 接入 RAG（WeKnora）做多报告对比
- OCR 支持扫描件 PDF
- 异步队列（Redis/RabbitMQ）削峰
- 评分智能体二本地化（替代 ADP）
