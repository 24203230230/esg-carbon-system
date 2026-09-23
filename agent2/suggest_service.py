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

# v=100：检索层（Agent 1.5）—— 离线同行对标库（D 方案），为根因分析补「外部事实」
#   可选依赖：索引缺失 / 模块异常时必须静默降级，绝不阻塞原有分析链路
try:
    import peer_retrieval
except Exception as _pr_err:          # noqa: BLE001
    peer_retrieval = None
    print(f"[WARN] 检索层不可用（降级为纯报告文本诊断）: {_pr_err}")

# v=101：A 方案（联网检索企业公开信息）+ 确定性分析（适用性 / 改进收益）
#   同样是可选增强：任一模块不可用都只降级为空块，不影响主链路。
try:
    import web_search
except Exception as _ws_err:          # noqa: BLE001
    web_search = None
    print(f"[WARN] 联网检索层不可用（降级为仅离线对标）: {_ws_err}")

try:
    import materiality
except Exception as _mt_err:          # noqa: BLE001
    materiality = None
    print(f"[WARN] 适用性/收益量化模块不可用（降级为不量化）: {_mt_err}")

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


def retrieve_external_facts(esg_json: dict, use_peer: bool = True, use_web: bool = True) -> dict:
    """检索层（Agent 1.5）：三段式证据链。

    v=101 起聚合三个来源，顺序与系统提示词 v5.0 的「分工表」一致：
        ① 确定性分析（Python）  —— 该不该披露？改了能提升多少分？
        ② 企业公开信息（联网）  —— 这家企业自己做过什么
        ③ 同行对标库（离线）    —— 别的公司怎么写
    任一来源失败只丢该段，其余照常 —— 检索层是增强项，绝不影响主链路。
    """
    out = {
        "启用": False, "文本块": "", "事实": [], "统计": {}, "语料规模": 0,
        "联网": {}, "确定性分析": {}, "分段": {},
    }

    # ---- ① 确定性分析（纯 Python，本地计算，永不失败） ----
    mat_text = ""
    if materiality is not None:
        try:
            peer_idx = None
            if peer_retrieval is not None:
                try:
                    peer_idx = peer_retrieval.load_index()
                except Exception:               # noqa: BLE001
                    peer_idx = None
            mat = materiality.analyze(esg_json, peer_idx)
            if mat.get("启用"):
                mat_text = mat.get("文本块", "")
                out["确定性分析"] = {
                    "启用": True,
                    "总纲": mat.get("总纲", {}),
                    "统计": mat.get("统计", {}),
                }
                logger.info("🧮 确定性分析：%s" % mat.get("统计"))
            else:
                out["确定性分析"] = {"启用": False, "原因": mat.get("原因", "")}
        except Exception as e:                   # noqa: BLE001
            logger.error(f"确定性分析异常（降级）: {e}", exc_info=True)
            out["确定性分析"] = {"启用": False, "原因": str(e)}

    # ---- ② 企业公开信息（联网检索） ----
    web_text = ""
    if use_web and web_search is not None:
        try:
            web = web_search.build_web_facts(esg_json)
            web_text = web.get("文本块", "")
            out["联网"] = {
                "启用": web.get("启用", False),
                "原因": web.get("原因", ""),
                "统计": web.get("统计", {}),
            }
            if web.get("启用"):
                logger.info("🌐 联网检索：%s" % web.get("统计"))
            else:
                logger.info(f"🌐 联网检索未启用：{web.get('原因')}")
        except Exception as e:                   # noqa: BLE001
            logger.error(f"联网检索异常（降级）: {e}", exc_info=True)
            out["联网"] = {"启用": False, "原因": str(e), "统计": {}}
    elif not use_web:
        out["联网"] = {"启用": False, "原因": "本次请求已关闭", "统计": {}}
    else:
        out["联网"] = {"启用": False, "原因": "联网检索模块未加载", "统计": {}}

    # ---- ③ 同行对标库（离线） ----
    peer_res = {"启用": False, "原因": "未启用", "事实": [], "文本块": "", "统计": {}}
    if use_peer:
        if peer_retrieval is None:
            peer_res = {"启用": False, "原因": "检索模块未加载", "事实": [], "文本块": "", "统计": {}}
        else:
            try:
                peer_res = peer_retrieval.build_external_facts(esg_json)
                if peer_res.get("启用"):
                    st = peer_res.get("统计", {})
                    logger.info(
                        f"🔍 同行对标：语料 {peer_res.get('语料规模')} 家 | 命中指标 {st.get('命中指标')} "
                        f"| 同行业 {st.get('同行业识别')} | 文本块 {st.get('文本块字符数')} 字符"
                    )
                else:
                    logger.info(f"🔍 同行对标未启用：{peer_res.get('原因')}")
            except Exception as e:               # noqa: BLE001
                logger.error(f"同行对标异常（降级）: {e}", exc_info=True)
                peer_res = {"启用": False, "原因": str(e), "事实": [], "文本块": "", "统计": {}}
    else:
        peer_res = {"启用": False, "原因": "本次请求已关闭", "事实": [], "文本块": "", "统计": {}}

    # ---- 拼接（顺序必须是 ① ② ③，与提示词分工表一致） ----
    blocks = [b for b in (mat_text, web_text, peer_res.get("文本块", "")) if b]
    out["文本块"] = "\n\n\n".join(blocks)
    out["事实"] = peer_res.get("事实", [])
    out["统计"] = peer_res.get("统计", {})
    out["语料规模"] = peer_res.get("语料规模", 0)
    out["分段"] = {
        "确定性分析": bool(mat_text),
        "企业公开信息": bool(web_text),
        "同行对标": bool(peer_res.get("文本块")),
    }
    out["启用"] = any(out["分段"].values())
    if not out["启用"]:
        out["原因"] = "三段证据均不可用"
    logger.info("📚 三段证据链：%s | 合计 %d 字符" % (
        out["分段"], len(out["文本块"])))
    return out


def call_deepseek_suggest_model(esg_json: dict, company_type: str, model_id: str,
                                extra_context: str = "") -> dict:
    """LLM 直调（v=78 多模型）：内置双提示词 + 增强 JSON → 根因分析严格 JSON

    extra_context（v=100）：检索层产出的「外部事实块」，插在用户提示词之后、
    esg_json 之前 —— 让模型先看到同行事实，再看到本企业数据。
    """
    from models import call_llm
    system_prompt = load_file(SYSTEM_PROMPT_FILE)
    user_template = load_file(USER_PROMPT_FILE)
    user_text = user_template
    if extra_context:
        user_text += "\n\n" + extra_context
    user_text += (
        "\n\n【实际输入 esg_json】\n"
        + json.dumps({"company_type": company_type, "esg_json": esg_json}, ensure_ascii=False)
    )
    logger.info(f"📤 LLM 直调 [{model_id}]（系统提示词 {len(system_prompt)} 字符 + 用户提示词 "
                f"{len(user_template)} 字符 + 外部事实 {len(extra_context)} 字符）")
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

            # v=100：检索层（Agent 1.5）—— 规则式离线同行对标，给推理层补外部事实
            # v=101：扩为三段式（确定性分析 + 联网企业检索 + 离线同行对标）
            #   use_peer=false / use_web=false 可单请求关闭某段（便于改造前后 A/B 对比）
            use_peer = req.get('use_peer', True) is not False
            use_web = req.get('use_web', True) is not False
            facts = {"启用": False, "原因": "本次请求已关闭", "事实": [], "文本块": "",
                     "统计": {}, "联网": {}, "确定性分析": {}, "分段": {}}
            if use_peer or use_web:
                facts = retrieve_external_facts(esg_json, use_peer=use_peer, use_web=use_web)

            try:
                result = call_deepseek_suggest_model(esg_json, company_type, model_id,
                                                     extra_context=facts.get("文本块", ""))
            except RuntimeError as e:
                return jsonify({"success": False, "error": str(e)}), 503
            suggestion = parse_suggestion_text(result['content'])
            duration = round(time.time() - start, 2)
            logger.info(f"📥 [{model_id}] 完成: tokens={result['tokens_used']} | {duration}s")
            return jsonify({
                "success": True,
                "suggestion": suggestion,
                # v=103：不再按 8000 截断。v5.0 结构化建议单条常 600+ 字符（含企业特征/现状/
                # 同行对照/改进收益/证据来源），10 条可达 2 万字符；截断会让前端 JSON.parse
                # 失败并退化成「整坨 JSON 当正文」。上限 100000 仅防极端畸形输出。
                "raw_text": result['content'][:100000],
                "raw_text_len": len(result['content'] or ''),
                "backend": model_id,
                "tokens_used": result['tokens_used'],
                "model": result['model'],
                "company_type": company_type,
                "duration_sec": duration,
                # v=100 检索层产物：外部事实（含出处）供前端展示「证据来源」
                "外部事实": facts.get("事实", []),
                "检索层": {
                    "启用": facts.get("启用", False),
                    "原因": facts.get("原因", ""),
                    "语料规模": facts.get("语料规模", 0),
                    "统计": facts.get("统计", {}),
                    # v=101：三段式证据链的可用性与统计
                    "分段": facts.get("分段", {}),
                    "联网": facts.get("联网", {}),
                    "确定性分析": facts.get("确定性分析", {}),
                },
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
            # v=103：与 DeepSeek 轨保持一致，不再 8000 截断
            "raw_text": result['text'][:100000] if result['text'] else '',
            "raw_text_len": len(result['text'] or ''),
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
    peer = {"启用": False, "可用": False, "原因": "检索模块未加载"}
    if peer_retrieval is not None:
        try:
            peer = peer_retrieval.index_status()
        except Exception as e:                   # noqa: BLE001
            peer = {"启用": False, "可用": False, "原因": str(e)}
    web = {"启用": False, "可用": False, "原因": "联网检索模块未加载"}
    if web_search is not None:
        try:
            web = dict(web_search.provider_status(), 可用=True)
        except Exception as e:                   # noqa: BLE001
            web = {"启用": False, "可用": False, "原因": str(e)}
    mat = {"可用": materiality is not None}
    return jsonify({
        "status": "ok",
        "service": "esg-agent2",
        "build": "v110",   # v110：基准口径标注（materiality + peer_retrieval 文案带口径与样本量，判定与分数不变）
        "backend": ADP_BACKEND,
        "deepseek_key": bool(DEEPSEEK_API_KEY),
        "adp_key": bool(ADP_APP_KEY),
        "peer_retrieval": peer,
        "web_search": web,
        "materiality": mat,
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
