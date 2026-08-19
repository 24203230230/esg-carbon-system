#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESG碳信息提取服务（Web API沙箱）
=================================

提供两个核心接口：
1. POST /api/extract         - 接收PDF/Word → 提取为结构化JSON
2. GET  /api/health          - 健康检查
3. POST /api/extract_score   - 提取+评分一站式（评分需ADP接入）

调用流程：
  前端 → POST /api/extract (上传文件) → 提取为JSON
       ↓
  智能体一提示词 + DeepSeek API
       ↓
  返回: {报告元数据, 指标提取结果{4维度42指标}}
       ↓
  前端 → 传给 ADP 评分智能体二

启动：python extract_service.py [--port 8082]
环境变量：
  DEEPSEEK_API_KEY      - DeepSeek API Key（必需）
  DEEPSEEK_BASE_URL     - DeepSeek API地址（默认 https://api.deepseek.com）
  DEEPSEEK_MODEL        - 模型名（默认 deepseek-chat）
  PORT                  - 监听端口（默认 8082）
"""
import os
import sys
import json
import logging
import argparse
import time
import traceback
from pathlib import Path
from typing import Optional

# 第三方依赖
try:
    import pdfplumber
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

try:
    from docx import Document as DocxDocument
    DOCX_SUPPORT = True
except ImportError:
    DOCX_SUPPORT = False

import requests
from flask import Flask, request, jsonify

# ============================== 日志 ==============================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger('extract_service')

# ============================== 服务实例 ==============================
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB上限（ESG报告PDF可能很大）

# ============================== 配置 ==============================
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_BASE_URL = os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
DEEPSEEK_MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash')
# 深度思考模式：enabled 开启 | disabled 关闭（提取任务默认关闭，速度更快更便宜）
DEEPSEEK_THINKING = os.environ.get('DEEPSEEK_THINKING', 'disabled')

# 提示词路径（容器内 /app，宿主机 [HOME]/WorkBuddy/Claw/esg_extractor/）
PROMPT_FILE = os.environ.get('EXTRACTION_PROMPT_FILE', '/app/extraction_prompt.md')


# ============================== 加载提示词 ==============================
def load_extraction_prompt() -> str:
    """加载智能体一提取提示词（启动时加载一次）"""
    candidates = [
        PROMPT_FILE,
        os.path.join(os.path.dirname(__file__), 'extraction_prompt.md'),
        'extraction_prompt.md',
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                prompt = f.read()
            logger.info(f"✅ 提示词已加载: {path} ({len(prompt)} 字符)")
            return prompt
    raise FileNotFoundError(
        f"找不到提取提示词！请确认 extraction_prompt.md 在以下路径之一：{candidates}"
    )

EXTRACTION_PROMPT = load_extraction_prompt()


# ============================== 文档解析 ==============================
def extract_text_from_pdf(file_bytes: bytes) -> str:
    """使用 pdfplumber 提取 PDF 文本（中文PDF兼容）"""
    if not PDF_SUPPORT:
        raise RuntimeError("缺少 pdfplumber 依赖")
    import io
    text_parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            try:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    text_parts.append(f"[第{i+1}页]\n{page_text}")
            except Exception as e:
                logger.warning(f"PDF第{i+1}页提取失败: {e}")
    return "\n\n".join(text_parts)


def extract_text_from_docx(file_bytes: bytes) -> str:
    """使用 python-docx 提取 Word 文本"""
    if not DOCX_SUPPORT:
        raise RuntimeError("缺少 python-docx 依赖")
    import io
    doc = DocxDocument(io.BytesIO(file_bytes))
    text_parts = []
    for para in doc.paragraphs:
        if para.text.strip():
            text_parts.append(para.text)
    # 提取表格
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(cell.text.strip() for cell in row.cells)
            if row_text.strip():
                text_parts.append(row_text)
    return "\n".join(text_parts)


def extract_text_from_file(filename: str, file_bytes: bytes) -> str:
    """根据文件类型路由到对应解析器"""
    ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
    if ext == 'pdf':
        return extract_text_from_pdf(file_bytes)
    elif ext in ('docx', 'doc'):
        return extract_text_from_docx(file_bytes)
    else:
        raise ValueError(f"不支持的文件类型: .{ext}（仅支持 .pdf/.docx/.doc）")


# ============================== LLM 调用（one-api 网关，v=78） ==============================
# 统一走 models.py 的 call_llm → http://esg-one-api:3000/v1 → 按 model 路由到对应 provider
# 保留 call_deepseek 作为兼容别名（历史调用点），实际走 one-api 网关
def call_deepseek(system_prompt: str, user_text: str, max_retries: int = 2) -> dict:
    """兼容别名：调用 one-api 网关（默认 deepseek-v4-flash）"""
    from models import call_llm
    return call_llm(system_prompt, user_text, model_id='deepseek-v4-flash', max_retries=max_retries)


# ============================== 4 维度结构兜底 ==============================
def normalize_extracted_structure(data: dict) -> dict:
    """保证 LLM 返回的 JSON 结构完整：4 维度嵌套 + 报告元数据齐备。

    LLM 偶尔抽风：拍扁结构、缺维度、漏报元数据。这里统一修补，并保留真实数据。
    """
    DIMS = ['企业内部碳管理', '供应链碳管理', '透明度', '金融化情况']
    EMPTY_DIM = {"指标": {}, "未覆盖指标": []}

    if not isinstance(data, dict):
        return {
            '报告元数据': {'企业名称': '', '报告名称': '', '报告年份': '', '报告页码/章节': ''},
            '指标提取结果': {d: dict(EMPTY_DIM) for d in DIMS}
        }

    # 智能定位数据所在层级
    if '指标提取结果' in data:
        # LLM 直接返回 报告元数据+指标提取结果 在根（按 prompt 5.1 节）
        dims_root = data
    elif 'extracted' in data and isinstance(data.get('extracted'), dict):
        if '指标提取结果' in data['extracted'] or '报告元数据' in data['extracted']:
            # LLM 包了一层 extracted
            data = data['extracted']
            dims_root = data
        else:
            dims_root = data['extracted']
    else:
        # 完全没有结构
        data = {
            '报告元数据': {'企业名称': '', '报告名称': '', '报告年份': '', '报告页码/章节': ''},
            '指标提取结果': {}
        }
        dims_root = data

    # 报告元数据兜底
    dims_root.setdefault('报告元数据', {
        '企业名称': '', '报告名称': '', '报告年份': '', '报告页码/章节': ''
    })

    # 指标提取结果兜底
    dims_root.setdefault('指标提取结果', {})
    dims_root = dims_root['指标提取结果']

    # 检测 4 维度嵌套
    has_dims = all(d in dims_root for d in DIMS)

    # 拍扁模式：叶子指标直接出现在 dims_root 根下
    if not has_dims:
        if not dims_root:
            for d in DIMS:
                dims_root[d] = dict(EMPTY_DIM)
        else:
            leaf_dump = {}
            for k, v in list(dims_root.items()):
                leaf_dump[k] = v
                del dims_root[k]
            dims_root['企业内部碳管理'] = {"指标": leaf_dump, "未覆盖指标": []}
            for d in DIMS:
                if d not in dims_root:
                    dims_root[d] = dict(EMPTY_DIM)
            logger.warning(f"⚠️ LLM 输出结构被拍扁→自动归位到「企业内部碳管理」（其余维度空补全）")

    # 保证 4 维度都存在
    for d in DIMS:
        if not isinstance(dims_root.get(d), dict):
            dims_root[d] = dict(EMPTY_DIM)
        else:
            dims_root[d].setdefault('指标', {})
            dims_root[d].setdefault('未覆盖指标', [])

    total = sum(sum(len(v) for v in dims_root[d].get('指标', {}).values() if isinstance(v, dict)) for d in DIMS)
    logger.info(f"✅ 规范化完成: 4 维度齐全, 叶子指标 {total} 个")
    return data




# ============================== API端点 ==============================
@app.route('/api/health', methods=['GET'])
def health():
    """健康检查"""
    return jsonify({
        "status": "ok",
        "service": "esg-extractor",
        "version": "1.0.0",
        "model": DEEPSEEK_MODEL,
        "thinking": DEEPSEEK_THINKING,
        "pdf_support": PDF_SUPPORT,
        "docx_support": DOCX_SUPPORT,
        "prompt_loaded": len(EXTRACTION_PROMPT) > 0,
        "deepseek_key_set": bool(DEEPSEEK_API_KEY)
    })


@app.route('/api/extract', methods=['POST'])
def extract():
    """提取ESG报告碳信息为结构化JSON

    接受：
      - file: PDF/Word文件
      - user_prompt: 可选，附加的用户提示词
    返回：
      - 报告元数据（企业名称、报告年份等）
      - 指标提取结果（4维度×12子域×42指标）
    """
    start_time = time.time()

    # 1. 校验文件
    if 'file' not in request.files:
        return jsonify({"error": "请上传文件（file字段）"}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({"error": "文件名为空"}), 400

    # 公司类型（评分分流：listed=上市公司 / unlisted=非上市公司）
    company_type = request.form.get('company_type', 'listed')
    if company_type not in ('listed', 'unlisted'):
        company_type = 'listed'

    filename = file.filename
    file_bytes = file.read()
    file_size = len(file_bytes)
    logger.info(f"📄 接收文件: {filename} ({file_size/1024:.1f}KB)")

    # 2. 提取文本
    try:
        report_text = extract_text_from_file(filename, file_bytes)
    except Exception as e:
        logger.error(f"文档解析失败: {e}")
        return jsonify({"error": f"文档解析失败: {str(e)}"}), 400

    text_len = len(report_text)
    logger.info(f"📝 文本提取完成: {text_len} 字符")
    if text_len < 100:
        return jsonify({"error": "提取文本过短，可能为扫描件PDF，建议用OCR"}), 400

    # 3. 截断文本（避免超出token限制）
    MAX_TEXT_CHARS = 60000  # 约 2万token，留余量
    if text_len > MAX_TEXT_CHARS:
        logger.warning(f"文本过长({text_len}字符)，截断到{MAX_TEXT_CHARS}字符")
        report_text = report_text[:MAX_TEXT_CHARS] + "\n\n[后续内容已截断]"

    # 4. 构建用户提示词
    user_text = f"""请从以下ESG报告内容中提取碳信息（按完整字段结构输出JSON）。

## 文件名
{filename}

## 报告内容
```
{report_text}
```

## 额外要求
- 缺失字段标注为"未披露"
- 严格按提示词的输出格式返回，不要添加任何解释
"""

    # 5. 调用 LLM（one-api 网关，按 model 路由；默认 deepseek-v4-flash）
    model_id = request.form.get('model', 'deepseek-v4-flash')
    try:
        from models import call_llm
        result = call_llm(EXTRACTION_PROMPT, user_text, model_id=model_id)
    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        return jsonify({"error": f"LLM调用失败: {str(e)}"}), 500

    # 6. 解析LLM返回的JSON
    try:
        extracted_json = json.loads(result['content'])
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败: {e}")
        logger.debug(f"LLM原始输出: {result['content'][:500]}")
        return jsonify({
            "error": "LLM返回非JSON格式",
            "raw_content": result['content'][:2000],
            "tokens_used": result['tokens_used']
        }), 500

    # 6.4 记录 LLM 输出结构（用于排错）
    logger.info(f"LLM 提取结果 keys: {list(extracted_json.keys()) if isinstance(extracted_json, dict) else type(extracted_json)}")
    if isinstance(extracted_json, dict):
        ext = extracted_json.get('extracted', extracted_json)
        sample = json.dumps(ext, ensure_ascii=False)[:200]
        logger.info(f"LLM 提取 indicators 数据预览: {sample}")

    # 6.5 关键！后端兜底——保证 4 维度嵌套结构完整（防止 LLM 抽风拍扁）
    extracted_json = normalize_extracted_structure(extracted_json)

    # 6.6 评分增强（本地查表 + 确定性计算，无LLM参与）
    #     输入: 提取JSON + 行业 + 公司类型
    #     输出: L4计算结果(42项含权重/均值/完整度/质量/基础/对标/L4得分/障碍度) + 维度得分 + 总分 + 等级 + 障碍度Top10
    score_data = None
    try:
        from weights_logic import score_all
        meta = extracted_json.get('报告元数据', {})
        industry = str(meta.get('所属行业') or '').strip()
        score_data = score_all(extracted_json, industry, company_type)
        # 评分增强数据并入提取结果（前端两段式渲染第一段：分数立即填充）
        extracted_json['评分增强数据'] = score_data
        logger.info(f"🎯 评分增强完成: 行业={industry} 类型={company_type} 总分={score_data['总分']} 等级={score_data['评分等级']} 指标数={len(score_data['L4计算结果'])}")
    except Exception as e:
        logger.error(f"评分增强失败(不影响提取): {e}", exc_info=True)

    # 7. 组装最终返回
    duration = round(time.time() - start_time, 2)
    response = {
        "success": True,
        "filename": filename,
        "file_size_kb": round(file_size / 1024, 1),
        "text_chars": text_len,
        "duration_sec": duration,
        "model": result['model'],
        "tokens_used": result['tokens_used'],
        "company_type": company_type,
        "score_calculated": score_data is not None,
        "extracted": extracted_json
    }
    logger.info(f"✅ 提取完成: {filename} | {duration}s | {result['tokens_used']} tokens")
    return jsonify(response)


@app.route('/api/extract_score', methods=['POST'])
def extract_score():
    """提取+评分一站式（评分需外部接入ADP）

    注意：当前版本只做提取，评分通过文件输出或专门的ADP接口处理
    """
    # 简化：复用 /api/extract，结果存到共享卷供 ADP 读取
    extract_response = extract()
    if isinstance(extract_response, tuple):  # 错误响应
        return extract_response

    data = extract_response.get_json()
    extracted = data.get('extracted', {})

    # 可选：保存到共享目录，供 ADP 后续读取
    output_dir = os.environ.get('EXTRACT_OUTPUT_DIR', '/app/output')
    os.makedirs(output_dir, exist_ok=True)
    filename = data.get('filename', 'unknown')
    out_path = os.path.join(output_dir, f"{Path(filename).stem}_extracted.json")
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(extracted, f, ensure_ascii=False, indent=2)
    logger.info(f"💾 提取结果已保存: {out_path}")

    return jsonify({
        **data,
        "output_path": out_path,
        "next_step": "请将 extracted 字段传给 ADP 评分智能体二"
    })




@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "文件过大（超过200MB）"}), 413


@app.errorhandler(500)
def server_error(e):
    logger.error(f"服务器内部错误: {e}")
    return jsonify({"error": "服务器内部错误", "detail": str(e)}), 500


# ============================== 启动 ==============================
def main():
    parser = argparse.ArgumentParser(description='ESG碳信息提取服务')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8082)))
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("🌱 ESG 碳信息提取服务")
    logger.info("=" * 60)
    logger.info(f"监听: {args.host}:{args.port}")
    logger.info(f"模型: {DEEPSEEK_MODEL}")
    logger.info(f"API Key: {'已设置' if DEEPSEEK_API_KEY else '❌ 未设置！无法调用LLM'}")
    logger.info(f"PDF支持: {PDF_SUPPORT} | DOCX支持: {DOCX_SUPPORT}")
    logger.info(f"提示词: {len(EXTRACTION_PROMPT)}字符")
    logger.info("接口: GET /api/health | POST /api/extract | POST /api/extract_score")
    logger.info("=" * 60)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == '__main__':
    main()
