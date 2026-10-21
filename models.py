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
    # 其他 provider 名字一致，同样直通：glm-5.2 / kimi-k3
    # v=202：MiniMax 上游模型名**大小写敏感**（MiniMax-M3），前端 id 为小写 minimax-m3
    #   ⇒ 必须映射，否则上游报模型不存在。
    'minimax-m3': 'MiniMax-M3',
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
# v=202：实测 glm-5.2 / kimi-k3 / MiniMax-M3 上游均支持 json_object
JSON_MODE_MODELS = {'deepseek-v4-flash', 'deepseek-v4-pro', 'deepseek-v4-flash-vision-exp', 'glm-5.2',
                    'kimi-k3', 'minimax-m3'}


def get_gateway_config() -> tuple:
    """返回 (base_url, api_key)。one-api 网关统一配置。"""
    base_url = os.environ.get('LLM_GATEWAY_URL', 'http://esg-one-api:3000/v1')
    api_key = os.environ.get('LLM_GATEWAY_KEY', '')
    return base_url, api_key


# v=202：按模型名推断 provider —— 各厂商对 temperature / thinking 的支持面不同，
#   需要按厂商注入差异化参数（增量逻辑，不改动原有调用链）。
def _provider_of(model: str) -> str:
    m = (model or '').lower()
    if m.startswith('deepseek'):
        return 'deepseek'
    if m.startswith('glm'):
        return 'zhipu'
    if m.startswith('kimi') or m.startswith('moonshot'):
        return 'kimi'
    if m.startswith('minimax'):
        return 'minimax'
    return 'unknown'


# Kimi(Moonshot) 温度只接受固定值：开思考=1，关思考=0.6。
# 传 0 / 0.1 会被上游 400 拒绝（实测 "invalid temperature: only 0.6 is allowed"）。
# 本项目统一关闭思考 ⇒ 固定 0.6。（智谱/MiniMax/DeepSeek 无此限制）
KIMI_TEMPERATURE_THINKING_OFF = 0.6


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
        # v=116：0.1 → 0.0。抽取任务要的是"读原文→填结构"，不存在"需要多样性"的场景。
        # 背景（2026-09-25）：同一份报告两次运行，总分 5.7 vs 6.25、透明度维度分
        #   5.51 vs 6.98，差异 100% 来自提取 JSON（算分是纯函数）。
        # ⚠️ 温度 0 只能消除**采样**随机性，消不掉 GPU 浮点归约顺序差异（batch 方差）
        #   —— DeepSeek 也未提供 seed 参数。故此项是"降方差"，不是"保证确定"。
        #   真正的确定性靠下游加固：受控枚举 + 规则判定 + 证据强制（见 extraction_prompt.md）。
        "temperature": 0.0,
    }
    # v=99：DeepSeek V4.1 系列**思考模式默认开启**（effort=high），思维链经
    # reasoning_content 返回且与 content **共享 max_tokens 预算**。实测影响：
    #   同一 prompt  completion_tokens  23（deepseek-chat）→ 186（deepseek-v4-flash）
    #   生产批跑      JSON 解析失败率 ~3% → 17%（5/30，content 被腰斩）
    # 抽取任务是"读原文→填结构"，不需要思维链，显式关闭（实测 186 → 24，8 倍差）。
    # v=202：扩展到 GLM / Kimi / MiniMax —— 三家同样默认开思考，且实测均接受
    #   {"thinking":{"type":"disabled"}}（GLM/Kimi 关思考后 reasoning 归零、延迟降 4~5 倍）。
    #   未识别的 provider 不注入，避免透传未知字段。
    # ------------------------------------------------------------------
    # v=204（2026-10-21）★ 修正：智谱必须改用 reasoning_effort，不能发 thinking。
    #   现象：前端切 GLM 5.2 报「LLM返回非JSON格式」，日志为
    #         finish_reason=length / content_len=3（"\n{\n"）/ tokens=63136。
    #   定位过程（三组对照）：
    #     ① 直连智谱 open.bigmodel.cn + thinking=disabled → reasoning=0，JSON 正常 ✅
    #     ② 同一字段经 one-api 网关            → reasoning=508，content 只剩 3 字符 ❌
    #        ⇒ 字段在网关这一跳丢了：one-api 的智谱渠道(type=16)用 DTO 重建请求体，
    #          而该版本 DTO 只有 reasoning_effort、没有 thinking（二进制里 json 标签可证）。
    #     ③ 同一请求改发 reasoning_effort="none" 经网关 → reasoning=0、JSON 完整，
    #        且 prompt_tokens 与①完全一致 ⇒ 智谱适配器把它翻成了 thinking.disabled。
    #   ⚠️ 因此"关思考"的字段是**按厂商不同**的，不是一套通吃：
    #        deepseek / kimi / minimax → thinking.type=disabled（实测均生效）
    #        zhipu                     → reasoning_effort="none"
    #   为什么以前没发现：v=202 那次验证是**短 prompt**，思考开销小、content 仍能出全，
    #   一旦输入上万 token，思考会把 16384 的 max_tokens 吃干，content 被腰斩。
    prov = _provider_of(real_model)
    if prov == 'zhipu':
        payload["reasoning_effort"] = "none"
    elif prov in ('deepseek', 'kimi', 'minimax'):
        payload["thinking"] = {"type": "disabled"}
    # v=202：Kimi 温度特判（关思考只能 0.6）—— 必须放在 thinking 之后覆写
    if _provider_of(real_model) == 'kimi':
        payload["temperature"] = KIMI_TEMPERATURE_THINKING_OFF
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
                "model": data.get('model', model_id),
                # v=204：把结束原因透出给上层 —— 用于区分「模型输出语法错」与
                #   「输出被 max_tokens 腰斩」。二者都表现为 JSON 解析失败，但根因完全不同。
                "finish_reason": finish_reason,
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
