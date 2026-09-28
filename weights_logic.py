#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESG碳治理能力评分计算模块（Python 确定性计算，无 LLM 参与）
================================================================
实现评分体系 v3.2 全部公式（公式1-10）：
  1. 完整度分（0-3）：定量=3 / 定性=2 / 简单提及=1 / 未披露=0
  2. 质量分（0-3）：多年=3 / 单年=2 / 仅文字=1 / 无=0
  3. 基础分（0-6）= 完整度 + 质量
  4. 对标分（0-4）：5档对标（×1.2/×1.0/×0.8/×0.6），均值<0.5特殊规则，均值缺失兜底=2
  5. L4得分（0-10）= 基础分 + 对标分
  6. 维度得分（0-10）= Σ(维度内L4得分×全局权重) / Σ(维度内全局权重)
  7. 总分（0-10）= Σ(42个L4得分×全局权重)，保留2位小数
  8. 障碍度（0-权重）= (1 − L4得分/10) × 全局权重，保留4位小数
  9. 等级映射：标杆9-10/先进7-8.9/发展中5-6.9/基础3-4.9/初始1-2.9/空白0-0.9
  10. 失分类型判定：完整度≤1=完整度失分 / 质量≤1=质量失分 / 对标≤1=对标失分

非上市版（company_type=unlisted）：
  - 有公开证据：标准5档对标（同上市版）
  - 无公开证据：取全市场中位数得分
"""
import json
import logging
import re
import statistics
from pathlib import Path

logger = logging.getLogger('weights_logic')

# ============================== 常量 ==============================
# v119（C1）：繁简归一 —— 报告可能是繁体，而本模块全部判定词是简体中文。
#   不归一时，繁体的「未揭露」「未提及」不匹配简体枚举 ⇒ 被判成"有实质内容"，
#   该指标白拿「完整度1 + 质量1」。证据校验侧实测：归一后命中率 0.021 → 0.800。
#   依赖缺失时**降级为原样返回**，绝不因此报错（保证任何环境都能跑）。
try:
    from zhconv import convert as _zh_convert
except Exception:                                   # noqa: BLE001
    _zh_convert = None


def _zh_norm(s) -> str:
    """繁体 → 简体；英文/数字/空值原样；无 zhconv 依赖时原样返回。"""
    if s is None:
        return ''
    t = str(s)
    if not t or _zh_convert is None:
        return t
    try:
        return _zh_convert(t, 'zh-cn')
    except Exception:                               # noqa: BLE001
        return t


WEIGHTS_PATH = Path(__file__).parent / 'weights.json'

# 4 个维度与其下 L3 子域映射（用于维度得分聚合）
DIM_SUB_MAP = {
    "企业内部碳管理": ["碳排放", "环境治理", "能源消耗与节能方案"],
    "供应链碳管理": ["供应商碳管理", "供应链协同减排", "供应链风险管控"],
    "透明度": ["披露范围与完整性", "披露质量与可信度", "披露合规性"],
    "金融化情况": ["碳金融", "碳资产管理"],
}

# v=84（M4）：LLM 输出维度名变体 → 标准维度名（防止变体静默归入错误维度导致分数错位）
DIM_NAME_NORMALIZE = {
    "企业内部碳管理": "企业内部碳管理", "内部碳管理": "企业内部碳管理", "企业内部管理": "企业内部碳管理",
    "内部管理": "企业内部碳管理", "企业碳管理": "企业内部碳管理",
    "供应链碳管理": "供应链碳管理", "供应链管理": "供应链碳管理", "供应链": "供应链碳管理",
    "供应商碳管理": "供应链碳管理",
    "透明度": "透明度", "透明度披露": "透明度", "披露透明度": "透明度", "信息披露": "透明度",
    "碳金融化": "金融化情况", "金融化情况": "金融化情况", "碳金融": "金融化情况", "碳金融转化": "金融化情况",
    "碳资产管理": "金融化情况",
}

def normalize_dim_name(dim_name: str) -> str:
    """维度名归一化（v=84）：LLM 变体 → 标准名；无法识别时返回原值（交给兜底）"""
    if not dim_name:
        return dim_name
    return DIM_NAME_NORMALIZE.get(str(dim_name).strip(), str(dim_name).strip())

# 多年迹象关键词（质量分3档判定）
# v=116 硬化：移除 "对比 / 趋势 / 增长 / 下降 / ~ ～ 至 — -" 这批**过于宽泛**的词与符号。
#   原实现里原文只要出现一个 "-"（表格线、破折号、数字区间都算）就判「多年」、
#   质量分 2→3 跳档，是评分漂移的主要放大器之一
#   （L4 跳 3 分 × 6.89% 权重 × 2 项 ≈ 总分 ±0.41，与实测漂移量级吻合）。
#   现改为**结构化判定**：文本中出现 ≥2 个不同的四位年份，或出现明确的
#   "逐年 / 同比 / 较上"等提示词，才认定为多年。
# v119（C1）：补英文等价式（英文报告的值可能是英文写法）。
#   匹配时统一 lower，故此处一律小写。
MULTI_YEAR_KEYWORDS = [
    "逐年", "连续", "多年", "近三年", "近五年", "同比", "较上", "相比上年", "与上年",
    # 英文等价式（英文报告 / 英文值）
    "year-on-year", "year over year", "yoy", "consecutive", "multi-year",
    "multi year", "over the past", "compared with last year",
    "compared to last year", "three-year", "five-year", "annual comparison",
]

# v=116："表示实质上没有披露"的表述集合。这些写法语义相同但字面不同，
#   原实现会把「未披露（报告未提供…）」判成定性披露（完整度2 / 质量1），
#   而简写「未披露」只拿 0 / 0 ⇒ 同一指标基础分 0↔3 跳档。
#   统一归为「无值」可显著收敛同一指标的跨轮次波动。
UNDISCLOSED_PAT = re.compile(
    r'(未披露|未提及|未找到|未明确|未涉及|未提供|未涵盖|未单独披露|未公布|未公开'
    r'|没有披露|无相关(?:信息|内容|表述|数据)?|无法获取|未识别|未体现)'
    # v119（C1）：用词变体（字形简体但用词不同，zhconv 转换无效，须显式列出）
    r'|(未揭露|未公示|未公告|未统计|暂未披露|暂未提及|未开展相关披露)'
    # v119（C1）：英文等价式（英文报告的值可能是英文写法）
    r'|(?:(?:not|no|non|without)[\s\-]*(?:disclosed|mention(?:ed|ing)?|report(?:ed|ing)?'
    r'|provid(?:ed|ing)|available|applicable|data|information|record(?:ed)?))'
    r'|undisclosed',
    re.IGNORECASE,
)
# 转折/肯定词：出现即说明该表述含实质内容（如"未明确…，但已建立…"），不算未披露
AFFIRM_PAT = re.compile(
    r'(但|已建立|已通过|已获得|已制定|已完成|已开展|已实施|已有|通过了|获得了|正在推进|已披露)'
    # v119（C1）：英文等价式
    r'|(?:\bbut\b|\bhowever\b|\byet\b'
    r'|has (?:established|obtained|formulated|completed|launched|implemented)'
    r'|have (?:established|obtained|formulated|completed|launched|implemented))',
    re.IGNORECASE,
)
# v=116c：补齐「表示没有披露」的**短字面量占位符**全集（与 UNDISCLOSED_PAT 对齐）。
#   背景：模型在未披露类指标上，除了填 "未披露"，还会填 "未提及/未找到/未明确"
#   等字面量；若只挡 "未披露"，这些等价写法会让「原文」被判为『有内容』，
#   使该指标白拿完整度1+质量1 分。牧原实测：4 个指标（含两个 0.0689 最高权重）
#   因此把透明度维度从 1.86 抬到 3.12，总分跳 0.12~0.66。
#   注：这里**只列纯占位符**，不含元描述长句（如"报告未提及…"），故对历史数据
#   保持零影响（与 v2-lite 同口径）。
EMPTY_VALUES = (
    '', '未披露', '未提及', '未找到', '未明确', '未涉及', '未提供', '未涵盖',
    '未单独披露', '未公布', '未公开', '未识别', '未体现', '没有披露',
    '无', '没有', '暂无', '不涉及', '不适用', '—', '-', 'N/A', 'n/a',
    # v119（C1）：**用词变体**（字形是简体、但用词与大陆不同）。
    #   ⚠️ zhconv 只做「字形」转换，不做「用词」转换 —— 「未揭露」四字繁简同形，
    #   归一后仍是「未揭露」，故必须在此显式列出。实测模型确实会填「未揭露」。
    '未揭露', '未公示', '未公告', '未统计', '暂未披露', '暂未提及',
    '无相关资料', '暂无相关信息', '未披露相关信息',
    # v119（C1）：英文等价式与常见变体（比较前统一 lower + 去首尾空白）
    'not disclosed', 'not mentioned', 'not reported', 'not provided',
    'not available', 'not applicable', 'not found', 'not specified',
    'no data', 'no information', 'no relevant information', 'no disclosure',
    'none', 'nil', 'null', 'na', 'undisclosed', 'unavailable',
    '--', '–',
)

# v119（C1）：占位符判定 —— 空白归一 + 大小写归一，兼容英文写法。
#   注意：**只判"纯占位符字面量"**，不启用 _is_undisclosed —— 历史数据里
#   「原文」为长句元描述（"报告未提及…"）的占很高比例，一并归零会改变既有评分。
_EMPTY_LOWER = {str(v).strip().lower() for v in EMPTY_VALUES}


def _is_empty_value(s) -> bool:
    """是否为空值 / 占位符（v119：兼容英文与大小写；空白视为空）。"""
    if s is None:
        return True
    t = str(s).strip()
    if not t:
        return True
    if t in EMPTY_VALUES:
        return True
    return t.lower() in _EMPTY_LOWER


def _is_undisclosed(val: str) -> bool:
    """值字段是否属于「实质上没有披露」。
    含具体数字的表述不算（有量化信息）；带肯定性转折的也不算（有实质内容）。
    v119（C1）：入口做繁简归一，使繁体「未揭露」等价于简体「未披露」。
    """
    val = _zh_norm(val)
    if not val:
        return False
    if re.search(r'\d', val):
        return False
    if not UNDISCLOSED_PAT.search(val):
        return False
    return not AFFIRM_PAT.search(val)


def _value_present(val: str) -> bool:
    """值字段是否构成「实质披露」—— v=116 统一入口（完整度分与质量分共用）。

    与原实现的差别仅在：把"带解释的未披露表述"也归为无值，消除同义不同判。
    正常披露（含数字 / 含实质文字 / 带转折的自述）行为完全不变。
    v119（C1）：占位符判定改走 `_is_empty_value`（兼容英文与大小写）。
    """
    if _is_empty_value(val):
        return False
    return not _is_undisclosed(val)


def _multi_year_evidence(text: str) -> bool:
    """是否具备「多年数据」证据（v=116 结构化判定）。

    判定顺序：① 文本含 ≥2 个不同的四位年份 → 是；② 含"逐年/同比/较上"等提示词 → 是。
    单个年份（如"2025年排放量：X"）**不算** —— 那只是单年数据。
    v119（C1）：先繁简归一并统一小写，使英文提示词（year-on-year / consecutive）可命中。
    """
    text = _zh_norm(text)
    if len(set(re.findall(r'(?<!\d)(20\d{2})(?!\d)', text or ''))) >= 2:
        return True
    low = (text or '').lower()
    return any(kw in low for kw in MULTI_YEAR_KEYWORDS)

# ============================== 数据加载 ==============================
def load_weights() -> dict:
    """加载 weights.json（权重+行业基准库+中位数+评分规则）

    v=84：文件缺失/损坏时不崩，返回空结构（评分走 0 权重兜底，提取链路不受影响）
    """
    try:
        with open(WEIGHTS_PATH, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"⚠️ weights.json 不存在（{WEIGHTS_PATH}），使用空权重兜底")
        return {"全局权重": {}, "行业基准库": {}, "全市场中位数": {}}
    except json.JSONDecodeError as e:
        logger.error(f"❌ weights.json 解析失败: {e}，使用空权重兜底")
        return {"全局权重": {}, "行业基准库": {}, "全市场中位数": {}}

WEIGHTS = load_weights()
GLOBAL_WEIGHTS = WEIGHTS.get('全局权重', {})
INDUSTRY_BASE = WEIGHTS.get('行业基准库', {})
MEDIANS = WEIGHTS.get('全市场中位数', {})

# ============================== 公式1：完整度分 ==============================
def calc_completeness(node: dict) -> int:
    """完整度分（0-3）：Python 自判（无"数据类型"字段）
    - 3：定量披露（值含数字）
    - 2：定性披露（值有文字）
    - 1：简单提及（值空，但原文有内容）
    - 0：未披露（全空）
    """
    if not isinstance(node, dict):
        return 0
    # v119（C1）：先繁简归一（简体输入为恒等变换 ⇒ 历史中文数据零影响）
    val = _zh_norm(node.get('值') or '').strip()
    raw = _zh_norm(node.get('原文') or '').strip()

    # v=116：统一走 _value_present —— 把「未披露（报告未提供…）」这类带解释的
    #   同义表述也归为无值（原先会被判成定性披露 2 分，造成同指标跨轮次跳档）。
    #   正常披露、以及"未明确…，但已建立…"这类含实质内容的表述行为不变。
    val_has = _value_present(val)
    # v=116b：「原文」也归一化占位符 —— 证据强制规则下，模型会把「原文」填成
    #   "未披露"/"无"（占位）或留空，两种写法语义等价。原实现只判 `not raw`，
    #   于是 值=未披露 时，「原文=未披露」判 1 分、「原文="" 」判 0 分，
    #   单指标差 2 分 ⇒ 实测放大成总分 3 分跳变（红豆 5.32 ↔ 2.33）。
    #   ⚠️ 此处**只判字面量占位符**，**不**启用 _is_undisclosed：
    #   历史数据中「原文」为长句元描述（"报告未提及XXX"）占 48.6%，若一并归零
    #   会大幅改变既有评分（行业基准库需重算）；只判字面量则影响面实测为 0。
    #   v119（C1）：判定改走 _is_empty_value（英文/繁体/大小写等价形式一并挡住）。
    raw_has = not _is_empty_value(raw)

    if not val_has and not raw_has:
        return 0  # 全空 → 未披露
    if val_has and re.search(r'\d', val):
        return 3  # 有数字 → 定量
    if val_has:
        return 2  # 有文字 → 定性
    return 1 if raw_has else 0  # 仅原文 → 简单提及


# ============================== 公式2：质量分 ==============================
def calc_quality(node: dict) -> int:
    """质量分（0-3）：
    - 3：多年连续数据（年份含范围 或 原文含多年迹象 且 有数值）
    - 2：单年数据（年份非空）
    - 1：仅文字描述（有原文无数值）
    - 0：无任何披露
    """
    if not isinstance(node, dict):
        return 0
    # v119（C1）：先繁简归一（简体输入为恒等变换 ⇒ 历史中文数据零影响）
    val = _zh_norm(node.get('值') or '').strip()
    year = _zh_norm(node.get('年份') or '').strip()
    raw = _zh_norm(node.get('原文') or '').strip()

    val_has = _value_present(val) and bool(re.search(r'\d', val))
    year_has = _value_present(year)
    # v=116b：原文只判字面量占位符（与 calc_completeness 同口径）
    # v119（C1）：改走 _is_empty_value（英文/繁体/大小写等价形式一并挡住）
    raw_has = not _is_empty_value(raw)

    if not val_has and not year_has and not raw_has:
        return 0  # 无任何披露
    if year_has and _multi_year_evidence(year):
        return 3  # 年份字段本身含多年证据
    if val_has and _multi_year_evidence(raw + ' ' + val):
        return 3  # 原文/值含多年证据
    if year_has:
        return 2  # 单年数据
    if raw_has:
        return 1  # 仅文字描述
    return 0


# ============================== 公式3：基础分 ==============================
def calc_basic_score(node: dict) -> int:
    """基础分（0-6）= 完整度 + 质量"""
    return calc_completeness(node) + calc_quality(node)


# ============================== 公式4：对标分 ==============================
def calc_benchmark_score(basic: float, mean: float) -> int:
    """对标分（0-4）：5档对标
    - 基础分 ≥ M×1.2 → 4（显著领先）
    - M ≤ 基础分 < M×1.2 → 3（小幅领先）
    - M×0.8 ≤ 基础分 < M → 2（接近平均）
    - M×0.6 ≤ 基础分 < M×0.8 → 1（小幅落后）
    - 基础分 < M×0.6 → 0（显著落后）
    特殊情况：
    - M < 0.5（行业几乎无人披露）→ 基础分>0得3，基础分=0得1
    - M 缺失 → 兜底 2（中性）
    """
    if mean is None:
        return 2  # 均值缺失兜底

    # 均值接近0的特殊处理（避免失真）
    if mean < 0.5:
        return 3 if basic > 0 else 1

    if basic >= mean * 1.2:
        return 4
    if basic >= mean:
        return 3
    if basic >= mean * 0.8:
        return 2
    if basic >= mean * 0.6:
        return 1
    return 0


# ============================== 公式5：L4得分 ==============================
def calc_l4_score(basic: float, benchmark: int) -> float:
    """L4得分（0-10）= 基础分 + 对标分"""
    return round(basic + benchmark, 2)


# ============================== 行业查询与降级 ==============================
def resolve_industry_mean(industry: str, indicator: str):
    """查行业均值，含降级规则：
    - 行业不在列表 → 用全市场均值
    - 行业样本量 < 10 → 用全市场均值（用户确认的容错规则）
    返回 (mean, base_source)
    """
    # 规范行业名（去掉"市""行业"等后缀）
    industry = str(industry or '').strip().replace('行业', '').replace('市', '')
    # 全市场均值 key（知识库中带【】符号）
    all_market = INDUSTRY_BASE.get('【全市场均值】') or INDUSTRY_BASE.get('全市场均值') or {}

    if industry in INDUSTRY_BASE:
        ind_data = INDUSTRY_BASE[industry]
        sample_n = ind_data.get('样本数', 0)
        means = ind_data.get('指标均值', {})
        if sample_n >= 10 and indicator in means:
            return means[indicator], '行业均值'
        # 样本不足 → 降级全市场
        fallback = all_market.get('指标均值', {}).get(indicator)
        return fallback, '全市场均值(样本<10降级)'

    # 行业不在列表 → 全市场
    fallback = all_market.get('指标均值', {}).get(indicator)
    return fallback, '全市场均值(行业不在列表)'


def resolve_baseline_n(industry: str, base_source: str = '') -> int:
    """返回 base_source 所对应的基准样本量（v110 B-lite：让「行业均值」能带上自己的样本量）。

    - base_source == '行业均值' → 该行业在 weights.json 里的样本数
    - 其余（降级到全市场）      → 全市场样本数
    取不到时返回 0（调用方据此省略样本量标注，不影响任何判定）。
    """
    try:
        ind = str(industry or '').strip().replace('行业', '').replace('市', '')
        if str(base_source or '') == '行业均值' and ind in INDUSTRY_BASE:
            return int((INDUSTRY_BASE[ind] or {}).get('样本数', 0) or 0)
        am = INDUSTRY_BASE.get('【全市场均值】') or INDUSTRY_BASE.get('全市场均值') or {}
        return int((am or {}).get('样本数', 0) or 0)
    except Exception:                                 # noqa: BLE001
        return 0


# ============================== 非上市版：中位数 ==============================
def resolve_unlisted_score(indicator: str, node: dict, has_evidence: bool) -> dict:
    """非上市版评分：
    - 有公开证据 → 标准评分（同上市版）
    - 无公开证据 → 取全市场中位数（基础分），对标分=中位数对标
    """
    if has_evidence:
        return {'基础分': calc_basic_score(node), '来源': '有公开证据(标准评分)'}
    median = MEDIANS.get(indicator)
    return {'基础分': round(median, 2) if median is not None else 0, '来源': '无公开证据(取全市场中位数)'}


# ============================== 主入口：单指标评分 ==============================
def score_indicator(indicator: str, node: dict, industry: str, company_type: str = 'listed') -> dict:
    """对单个 L4 指标完整评分，返回含全部中间结果的字典"""
    result = {
        '指标': indicator,
        '值': str(node.get('值') or ''),
        '原文': str(node.get('原文') or '')[:120],
        '权重': GLOBAL_WEIGHTS.get(indicator, 0),
    }

    # 行业均值（含降级）
    mean, base_source = resolve_industry_mean(industry, indicator)
    result['行业均值'] = mean
    result['基准来源'] = base_source
    # v110 B-lite：基准样本量（与「行业均值」同源，供 agent2 标注口径用；不参与任何计算）
    result['基准样本数'] = resolve_baseline_n(industry, base_source)

    # ------------------------------------------------------------------
    # v112：行业「同口径」对标值 —— **纯展示派生字段，不参与任何打分与判定**
    # ------------------------------------------------------------------
    # 为什么需要它：图表原先拿「行业均值」直接去比「L4得分」，但两者量纲不同 ——
    #   「行业均值」是【基础分】口径（0-6），「L4得分」是【基础分+对标分】口径（0-10）。
    #   同一根轴比两个量纲，得出的高低没有意义，典型症状是「零披露吃保底 1 分」的指标
    #   在图上看起来"领先行业均值"。
    # 换算规则：把行业基础分均值代入同一套对标分规则（即报告正文「达行业均值→X」那一列，
    #   已验证与正文逐项吻合）：
    #     行业均值 0.00（本行业普遍未披露）→ 对标分 1 → 1.00  ← 保底线
    #     行业均值 0.18 → 对标分 3 → 3.18
    #     行业均值 3.18 → 对标分 3 → 6.18
    result['行业L4得分'] = (round(mean + calc_benchmark_score(mean, mean), 2)
                        if isinstance(mean, (int, float)) else None)
    # 未披露：完整度与质量均为 0（上市版即基础分 0；非上市版即无公开证据）
    result['是否未披露'] = (calc_completeness(node) == 0 and calc_quality(node) == 0)
    # 保底分：行业均值 < 0.5 且本企业未披露 → 对标分走兜底分支拿 1 分
    #   （语义是"行业都没做，不罚你"，不是能力得分；仅此类指标需要画保底线）
    result['保底分'] = (1.0 if (result['是否未披露']
                            and isinstance(mean, (int, float)) and mean < 0.5) else None)

    if company_type == 'unlisted':
        # 非上市版：判断是否有公开证据
        has_evidence = calc_completeness(node) > 0 or calc_quality(node) > 0
        if has_evidence:
            # 有公开证据：标准评分（同上市版，基础分+对标分）
            completeness = calc_completeness(node)
            quality = calc_quality(node)
            basic = completeness + quality
            benchmark = calc_benchmark_score(basic, mean)
            result['完整度分'] = completeness
            result['质量分'] = quality
            result['基础分'] = basic
            result['对标分'] = benchmark
            result['L4得分'] = round(basic + benchmark, 2)
            result['基础分来源'] = '有公开证据(标准评分)'
        else:
            # 无公开证据：取全市场中位数得分
            median = MEDIANS.get(indicator)
            m = round(median, 2) if median is not None else 0
            result['完整度分'] = 0
            result['质量分'] = 0
            result['基础分'] = m
            result['对标分'] = 0
            result['L4得分'] = m
            result['基础分来源'] = '无公开证据(取全市场中位数)'
        return result

    # 上市版：标准评分
    completeness = calc_completeness(node)
    quality = calc_quality(node)
    basic = completeness + quality
    benchmark = calc_benchmark_score(basic, mean)
    l4 = calc_l4_score(basic, benchmark)

    result['完整度分'] = completeness
    result['质量分'] = quality
    result['基础分'] = basic
    result['对标分'] = benchmark
    result['L4得分'] = l4
    return result


# ============================== 公式6/7：维度与总分 ==============================
def calc_obstacle(l4_score: float, weight: float) -> float:
    """障碍度 = (1 − L4得分/10) × 全局权重，保留4位小数"""
    return round((1 - l4_score / 10) * weight, 4)


def map_level(total: float) -> str:
    """等级映射（公式9）"""
    if total >= 9.0:
        return '标杆'
    if total >= 7.0:
        return '先进'
    if total >= 5.0:
        return '发展中'
    if total >= 3.0:
        return '基础'
    if total >= 1.0:
        return '初始'
    return '空白'


# ============================== 失分类型判定（公式10） ==============================
def classify_loss_type(indicator_result: dict) -> str:
    """失分类型：完整度失分 / 质量失分 / 对标失分"""
    completeness = indicator_result.get('完整度分', 0)
    quality = indicator_result.get('质量分', 0)
    benchmark = indicator_result.get('对标分', 0)
    if completeness <= 1:
        return '完整度失分'
    if quality <= 1:
        return '质量失分'
    if benchmark <= 1:
        return '对标失分'
    return '无明显失分'


# ============================== 顶层：完整评分 ==============================
def score_all(extracted: dict, industry: str, company_type: str = 'listed') -> dict:
    """完整评分入口：
    输入：提取结果 JSON（指标提取结果 4维度嵌套）
    输出：增强 JSON（L4计算结果42项 + 维度得分 + 总分 + 等级 + 障碍度Top10）
    """
    dims = extracted.get('指标提取结果', {})
    weights = GLOBAL_WEIGHTS

    l4_results = {}
    indicator_dim_map = {}  # 指标名 → 维度

    # 遍历 4 维度 → 子域 → 指标
    for dim_name, dim_data in dims.items():
        if not isinstance(dim_data, dict):
            continue
        dim_name = normalize_dim_name(dim_name)   # v=84（M4）：LLM 变体 → 标准名
        indicators = dim_data.get('指标', {})
        for sub_name, sub_data in indicators.items():
            if not isinstance(sub_data, dict):
                continue
            for ind_name, node in sub_data.items():
                if not isinstance(node, dict):
                    continue
                # 单个指标评分
                result = score_indicator(ind_name, node, industry, company_type)
                result['维度'] = dim_name
                result['子域'] = sub_name
                l4_results[ind_name] = result
                indicator_dim_map[ind_name] = dim_name

    # 维度得分：Σ(维度内L4得分×权重) / Σ(维度内权重)
    dim_scores = {}
    dim_weights = {}
    for dim_name in DIM_SUB_MAP:
        dim_scores[dim_name] = 0.0
        dim_weights[dim_name] = 0.0
    for ind_name, result in l4_results.items():
        dim = normalize_dim_name(result.get('维度', '企业内部碳管理'))   # v=84（M4）：聚合时也归一化
        w = result.get('权重', 0)
        dim_scores[dim] = dim_scores.get(dim, 0) + result['L4得分'] * w
        dim_weights[dim] = dim_weights.get(dim, 0) + w

    final_dim_scores = {}
    for dim, score_sum in dim_scores.items():
        w_sum = dim_weights.get(dim, 0)
        final_dim_scores[dim] = round(score_sum / w_sum, 2) if w_sum > 0 else 0.0

    # 总分：Σ(42个L4得分×全局权重)
    total = sum(r['L4得分'] * r['权重'] for r in l4_results.values())
    total = round(total, 2)

    # 障碍度：全部42项 + Top10
    obstacle_list = []
    for ind_name, result in l4_results.items():
        obs = calc_obstacle(result['L4得分'], result['权重'])
        result['障碍度'] = obs
        result['失分类型'] = classify_loss_type(result)
        obstacle_list.append({
            '指标': ind_name,
            'L4得分': result['L4得分'],
            '权重': result['权重'],
            '障碍度': obs,
            '失分类型': result['失分类型'],
        })
    obstacle_list.sort(key=lambda x: -x['障碍度'])
    top10 = obstacle_list[:10]

    return {
        '公司类型': company_type,
        '行业': industry,
        'L4计算结果': l4_results,
        '维度得分': final_dim_scores,
        '总分': total,
        '评分等级': map_level(total),
        '障碍度Top10': top10,
    }


if __name__ == '__main__':
    # 自测：用示例数据跑一遍
    sample = {
        "指标提取结果": {
            "企业内部碳管理": {
                "指标": {
                    "碳排放": {
                        "碳总量": {"值": "14,446", "单位": "tCO₂e", "年份": "2025", "原文": "温室气体排放总量13,380.09 14,446（第19页），较上年下降8.5%"},
                        "范围一排放": {"值": "246", "单位": "tCO₂e", "年份": "2025", "原文": "范围一排放246吨"},
                        "范围二排放": {"值": "14,200", "单位": "tCO₂e", "年份": "2025", "原文": "范围二排放14200吨"},
                        "范围三排放": {"值": "", "原文": ""},
                        "碳排放强度": {"值": "3.25", "单位": "tCO₂e/百万元", "年份": "2025", "原文": "碳排放强度3.25"},
                        "碳排放抵消与同比变化": {"值": "下降8.5%", "原文": "较上年下降8.5%"},
                    }
                }
            }
        }
    }
    import sys
    result = score_all(sample, '电子科技', 'listed')
    print(json.dumps(result, ensure_ascii=False, indent=2))
