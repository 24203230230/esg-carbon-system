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

SUPPORTED_MODELS = [
    {'id': 'deepseek-v4-flash', 'label': 'DeepSeek V4 Flash', 'desc': '快速 · 默认'},
    {'id': 'deepseek-v4-pro',   'label': 'DeepSeek V4 Pro',   'desc': '深度推理'},
    {'id': 'glm-5.2',           'label': 'GLM 5.2',           'desc': '智谱'},
    {'id': 'glm-5v-turbo',      'label': 'GLM 5V Turbo',      'desc': '多模态'},
    {'id': 'kimi-k3',           'label': 'Kimi K3',           'desc': '月之暗面'},
    {'id': 'minimax-m3',        'label': 'MiniMax M3',        'desc': 'MiniMax'},
]

# 支持 JSON 输出模式的模型（one-api 透传 response_format）
JSON_MODE_MODELS = {'deepseek-v4-flash', 'deepseek-v4-pro', 'glm-5.2', 'glm-5v-turbo'}


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

    base_url, api_key = get_gateway_config()
    if not api_key:
        raise RuntimeError("LLM_GATEWAY_KEY 未配置（请在 one-api Web UI 生成分发 token 后写入 .env）")

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ],
        "max_tokens": 16384,
        "temperature": 0.1,
    }
    # JSON 输出：仅支持 json_mode 的模型透传 response_format（one-api 透传给上游）
    if model_id in JSON_MODE_MODELS:
        payload["response_format"] = {"type": "json_object"}

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            logger.info(f"[LLM] {model_id} 调用（尝试 {attempt + 1}/{max_retries + 1}）→ {base_url}")
            resp = requests.post(url, headers=headers, json=payload, timeout=240)
            resp.raise_for_status()
            data = resp.json()
            content = data['choices'][0]['message']['content']
            tokens_used = data.get('usage', {}).get('total_tokens', 0)
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
            logger.error(f"[LLM] {model_id} 响应格式异常: {resp.text[:300]}")
            break
    raise RuntimeError(f"[LLM] {model_id} 调用失败（重试{max_retries+1}次）: {last_error}")
