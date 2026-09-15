# -*- coding: utf-8 -*-
"""
models.py — 多模型支持（v=78）
================================
通过 one-api 网关统一接入多 LLM 渠道（DeepSeek/GLM/Kimi/MiniMax 等）。

架构：
  esg-extractor / esg-agent2
      → call_llm()（本文件）
      → http://esg-one-api:3000/v1（one-api 网关，按 model 字段自动路由）
      → 真实 provider 官方 API

环境变量：
  LLM_GATEWAY_URL   - one-api 网关地址（默认 http://esg-one-api:3000/v1）
  LLM_GATEWAY_KEY   - one-api 分发 token（用户在 one-api Web UI 生成）
"""
import os
import time
import logging

import requests

logger = logging.getLogger('llm')

# ============================== 模型列表（前端下拉同步） ==============================
DEFAULT_MODEL = 'deepseek-v4-flash'

# 模型映射：前端语义化名 → one-api 网关/上游 provider 真实模型名
# v=98：改为**直通**。原映射把 deepseek-v4-flash→deepseek-chat、
#   deepseek-v4-pro→deepseek-reasoner，而这两个名字在 DeepSeek 最新官方文档中
#   **已不再列出**（文档只列 deepseek-flash / deepseek-v4-pro）→ 潜在断供点。
#
# 实测（2026-09-12 经网关逐个探针调用）：
#   deepseek-v4-flash  → OK，上游返回 model=deepseek-flash（官方已路由到 V4.1-Flash）
#   deepseek-chat      → OK，上游同样返回 model=deepseek-flash（官方仍兼容旧名）
#   deepseek-v4-pro    → OK
#   deepseek-flash     → 403「该令牌无权使用模型」（网关 token 未勾选，非渠道缺失）
# ⇒ 直通 v4 名少一跳依赖、更贴近官方口径，且**无需改动 one-api 配置**。
#
# ⚠️ 官方推荐名 `deepseek-flash` 若要启用，需先在 one-api：
#    ① 渠道「模型」字段加入 deepseek-flash；② 分发 token 勾选该模型。
MODEL_MAPPING = {
    # 留空 = 直通：前端 id 即网关模型名，不再二次映射。
    # 其他 provider 名字一致，同样直通：glm-5.2 / glm-5v-turbo / kimi-k3 / minimax-m3
}

SUPPORTED_MODELS = [
    {'id': 'deepseek-v4-flash', 'label': 'DeepSeek V4 Flash', 'desc': '快速 · 默认'},
    {'id': 'deepseek-v4-pro',   'label': 'DeepSeek V4 Pro',   'desc': '深度推理'},
    {'id': 'deepseek-v4-flash-vision-exp', 'label': 'DeepSeek V4 Vision', 'desc': '多模态视觉'},
    {'id': 'glm-5.2',           'label': 'GLM 5.2',           'desc': '智谱'},
    {'id': 'kimi-k3',           'label': 'Kimi K3',           'desc': '月之暗面'},
    {'id': 'minimax-m3',        'label': 'MiniMax M3',        'desc': 'MiniMax'},
]

# 支持 JSON 输出模式的模型（one-api 透传 response_format）
# v=87：vision 模型按官方文档推测支持（多模态输出可能受限），先启用；不兼容会自动报错
JSON_MODE_MODELS = {'deepseek-v4-flash', 'deepseek-v4-pro', 'deepseek-v4-flash-vision-exp', 'glm-5.2'}


def get_gateway_config() -> tuple:
    """返回 (base_url, api_key)。one-api 网关统一配置。"""
    base_url = os.environ.get('LLM_GATEWAY_URL', 'http://esg-one-api:3000/v1')
    api_key = os.environ.get('LLM_GATEWAY_KEY', '')
    return base_url, api_key


# ============================== 统一 LLM 调用（one-api 网关） ==============================
def call_llm(system_prompt: str, user_text: str, model_id: str = None,
             max_retries: int = 2) -> dict:
    """调用 one-api 网关，按 model 字段路由到对应 provider。

    参数：
      system_prompt: 系统提示词
      user_text:     用户内容
      model_id:      模型 ID（默认 deepseek-v4-flash），one-api 网关自动路由
    返回：
      {"content": str, "tokens_used": int, "model": str}
    异常：
      RuntimeError（key 未配置 / 调用失败）
    """
    if not model_id:
        model_id = DEFAULT_MODEL
    # 校验模型在列表内（防任意注入）
    valid_ids = {m['id'] for m in SUPPORTED_MODELS}
    if model_id not in valid_ids:
        raise RuntimeError(f"不支持的模型: {model_id}（可选: {', '.join(sorted(valid_ids))}）")

    # 应用模型映射：前端语义化名 → one-api 渠道真实模型名
    real_model = MODEL_MAPPING.get(model_id, model_id)

    base_url, api_key = get_gateway_config()
    if not api_key:
        raise RuntimeError("LLM_GATEWAY_KEY 未配置（请在 one-api Web UI 生成分发 token 后写入 .env）")

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": real_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ],
        "max_tokens": 16384,
        "temperature": 0.1,
    }
    # v=99：DeepSeek V4.1 系列**思考模式默认开启**（effort=high），思维链经
    # reasoning_content 返回且与 content **共享 max_tokens 预算**。实测影响：
    #   同一 prompt  completion_tokens  23（deepseek-chat）→ 186（deepseek-v4-flash）
    #   生产批跑      JSON 解析失败率 ~3% → 17%（5/30，content 被腰斩）
    # 抽取任务是"读原文→填结构"，不需要思维链，显式关闭（实测 186 → 24，8 倍差）。
    # 仅对 DeepSeek 系生效，避免给 GLM/Kimi/MiniMax 透传未知字段。
    if real_model.startswith('deepseek'):
        payload["thinking"] = {"type": "disabled"}
    # JSON 输出：仅支持 json_mode 的模型透传 response_format（one-api 透传给上游）
    if model_id in JSON_MODE_MODELS:
        payload["response_format"] = {"type": "json_object"}

    last_error = None
    resp = None  # v=84：初始化，防止 except 分支引用未定义变量（UnboundLocalError 掩盖真错误）
    for attempt in range(max_retries + 1):
        try:
            logger.info(f"[LLM] {model_id} 调用（尝试 {attempt + 1}/{max_retries + 1}）→ {base_url}")
            resp = requests.post(url, headers=headers, json=payload, timeout=240)
            resp.raise_for_status()
            data = resp.json()
            content = data['choices'][0]['message']['content']
            tokens_used = data.get('usage', {}).get('total_tokens', 0)
            # v=99：截断可见化。截断会让 JSON 不完整（表现为"LLM返回非JSON格式"），
            # 只有落日志才能区分"模型输出语法错"与"输出被 max_tokens 腰斩"。
            finish_reason = data['choices'][0].get('finish_reason')
            if finish_reason == 'length':
                logger.warning(
                    f"[LLM] ⚠️ 输出被 max_tokens 截断（finish_reason=length，"
                    f"content_len={len(content)}），JSON 可能不完整"
                )
            logger.info(f"[LLM] {model_id} 返回成功，tokens={tokens_used}，content_len={len(content)}")
            return {
                "content": content,
                "tokens_used": tokens_used,
                "model": data.get('model', model_id)
            }
        except requests.exceptions.RequestException as e:
            last_error = e
            # 尝试读取 one-api 网关返回的错误详情
            err_detail = ''
            try:
                err_detail = resp.text[:300]
            except Exception:
                pass
            logger.warning(f"[LLM] {model_id} 调用失败（第{attempt+1}次）: {e} {err_detail}")
            if attempt < max_retries:
                time.sleep(2 ** attempt)
        except (KeyError, ValueError) as e:
            last_error = f"响应解析失败: {e}"
            # v=84：resp 可能为 None（异常发生在 post 之前），防御访问
            err_body = resp.text[:300] if resp is not None else '(请求未发出)'
            logger.error(f"[LLM] {model_id} 响应格式异常: {err_body}")
            break
    raise RuntimeError(f"[LLM] {model_id} 调用失败（重试{max_retries+1}次）: {last_error}")
