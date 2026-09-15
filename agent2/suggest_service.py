# -*- coding: utf-8 -*-
"""
ESG 碳画像系统 · 智能体二服务（障碍度根因分析）

职责：接收上游增强 JSON（已算好分数），对障碍度 Top10 做失分根源诊断 + 可落地改进建议。

架构定位：智能体二 = 推理层（语义理解），不做评分（Python 已算好）、不做提取、不做整合。

双后端模式（ADP_BACKEND 环境变量切换）：
  - deepseek（默认）: 内置双提示词（system_prompt.md + user_prompt.md），DeepSeek 直调，全自主可控
  - adp（可选）:     转发腾讯云 ADP 工作流（比赛展示·权威平台）

接口协议与旧版 extract_service /api/suggest 完全一致，前端零改动。
"""
import argparse
import json
import logging
import os
import time
import uuid as _uuid

import requests
from flask import Flask, jsonify, request

# ============================== 配置 ==============================
ADP_BACKEND = os.environ.get('ADP_BACKEND', 'deepseek')   # 'deepseek' | 'adp'

# DeepSeek（deepseek 模式必需）
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_BASE_URL = os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
DEEPSEEK_MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash')
# 根因分析是深度推理任务：enabled 更专业但慢/贵；disabled 快（与提取一致）。按需切换。
DEEPSEEK_THINKING = os.environ.get('DEEPSEEK_THINKING', 'disabled')

# ADP（adp 模式必需）
ADP_APP_KEY = os.environ.get('ADP_APP_KEY', '')
ADP_CHAT_ENDPOINT = os.environ.get('ADP_CHAT_ENDPOINT', 'https://wss.lke.cloud.tencent.com/adp/v2/chat')

# 提示词文件（COPY 进镜像，自包含）
SYSTEM_PROMPT_FILE = os.environ.get('SYSTEM_PROMPT_FILE', '/app/system_prompt.md')
USER_PROMPT_FILE = os.environ.get('USER_PROMPT_FILE', '/app/user_prompt.md')

# ============================== 日志 ==============================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('agent2')

app = Flask(__name__)
# v=83：请求体上限 50MB（防超大 body 打崩内存；正常增强 JSON 远小于此）
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024


# ============================== 提示词加载 ==============================
def load_file(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


# ============================== LLM 直调（one-api 网关，v=78） ==============================
def call_deepseek(system_prompt: str, user_text: str, max_retries: int = 2) -> dict:
    """调用 one-api 网关（deepseek 轨，按 model 路由）。"""
    from models import call_llm
    return call_llm(system_prompt, user_text, model_id='deepseek-v4-flash', max_retries=max_retries)


def call_deepseek_suggest(esg_json: dict, company_type: str) -> dict:
    """DeepSeek 直调：内置双提示词 + 增强 JSON → 根因分析严格 JSON"""
    return call_deepseek_suggest_model(esg_json, company_type, 'deepseek-v4-flash')


def call_deepseek_suggest_model(esg_json: dict, company_type: str, model_id: str) -> dict:
    """LLM 直调（v=78 多模型）：内置双提示词 + 增强 JSON → 根因分析严格 JSON"""
    from models import call_llm
    system_prompt = load_file(SYSTEM_PROMPT_FILE)
    user_template = load_file(USER_PROMPT_FILE)
    user_text = (
        user_template
        + "\n\n【实际输入 esg_json】\n"
        + json.dumps({"company_type": company_type, "esg_json": esg_json}, ensure_ascii=False)
    )
    logger.info(f"📤 LLM 直调 [{model_id}]（系统提示词 {len(system_prompt)} 字符 + 用户提示词 {len(user_template)} 字符）")
    return call_llm(system_prompt, user_text, model_id=model_id)


# ============================== ADP 对话流（比赛展示·可选） ==============================
def call_adp_chat_streaming(text: str, visitor_id: str = None, timeout: int = 180) -> dict:
    """调用 ADP 对话流端点（SSE 流式）。返回 {"success", "text", "error", "events_count"}"""
    if not ADP_APP_KEY:
        return {"success": False, "text": "", "error": "ADP_APP_KEY 未配置"}

    body = {
        "RequestId": _uuid.uuid4().hex,
        "ConversationId": _uuid.uuid4().hex,
        "AppKey": ADP_APP_KEY,
        "VisitorId": visitor_id or f"esg-frontend-{_uuid.uuid4().hex[:8]}",
        "Contents": [{"Type": "text", "Text": text}],
        "Stream": "enable",
        "Incremental": True,
    }
    accumulated_text = ""
    events_count = 0
    final_error = None
    try:
        resp = requests.post(ADP_CHAT_ENDPOINT, json=body,
                             headers={"Accept": "text/event-stream"},
                             timeout=timeout, stream=True)
        if resp.status_code != 200:
            return {"success": False, "text": "", "events_count": 0,
                    "error": f"HTTP {resp.status_code}"}
        resp.encoding = 'utf-8'
        # 显式 UTF-8 解码（requests 默认 ISO-8859-1 会双重编码乱码）
        with resp:
            current_event = None
            pending_decoder = b''
            for raw_bytes in resp.iter_content(chunk_size=8192, decode_unicode=False):
                if not raw_bytes:
                    continue
                try:
                    chunk = raw_bytes.decode('utf-8')
                except UnicodeDecodeError:
                    pending_decoder += raw_bytes
                    continue
                if pending_decoder:
                    try:
                        pending_decoder += raw_bytes
                        chunk = pending_decoder.decode('utf-8')
                        pending_decoder = b''
                    except UnicodeDecodeError:
                        continue
                for line in chunk.split('\n'):
                    line = line.rstrip('\r')
                    if not line:
                        current_event = None
                        continue
                    if line.startswith("event:"):
                        current_event = line[6:].strip()
                    elif line.startswith("data:"):
                        data_str = line[5:].strip()
                        if not data_str:
                            continue
                        events_count += 1
                        try:
                            evt = json.loads(data_str)
                            evt_type = evt.get('Type') or current_event
                            if evt_type == 'text.delta':
                                accumulated_text += evt.get('Text', '') or ''
                            elif evt_type == 'content.added':
                                content = evt.get('Content', {}) or {}
                                accumulated_text += content.get('Text', '') or ''
                            elif evt_type == 'message.added':
                                msg = evt.get('Message', {}) or {}
                                if msg.get('Role') == 'assistant' and msg.get('Type') == 'reply':
                                    accumulated_text += msg.get('Text', '') or ''
                            elif evt_type == 'error':
                                final_error = (evt.get('Error') or {}).get('Message', '未知错误')
                        except json.JSONDecodeError:
                            pass
                    if current_event == 'response.completed':
                        break
                    if final_error:
                        break
                if current_event == 'response.completed':
                    break
                if final_error:
                    break
    except requests.exceptions.Timeout:
        return {"success": False, "text": accumulated_text, "events_count": events_count,
                "error": f"ADP 调用超时（{timeout}s）"}
    except Exception as e:
        logger.error(f"ADP 调用异常: {e}", exc_info=True)
        return {"success": False, "text": accumulated_text, "events_count": events_count,
                "error": str(e)}
    if final_error:
        return {"success": False, "text": accumulated_text, "events_count": events_count,
                "error": final_error}
    return {"success": True, "text": accumulated_text, "events_count": events_count, "error": None}


# ============================== 解析（兼容三种输出形态） ==============================
def parse_suggestion_text(text: str) -> dict:
    """解析返回文本为前端期望的建议 JSON：
    - 嵌套 {"Code":0, "Output":{"output":"<json>"}}
    - 单层评分结果 JSON
    - 自由 Markdown 文本（兜底 raw_text）
    """
    if not text:
        return None
    text = text.strip()
    try:
        outer = json.loads(text)
        if isinstance(outer, dict) and 'Output' in outer and isinstance(outer['Output'], dict):
            inner_str = outer['Output'].get('output', '')
            if inner_str:
                try:
                    return json.loads(inner_str)
                except json.JSONDecodeError:
                    return {'raw_inner': inner_str, 'code': outer.get('Code', 0)}
        if isinstance(outer, dict) and ('总分' in outer or '评分等级' in outer or '改进建议' in outer):
            return outer
    except json.JSONDecodeError:
        pass
    if text.startswith("```"):
        lines = text.split("\n")
        inner = "\n".join(lines[1:-1]) if lines[-1].strip().startswith("```") else "\n".join(lines[1:])
        try:
            return json.loads(inner.strip())
        except json.JSONDecodeError:
            pass
    return {"raw_text": text}


# ============================== 端点 ==============================
@app.route('/api/suggest', methods=['POST'])
def suggest():
    """根因分析 + 改进建议（智能体二）

    输入（JSON body）：
      - esg_json: dict      增强 JSON（含报告元数据+指标提取结果+评分增强数据）
      - company_type: str   'listed'|'unlisted'
    输出：
      - success / suggestion / raw_text / backend / error / duration_sec
    """
    start = time.time()
    try:
        req = request.get_json(force=True, silent=False)
        if not req:
            return jsonify({"success": False, "error": "请求体为空（非 JSON）"}), 400
        esg_json = req.get('esg_json') or req.get('extract_data') or req.get('user')
        company_type = req.get('company_type', 'listed')
        # v=78：模型选择（one-api 网关按 model 路由）
        model_id = req.get('model', 'deepseek-v4-flash')
        if not esg_json:
            return jsonify({"success": False, "error": "缺少 esg_json 字段"}), 400

        backend = ADP_BACKEND

        if backend == 'deepseek':
            # ---- 轨2：LLM 直调（one-api 网关，按 model 路由）----
            if not os.environ.get('LLM_GATEWAY_KEY'):
                return jsonify({"success": False, "error": "LLM_GATEWAY_KEY 未配置（one-api 分发 token）"}), 503
            try:
                result = call_deepseek_suggest_model(esg_json, company_type, model_id)
            except RuntimeError as e:
                return jsonify({"success": False, "error": str(e)}), 503
            suggestion = parse_suggestion_text(result['content'])
            duration = round(time.time() - start, 2)
            logger.info(f"📥 [{model_id}] 完成: tokens={result['tokens_used']} | {duration}s")
            return jsonify({
                "success": True,
                "suggestion": suggestion,
                "raw_text": result['content'][:8000],
                "backend": model_id,
                "tokens_used": result['tokens_used'],
                "model": result['model'],
                "company_type": company_type,
                "duration_sec": duration,
                "error": None,
            })

        # ---- 轨1：ADP 工作流（比赛展示）----
        if not ADP_APP_KEY:
            return jsonify({"success": False,
                            "error": "ADP_APP_KEY 未配置（切换 ADP_BACKEND=deepseek 或配置密钥）"}), 503
        user_text = json.dumps({"company_type": company_type, "esg_json": esg_json}, ensure_ascii=False)
        logger.info(f"📤 转发到 ADP 工作流（JSON 大小={len(user_text)} 字节）")
        result = call_adp_chat_streaming(text=user_text, visitor_id=f"esg-frontend-{company_type}", timeout=180)
        duration = round(time.time() - start, 2)
        suggestion = parse_suggestion_text(result['text']) if result['success'] else None
        logger.info(f"📥 [adp] 完成: events={result['events_count']} | {duration}s")
        return jsonify({
            "success": result['success'],
            "suggestion": suggestion,
            "raw_text": result['text'][:8000] if result['text'] else '',
            "backend": "adp",
            "events_count": result['events_count'],
            "company_type": company_type,
            "duration_sec": duration,
            "error": result['error'],
        })
    except Exception as e:
        logger.error(f"suggest 端点异常: {e}", exc_info=True)
        return jsonify({"success": False, "error": f"服务器异常: {str(e)}"}), 500


@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "service": "esg-agent2",
        "backend": ADP_BACKEND,
        "deepseek_key": bool(DEEPSEEK_API_KEY),
        "adp_key": bool(ADP_APP_KEY),
    })


@app.errorhandler(413)
def too_large(e):
    """v=83：请求体超限（MAX_CONTENT_LENGTH）→ 明确 413，避免被通用 except 包成 500"""
    return jsonify({"success": False, "error": "请求体过大（超过50MB上限）"}), 413


# ============================== 启动 ==============================
def main():
    parser = argparse.ArgumentParser(description='ESG智能体二·根因分析服务')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8083)))
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("🧠 ESG 智能体二 · 障碍度根因分析服务")
    logger.info("=" * 60)
    logger.info(f"后端模式: {ADP_BACKEND}（deepseek=自主可控 | adp=比赛展示）")
    logger.info(f"DeepSeek: {'已配置' if DEEPSEEK_API_KEY else '❌ 未配置'} | 模型: {DEEPSEEK_MODEL} | thinking: {DEEPSEEK_THINKING}")
    logger.info(f"ADP: {'已配置' if ADP_APP_KEY else '未配置（deepseek 模式无需）'}")
    logger.info(f"系统提示词: {os.path.basename(SYSTEM_PROMPT_FILE)} ({len(load_file(SYSTEM_PROMPT_FILE))}字符)")
    logger.info(f"用户提示词: {os.path.basename(USER_PROMPT_FILE)} ({len(load_file(USER_PROMPT_FILE))}字符)")
    logger.info(f"监听: {args.host}:{args.port}")
    logger.info("接口: GET /api/health | POST /api/suggest")
    logger.info("=" * 60)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == '__main__':
    main()
