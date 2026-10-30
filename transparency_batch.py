# -*- coding: utf-8 -*-
"""透明度维度补抽（v205，E 方案）
================================================================
为什么只补抽「透明度」这一个维度：
  2026-10-30 的 A/C/E 三组共 45 次完整抽取实验（详见
  `_perf/shadow/复现性改造_三组实验结论_20261030.md`）显示：

  · A 组（整篇一次抽 42 项，即线上现状）
      → 「企业内部碳管理」「供应链碳管理」两个维度 5 轮几乎不动，
        方差**几乎全部集中在「透明度」这 10 项受控枚举判断上**
        （牧原 2.7~4.8、铜牛 1.4 极差）
  · C 组（按 4 维度全拆批）
      → 透明度治好了（3.2×5 稳定），但供应链在牧原上**整批崩塌**
        （12 项同时判未披露，维度分 0.1），极差反而恶化到 1.54
  · E 组（本方案：主抽 42 项 + 透明度单独补抽覆盖）
      → 牧原 0.47 → 0.21、铜牛 0.74 → 0.38，平均极差 0.45 → 0.24（−46%）

  ⇒ 只把「病根维度」拆出来单独抽，其余 32 项保持原样，
    既拿 C 的收益，又不引入 C 的整批崩塌风险。成本 +1 次调用（+100%），不是 C 的 +300%。

成本为什么不会翻倍：
  报告全文放在提示词**前面**且与主抽逐字相同，批次指令只追加在末尾
  ⇒ 命中上下文缓存（prompt prefix cache），实测单批约 3 秒。

设计约束（严格遵守"不碰原逻辑"）：
  1. **只覆盖、不合并**：补抽结果直接替换主抽的「透明度」维度，不做逐项融合；
  2. **失败一律保底**：补抽返回空 / 结构不对 / 抛异常 ⇒ 保留主抽结果，绝不让整单失败；
  3. **可关停**：环境变量 TRANSPARENCY_RESAMPLE=0 立即回到 v204b 行为；
  4. **不改动调用方**：本模块只返回 (指标字典, 未覆盖列表)，写入由调用方决定。
================================================================
"""
import json
import logging
import re

logger = logging.getLogger('transparency_batch')

# 透明度维度下的 10 项指标（与提示词 5.1 节完全同构）
TRANSPARENCY_SUBS = {
    '披露范围与完整性': ['碳排放披露范围', '供应链碳披露层级', '碳资产与交易信息披露比例'],
    '披露质量与可信度': ['第三方核查覆盖率', 'ESG评级结果及行业排名',
                  'ISO14064/ISO50001认证', '信息披露频次'],
    '披露合规性': ['监管强制披露要求符合性', '交易所ESG披露指引符合性', '历史信息披露更正次数'],
}

DIM = '透明度'


def _batch_spec(subs) -> str:
    """生成该批的 JSON 骨架文本，给模型做输出模板（必须带维度名这一层）。"""
    lines = []
    for sub, leaves in subs.items():
        leaf_lines = ',\n'.join(
            '            "%s": {"值": "", "单位": "", "年份": "", "原文": ""}' % l for l in leaves)
        lines.append('          "%s": {\n%s\n          }' % (sub, leaf_lines))
    return ('{\n'
            '    "指标提取结果": {\n'
            '      "%s": {\n'
            '        "指标": {\n%s\n        },\n'
            '        "未覆盖指标": []\n'
            '      }\n'
            '    }\n}') % (DIM, ',\n'.join(lines))


def build_user_text(report_text: str, filename: str) -> str:
    """构造补抽的 user prompt。

    ⚠️ 前两段必须与主抽**逐字一致**（`## 文件名` / `## 报告内容` 及围栏），
       否则前缀缓存不命中，成本会按全价重算一遍全文。
    """
    return """## 文件名
%s

## 报告内容
```
%s
```

## 本次任务范围（最高优先级，覆盖提示词中的完整结构要求）
这是一次**针对性补抽**：本次**只输出「%s」这一个维度**，
其余三个维度一律不要输出、不要留空占位。
其余规则（诚实原则、原文可追溯、受控枚举、单位统一）与系统提示词完全一致。

## 本批输出 JSON 结构（严格照此层级，不要增删字段）
```json
%s
```

## ⚠️ 未披露的写法（违反此项会导致评分错误，务必遵守）
若某字段报告确实未涉及：
- `值` 填 `未披露`；
- `原文`、`单位`、`年份` **一律留空字符串** `""`；
- **严禁**把与该指标无关的句子填进 `原文` 充数。
  （例如「ESG评级结果及行业排名」未披露时，不得把"获某评选产品责任奖"写进 `原文`。）
  原因：下游评分会把"值为未披露但原文有内容"判为「简单提及」并加分，
  填无关内容等于凭空加分。宁可全空，不可凑数。

## ⚠️ 部分披露要写全
本维度大量指标是「披露程度」型判断，标准答案常常是**部分披露**。
例如「碳排放披露范围」的正确答案可能是「披露范围一和范围二，未披露范围三」——
**必须把已披露的部分完整写出**，不要因为句子里出现"未披露"就整条填 `未披露`。
""" % (filename, report_text, DIM, _batch_spec(TRANSPARENCY_SUBS))


def _parse_json(content: str):
    """解析 LLM 返回的 JSON；容忍 Markdown 代码围栏（MiniMax 会包 ```json）。"""
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r'^\s*```(?:json)?\s*|\s*```\s*$', '', content.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def _extract_dim(data: dict):
    """从补抽结果里取出透明度的 {指标: {...}, 未覆盖指标: [...]}。

    容忍三种模型输出形态：
      A. {"指标提取结果": {"透明度": {"指标": {...}}}}   ← 期望形态
      B. {"透明度": {"指标": {...}}}
      C. {"指标": {...}}                                ← 省掉维度名
    """
    if not isinstance(data, dict):
        return None
    ext = data.get('指标提取结果') or data.get('extracted') or data
    if not isinstance(ext, dict):
        return None
    node = ext.get(DIM)
    if isinstance(node, dict) and isinstance(node.get('指标'), dict):
        return node
    if isinstance(ext.get('指标'), dict):
        return {'指标': ext['指标'], '未覆盖指标': ext.get('未覆盖指标') or []}
    return None


def resample(report_text: str, filename: str, system_prompt: str,
             model_id: str = 'deepseek-v4-flash') -> dict:
    """补抽透明度维度。

    返回 {'指标': {...}, '未覆盖指标': [...]}；任何失败返回 None（调用方保留主抽）。
    """
    try:
        from models import call_llm
        resp = call_llm(system_prompt, build_user_text(report_text, filename),
                        model_id=model_id, max_retries=1)
        data = _parse_json(resp.get('content', ''))
        node = _extract_dim(data)
        if not node:
            logger.warning('透明度补抽返回空或结构不符，保留主抽结果')
            return None
        n = sum(len(v) for v in node['指标'].values() if isinstance(v, dict))
        logger.info(f'🔁 透明度补抽成功：{n} 项（tokens={resp.get("tokens_used")}）')
        if n == 0:
            # 一项都没抽到 ⇒ 视为失败，避免用空维度把主抽的好结果覆盖掉
            logger.warning('透明度补抽 0 项，保留主抽结果')
            return None
        return node
    except Exception as e:                          # noqa: BLE001
        logger.warning(f'透明度补抽异常（保留主抽结果）：{e}')
        return None
