# -*- coding: utf-8 -*-
"""
指标适用性与改进收益量化（Agent 1.5 · 确定性分析层）

职责
    回答智能体二「不该由 LLM 猜」的两个问题：
      ① **改进后能提升多少分？** —— 用 weights.json 精确重算总分（不是估算）
      ② **这个指标对该企业是否适用 / 是否本来就无需披露？** —— 用披露率 + 企业自述判定

为什么必须放在 Python 层
    项目立身之本是「算术层确定性 / 推理层语义」的双层边界。
    「提升 X 分」「等级从初始→基础」属于**算术**，交给 LLM 必然出现编造数字。
    本模块把这两件事变成可复现的确定性计算，LLM 只负责把结论写成人话。

三道判定依据（由硬到软，逐级降权）
    1. **企业自述理由**（最硬）：报告原文里已写明「范围三难以统计」「不属于重点排放单位」
       「未纳入碳配额管理」等 → 属**企业已说明不适用**，不应再建议「补充披露」，
       而应转为「评估其理由是否充分 / 是否需按准则补充说明」。
    2. **行业披露率 + 行业均值**（数据驱动、可复现）：来自离线同行索引与 weights.json。
       · 披露率 ≥60% 且均值 ≥3 → 行业惯例，属常规披露项
       · 披露率 <15% 且均值 <1 → 行业普遍空白（新兴议题），非本企业独有短板
    3. **业务关联度**（最软，仅用于明显不相关时提示）：由企业画像关键词判定。

设计约束
    · 纯 Python，无网络、无 LLM，毫秒级，结果可复现
    · 缺数据时降级（不抛异常），结论里标明依据来源
"""
import json
import os
import re

# ============================== 配置 ==============================
def _resolve_weights_file() -> str:
    """定位 weights.json：兼容「容器内 /app/weights.json」与「本地 esg_extractor/weights.json」。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.environ.get('MATERIALITY_WEIGHTS', ''),
        os.path.join(here, 'weights.json'),                    # 容器内
        os.path.join(os.path.dirname(here), 'weights.json'),   # 本地开发
    ]
    for p in cands:
        if p and os.path.exists(p):
            return p
    return cands[1]


WEIGHTS_FILE = _resolve_weights_file()

# 等级阈值（与 weights_logic.map_level 保持一致，避免两处口径漂移）
LEVEL_TABLE = [(9.0, '标杆'), (7.0, '先进'), (5.0, '发展中'), (3.0, '基础'), (1.0, '初始')]

# ============================== 企业自述「不适用」的表述特征 ==============================
# 命中即认为企业**已主动说明为何不披露/不统计**，属「说明性未披露」而非「遗漏」
RATIONALE_PATTERNS = [
    (r"难以统计|无法统计|不易统计|难以核算|不具备统计条件", "企业自述统计口径困难"),
    (r"不适用|不涉及|未涉及该|无此业务|不属于.*范围", "企业自述该议题不适用"),
    (r"不属于.*重点排放单位|未纳入.*碳配额|非控排企业|不属于.*控排", "企业自述未被纳入强制碳市场"),
    (r"不包含范围三|未包含范围三|不含范围三|范围三.*未|暂未.*范围三", "企业自述范围三不在核算边界内"),
    (r"将在.*年.*披露|计划.*披露|拟.*披露|逐步.*披露|下一年度.*披露", "企业承诺后续披露"),
    (r"该指标不适用|非本公司.*业务|与本.*业务无关", "企业自述与本企业业务无关"),
]

# 指标名 → 该指标是否天然只对特定业务类型有意义（用于业务关联度提示）
# 键为「业务标签」，值为「与该业务关联度较低的指标」
BUSINESS_TAGS = {
    "金融": (["银行", "证券", "保险", "金融", "信托", "基金", "资管"],
             ["包装减量及回收情况", "绿色物流", "循环经济链塑造", "能源类型和强度", "能效提升对比度"]),
    "软件信息": (["软件", "信息", "通信", "互联网", "数据", "系统集成", "信息技术"],
               ["包装减量及回收情况", "循环经济链塑造", "碳配额管理", "能源类型和强度"]),
    "电力": (["电力", "电网", "发电", "输配", "供电"],
           []),
    "高耗能制造": (["钢铁", "水泥", "化工", "有色", "造纸", "玻璃", "陶瓷", "冶炼"],
                []),
    "服务": (["服务", "咨询", "文旅", "酒店", "零售", "贸易"],
           ["包装减量及回收情况", "循环经济链塑造", "碳配额管理", "碳资产交易收益"]),
}

# 「软件信息」标签的实体行业豁免（v5.1 修复）
# 背景：能源电力/装备制造企业的画像里常出现「能源互联网」「系统集成」等**行业术语**，
# 会被误判为软件信息业，从而把「循环经济链塑造」等真实短板豁免成「低重要性」。
# 实测案例：国电南瑞（行业=能源电力）被判"循环经济链不相关"，但同行披露率高达 83.3%。
# 规则：行业命中下列任一实体行业时，不再套用「软件信息」的低关联判定。
SOFT_EXEMPT_INDUSTRY_KWS = (
    "能源", "电力", "电网", "发电", "输配", "供电", "制造", "装备", "重工", "机械",
    "化工", "材料", "钢铁", "水泥", "有色", "冶炼", "建筑", "地产", "建设",
    "医药", "生物", "环保", "水务", "汽车", "消费", "食品", "纺织", "交通", "运输", "物流",
)


def _load_weights() -> dict:
    try:
        with open(WEIGHTS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:                                # noqa: BLE001 — 缺失不致命
        return {}


_WEIGHTS = _load_weights()


def _map_level(total: float) -> str:
    for th, name in LEVEL_TABLE:
        if total >= th:
            return name
    return '空白'


def _cut(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n] + "…"


# ============================== ① 企业自述理由识别 ==============================
def detect_rationale(node: dict):
    """检测企业是否已主动说明「为何未披露」。

    只看报告的**值 / 原文**字段（企业自己的文字），命中返回 (类型, 证据句)，否则 None。
    """
    if not isinstance(node, dict):
        return None
    text = "%s %s" % (node.get('值') or '', node.get('原文') or '')
    if not text.strip():
        return None
    for pat, label in RATIONALE_PATTERNS:
        m = re.search(pat, text)
        if m:
            s = max(0, m.start() - 30)
            return label, _cut(text[s:m.end() + 40], 120)
    return None


# ============================== ② 行业基准（披露率 + 均值） ==============================
def _peer_rate(metric: str, peer_index: dict):
    """从离线同行索引取「全市场披露率」。返回 (已披露数, 样本数, 披露率%)。"""
    try:
        entry = (peer_index or {}).get('by_metric', {}).get(metric)
        if not entry:
            return 0, 0, None
        total = int(((peer_index or {}).get('meta', {}).get('覆盖', {}) or {}).get('公司数', 0) or 0)
        cov = int(entry.get('全市场已披露', 0) or 0)
        if total <= 0:
            return cov, total, None
        return cov, total, round(cov / total * 100, 1)
    except Exception:                                # noqa: BLE001
        return 0, 0, None


def _industry_mean(metric: str, industry: str, mean_hint=None):
    """取该指标的行业均值（0-6 基础分尺度）。

    优先用 **评分层已经算好的值**（`L4计算结果[指标].行业均值`）——容器内无需 weights.json，
    避免权重文件出现第二份副本导致口径漂移；本地开发时若没有 hint 再回落到 weights.json。
    返回 (mean, 来源说明)。
    """
    if isinstance(mean_hint, (int, float)):
        return float(mean_hint), '评分层行业均值'
    try:
        base = _WEIGHTS.get('行业基准库', {}) or {}
        all_market = base.get('【全市场均值】') or base.get('全市场均值') or {}
        ind = str(industry or '').strip().replace('行业', '').replace('市', '')
        if ind in base:
            d = base[ind]
            means = d.get('指标均值', {}) or {}
            if int(d.get('样本数', 0) or 0) >= 10 and metric in means:
                return means[metric], '行业均值'
        m = (all_market.get('指标均值', {}) or {}).get(metric)
        return (m, '全市场均值') if m is not None else (None, '缺失')
    except Exception:                                # noqa: BLE001
        return None, '缺失'


def _basis_note(rate, total, mean, industry, base_source='', sample_n=None) -> str:
    """构造「对标基准」的口径标注串（v110 B-lite）。

    目的：消掉「全市场覆盖率」与「本行业均值」并列时读者无法分辨来源的歧义 ——
    两个数字各标口径与样本量，使读者（含 LLM 与评委）一眼看出它们来自不同群体。

    形如：全市场覆盖率 65.7%（同业库 102 家）· 本行业均值 3.92（化工新材料 12 家）
    """
    parts = []
    if rate is not None:
        parts.append("全市场覆盖率 %.1f%%（同业库 %d 家）" % (rate, int(total or 0)))
    if mean is not None:
        src = str(base_source or '')
        if '降级' in src:
            parts.append("全市场均值 %.2f（该行业样本不足 10 家，已降级）" % mean)
        elif '不在列表' in src:
            parts.append("全市场均值 %.2f（该行业未单独统计）" % mean)
        else:
            n = int(sample_n or 0)
            who = ("%s %d 家" % (industry, n)) if (industry and n) else ""
            parts.append("本行业均值 %.2f%s" % (mean, ("（%s）" % who) if who else ""))
    return " · ".join(parts)


# ============================== ③ 业务关联度 ==============================
def business_hint(metric: str, profile: dict, industry: str = ''):
    """业务关联度提示：返回 (是否低关联, 说明)。profile 为企业画像 dict。

    v5.1：新增行业豁免——实体行业（能源电力/装备制造等）不套用「软件信息」低关联判定，
    避免「能源互联网」「系统集成」等行业术语被误认为软件企业特征。
    """
    if not profile:
        return False, ""
    text = " ".join(str(v) for v in profile.values() if isinstance(v, str))
    if not text:
        return False, ""
    ind = str(industry or '') or str((profile or {}).get('所属行业') or '')
    for tag, (kws, low_metrics) in BUSINESS_TAGS.items():
        if tag == "软件信息" and any(x in ind for x in SOFT_EXEMPT_INDUSTRY_KWS):
            continue                                    # 实体行业豁免，跳过
        if any(k in text for k in kws) and metric in low_metrics:
            return True, "该能力与「%s」类业务的关联度较低，碳强度主要来自办公与差旅而非生产环节" % tag
    return False, ""


# ============================== ④ 适用性判定 ==============================
def judge_applicability(metric: str, node: dict, industry: str,
                        peer_index: dict, profile: dict, mean_hint=None,
                        base_source: str = '', sample_n=None) -> dict:
    """判定单个指标对企业的「披露必要性」。

    返回 {"等级", "依据", "说明", "企业已说明理由"}
    等级取值：必备 / 应披露 / 可选 / 低重要性 / 已说明不适用
    """
    rationale = detect_rationale(node)
    rate = None
    mean = None
    total = 0
    src = ''
    try:
        cov, total, rate = _peer_rate(metric, peer_index)
        mean, src = _industry_mean(metric, industry, mean_hint)
    except Exception:                                # noqa: BLE001
        pass

    # v110 B-lite：把两个数字各自的口径与样本量拼好，供下面各分支统一引用
    basis = _basis_note(rate, total, mean, industry, base_source or src, sample_n)

    low_biz, biz_note = business_hint(metric, profile, industry)

    # 1) 企业已自述理由 → 最高优先，直接定级
    if rationale:
        kind, quote = rationale
        return {
            "等级": "已说明不适用",
            "依据": "企业自述",
            "说明": "企业已主动说明该项能力不适用的理由（%s），属「说明性不适用」（判据 L5），"
                    "评估重点应是**理由是否充分、是否符合准则要求**，而非简单建议建设该项能力。" % kind,
            "企业已说明理由": quote,
        }

    # 2) 业务明显不相关
    if low_biz:
        return {
            "等级": "低重要性",
            "依据": "业务关联度",
            "说明": biz_note + "；该能力**不宜列为本企业的优先建设方向**（判据 L4/L5），资源允许时可做定性说明。",
            "企业已说明理由": None,
        }

    # 3) 数据驱动（披露率 + 行业均值）
    if rate is not None and mean is not None:
        if rate >= 60 and mean >= 3:
            return {"等级": "必备", "依据": "行业数据",
                    "说明": "对标基准：%s，表明该能力**属行业普遍水平**"
                            "（判据 L3）；本企业尚未达到，属实质性能力缺口，建议优先建设。" % basis,
                    "企业已说明理由": None}
        if rate >= 30:
            return {"等级": "应披露", "依据": "行业数据",
                    "说明": "对标基准：%s，属**行业主流能力水平**"
                            "（判据 L3）；建议补齐该项能力以对齐行业水平。" % basis,
                    "企业已说明理由": None}
        if rate >= 15 and mean >= 1:
            return {"等级": "可选", "依据": "行业数据",
                    "说明": "对标基准：%s，仅少数同行已建立该能力"
                            "且行业整体水平偏低（判据 L4）；资源允许时建设，不必作为优先事项。" % basis,
                    "企业已说明理由": None}
        # 覆盖低 且 行业均值近乎为 0 → 行业尚未普遍建立，不是本企业独有短板
        return {"等级": "低重要性", "依据": "行业数据",
                "说明": "对标基准：%s，属**行业尚未普遍建立的新兴能力**"
                        "（判据 L4），**不是本企业独有短板**；论述时应明确这一点，避免给读者造成"
                        "「本企业特别差」的误解。" % basis,
                "企业已说明理由": None}

    if rate is not None:
        return {"等级": "应披露" if rate >= 30 else "可选", "依据": "行业披露率",
                "说明": "对标基准：%s。" % basis, "企业已说明理由": None}
    if mean is not None:
        return {"等级": "应披露" if mean >= 3 else "可选", "依据": "行业均值",
                "说明": "对标基准：%s。注意：本次未取到同行覆盖数据，依据较弱。" % basis,
                "企业已说明理由": None}

    return {"等级": "未知", "依据": "无",
            "说明": "本次未取到行业对标基准，无法判定该能力是否应由本企业承担，建议按行业常规能力水平处理。",
            "企业已说明理由": None}


# ============================== ⑤ 改进收益量化 ==============================
def simulate(l4_results: dict, metric: str, target_l4: float, total_now: float,
             dim_scores: dict) -> dict:
    """把某指标的 L4 得分提升到 target_l4，重算总分（精确，不是估算）。

    总分 = Σ(L4得分 × 全局权重)，因此单项收益 = ΔL4 × 该指标权重（线性、可复现）。
    """
    r = (l4_results or {}).get(metric)
    if not isinstance(r, dict):
        return {}
    cur = float(r.get('L4得分', 0) or 0)
    w = float(r.get('权重', 0) or 0)
    tgt = max(cur, float(target_l4))
    delta = round((tgt - cur) * w, 4)
    new_total = round(total_now + delta, 2)
    return {
        "当前L4": cur,
        "目标L4": round(tgt, 2),
        "权重": w,
        "ΔL4": round(tgt - cur, 2),
        "Δ总分": delta,
        "新总分": new_total,
        "当前等级": _map_level(total_now),
        "新等级": _map_level(new_total),
        "Δ百分比": round(delta / total_now * 100, 2) if total_now else 0.0,
    }


def improvement_options(l4_results: dict, metric: str, total_now: float,
                        industry: str, dim_scores: dict) -> dict:
    """给出三档改进情境：能力补齐 / 达到行业均值 / 满分。"""
    r = (l4_results or {}).get(metric) or {}
    cur = float(r.get('L4得分', 0) or 0)
    comp = float(r.get('完整度分', 0) or 0)
    qual = float(r.get('质量分', 0) or 0)
    mean = r.get('行业均值')

    out = {}

    # 情境一：能力补齐（完整度拉满到 3，质量维持）
    basic_new = 3 + qual
    # 对标分按新基础分重估（与 weights_logic 的 5 档口径一致）
    bench_new = _bench(basic_new, mean)
    out['能力补齐'] = simulate(l4_results, metric, min(basic_new + bench_new, 10), total_now, dim_scores)

    # 情境二：达到行业均值水平
    if isinstance(mean, (int, float)):
        tgt = round(float(mean) + _bench(float(mean), mean), 2)
        out['达行业均值'] = simulate(l4_results, metric, min(tgt, 10), total_now, dim_scores)

    # 情境三：满分
    out['满分'] = simulate(l4_results, metric, 10, total_now, dim_scores)

    # 按 Δ总分 从高到低（满分 > 达均值 > 补齐）
    return {k: v for k, v in sorted(out.items(), key=lambda kv: -(kv[1].get('Δ总分', 0) if kv[1] else 0))}


def _bench(basic: float, mean) -> int:
    """对标分 5 档（与 weights_logic.calc_benchmark_score 口径一致）。"""
    if mean is None:
        return 2
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


# ============================== 主入口 ==============================
def analyze(esg_json: dict, peer_index: dict = None, **kw) -> dict:
    """主入口：增强 JSON → 适用性 + 改进收益 的确定性分析。

    Args:
        esg_json:   增强 JSON（含 报告元数据 / 指标提取结果 / 评分增强数据）
        peer_index: 离线同行索引（peer_retrieval.load_index()），可为 None
    Returns:
        {"启用", "企业", "逐项": [...], "总纲": {...}, "文本块": str}
        任何异常返回 启用=False，绝不影响主链路。
    """
    try:
        esg_json = _unwrap(esg_json)
        meta = (esg_json or {}).get('报告元数据', {}) or {}
        scored = (esg_json or {}).get('评分增强数据', {}) or {}
        l4 = scored.get('L4计算结果', {}) or {}
        dim_scores = scored.get('维度得分', {}) or {}
        total_now = float(scored.get('总分', 0) or 0)
        industry = str(scored.get('行业') or meta.get('所属行业') or '').strip()
        top10 = [o for o in (scored.get('障碍度Top10') or []) if isinstance(o, dict)]

        if not l4:
            return {"启用": False, "原因": "缺 L4计算结果，无法量化收益",
                    "逐项": [], "总纲": {}, "文本块": ""}

        # 企业画像（报告元数据里的画像字段，缺失则为空）
        profile = {k: meta.get(k) for k in
                   ('企业名称', '所属行业', '主营业务', '企业简介', '企业性质', '营收规模') if meta.get(k)}

        # 原始提取节点（用于识别企业自述理由）
        nodes = _flatten_nodes(esg_json.get('指标提取结果', {}) or {})

        items = []
        sum_delta_complete = 0.0
        for o in top10:
            metric = str(o.get('指标', '')).strip()
            if not metric:
                continue
            node = nodes.get(metric, {})
            l4m = l4.get(metric) or {}
            mean_hint = l4m.get('行业均值')
            app = judge_applicability(metric, node, industry, peer_index, profile,
                                      mean_hint=mean_hint,
                                      base_source=l4m.get('基准来源') or '',
                                      sample_n=l4m.get('基准样本数'))
            opts = improvement_options(l4, metric, total_now, industry, dim_scores)
            # 用「能力补齐」情境估算 Top10 总收益（最贴近实际可执行的动作）
            base_opt = opts.get('能力补齐') or {}
            sum_delta_complete += float(base_opt.get('Δ总分', 0) or 0)
            items.append({
                "指标": metric,
                "失分类型": o.get('失分类型', ''),
                "当前L4": o.get('L4得分'),
                "权重": o.get('权重'),
                "行业均值": l4m.get('行业均值'),
                "基准来源": l4m.get('基准来源'),
                "基准样本数": l4m.get('基准样本数'),
                "适用性": app,
                "收益": opts,
            })

        # 总纲：Top10 全部补齐披露后的总分与等级
        new_total = round(total_now + sum_delta_complete, 2)
        overview = {
            "当前总分": round(total_now, 2),
            "当前等级": _map_level(total_now),
            "Top10全部补齐披露后": new_total,
            "升级后等级": _map_level(new_total),
            "Δ总分": round(sum_delta_complete, 2),
            "Δ百分比": round(sum_delta_complete / total_now * 100, 2) if total_now else 0.0,
            "跨级": _map_level(new_total) != _map_level(total_now),
        }

        text = _render_text(industry, overview, items, profile)
        return {
            "启用": True,
            "企业": profile,
            "行业": industry,
            "逐项": items,
            "总纲": overview,
            "文本块": text,
            "统计": {
                "指标数": len(items),
                "适用性分布": _count(items, lambda i: i["适用性"]["等级"]),
                "有自述理由": sum(1 for i in items if i["适用性"].get("企业已说明理由")),
            },
        }
    except Exception as e:                            # noqa: BLE001
        return {"启用": False, "原因": "适用性分析异常: %s" % e,
                "逐项": [], "总纲": {}, "文本块": ""}


def _count(items, fn) -> dict:
    out = {}
    for i in items:
        k = fn(i) or "未知"
        out[k] = out.get(k, 0) + 1
    return out


def _flatten_nodes(dims: dict) -> dict:
    """指标提取结果（4维度嵌套）→ {指标名: 节点}。"""
    out = {}
    for _dim, d in (dims or {}).items():
        if not isinstance(d, dict):
            continue
        for _sub, s in (d.get('指标', {}) or {}).items():
            if not isinstance(s, dict):
                continue
            for name, node in s.items():
                if isinstance(node, dict):
                    out[name] = node
    return out


def _fact_basis(app: dict) -> str:
    """把「适用性判定」映射为建议的 `事实基础` 取值。

    存在的意义：`事实基础` 是**事实标签**，不该由 LLM 凭语气猜。
    v5.0 实测发现 LLM 会把「低重要性（依据=业务关联度）」误写成「行业普遍空白」，
    与同行对照里 67.6% 的披露率自相矛盾。此处由 Python 钉死取值：
      · 依据=企业自述   → 已说明不适用
      · 依据=业务关联度 → 报告未披露（业务本身不涉及，与行业披露率无关）
      · 依据=行业数据 且 说明含「行业普遍空白」→ 行业普遍空白（仅此处可为）
      · 其余（必备/应披露/可选）→ 报告未披露
    注：「已确证公开事实」需联网确证，由 LLM 结合【企业公开信息】判断，此处不产出。
    """
    src = (app or {}).get('依据', '')
    note = (app or {}).get('说明', '')
    if src == '企业自述':
        return '已说明不适用'
    if src == '业务关联度':
        return '报告未披露'
    if '行业普遍空白' in note:
        return '行业普遍空白'
    return '报告未披露'


def _render_text(industry, overview, items, profile) -> str:
    """渲染给 LLM 的「确定性分析结论」文本块。"""
    L = []
    L.append("【确定性分析 · 能力缺口对标基准与补齐收益】")
    L.append("（本节所有数字由 Python 按评分公式精确重算，**不得改写、不得自行推算新数字**）")
    if profile:
        L.append("企业画像：%s" % "；".join("%s=%s" % (k, _cut(v, 40)) for k, v in profile.items()))
    if industry:
        L.append("行业：%s" % industry)
    L.append("")
    L.append("■ 总纲：若把能力缺口 Top10 全部补齐 → 总分 %s（%s）→ %s（%s），"
             "Δ=%+0.2f 分（%+0.1f%%）%s" % (
                 overview['当前总分'], overview['当前等级'],
                 overview['Top10全部补齐披露后'], overview['升级后等级'],
                 overview['Δ总分'], overview['Δ百分比'],
                 "，**可实现等级跃升**" if overview['跨级'] else ""))
    L.append("")
    L.append("■ 逐项（按能力缺口严重度排序）：")
    for i, it in enumerate(items, 1):
        app = it['适用性']
        _mnote = _basis_note(None, 0, it.get('行业均值'), industry,
                             it.get('基准来源') or '', it.get('基准样本数'))
        L.append("%d. %s ｜ 缺口类型：%s ｜ 当前 L4=%s ｜ %s" % (
            i, it['指标'], it['失分类型'], it['当前L4'],
            _mnote if _mnote else "行业均值缺失"))
        L.append("   【能力判据】请按下方【适用性】自行落到 L1~L5，填进输出的 `能力判据` 字段")
        L.append("   【适用性】%s（依据：%s）—— %s" % (app['等级'], app['依据'], app['说明']))
        L.append("   【事实基础】%s（`事实基础` 字段请直接填此值；"
                 "仅当【企业公开信息】里有确证条目时，才可改填「已确证公开事实」）"
                 % _fact_basis(app))
        if app.get('企业已说明理由'):
            L.append("   【企业已说明理由】「%s」" % app['企业已说明理由'])
        opts = it['收益']
        parts = []
        for label, v in opts.items():
            if v:
                parts.append("%s→%s（%+0.2f 分，新总分 %s%s）" % (
                    label, v['目标L4'], v['Δ总分'], v['新总分'],
                    "，升至%s" % v['新等级'] if v['新等级'] != v['当前等级'] else ""))
        if parts:
            L.append("   【改进收益】" + "；".join(parts))
    L.append("")
    L.append("用法要求：")
    L.append("  · 「能力补齐后能提升多少分」必须直接引用上述数字，禁止自己估算或改写。")
    L.append("  · 本节的【适用性】就是你判定 `能力判据` 的依据：「必备 / 应披露」→ L3（真能力缺口）；"
             "「低重要性」→ L4（行业新兴议题）；「已说明不适用」→ L5；"
             "若【企业公开信息】有公告/制度确证 → L2。")
    L.append("  · 适用性为「已说明不适用 / 低重要性」的能力项，不得再写成「企业遗漏」；"
             "应转为评估其理由是否充分、是否需要按准则补充说明。")
    L.append("  · 适用性为「必备 / 应披露」的能力项，才是真正该优先投入资源的缺口。")
    L.append("  · ⚠️ 本节基准数字的**口径已逐项标注**：「全市场覆盖率」是全市场口径，"
             "「本行业均值」是该公司所属行业口径（二者样本量不同）——引用时必须**连口径与样本量一起引用**，"
             "不得把两个数字混称「行业水平」；若标了「已降级」，则说明该行业样本不足，"
             "**不要声称是同行业对标**。它们都是**对标基准证据**，不是结论；"
             "结论必须落在「该企业这项能力有没有、与标杆差在哪」上（见「评估视角总纲」）。")
    L.append("  · 「事实基础」必须采用本节的【事实基础】建议值：只有「依据=行业数据且披露率<15%」"
             "才可写「行业普遍空白」；「依据=业务关联度」（业务本身不涉及）一律写「报告未披露」，"
             "**严禁**写成「行业普遍空白」——那会与同行对照里的披露率自相矛盾。")
    return "\n".join(L)


def _unwrap(data) -> dict:
    """兼容平铺增强 JSON 与 {extracted:{…}} 包裹体。"""
    if not isinstance(data, dict):
        return {}
    if "评分增强数据" in data or "报告元数据" in data:
        return data
    for key in ("extracted", "result", "data", "esg_json"):
        inner = data.get(key)
        if isinstance(inner, dict) and ("评分增强数据" in inner or "报告元数据" in inner):
            return inner
    return data


# ============================== 自测 ==============================
if __name__ == '__main__':
    import io as _io
    import sys as _sys
    _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')
    src = r"[DATA_ROOT]\ESG-JSON-格式提取结果\国电南瑞科技股份有限公司-2025年_ESG报告_JSON.json"
    with open(src, encoding='utf-8') as f:
        ext = json.load(f)
    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import weights_logic as W
    scored = W.score_all(ext, industry="装备制造", company_type="listed")
    esg = {"报告元数据": ext["报告元数据"], "指标提取结果": ext["指标提取结果"], "评分增强数据": scored}
    peer = None
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'peer_index.json')
        with open(p, encoding='utf-8') as f:
            peer = json.load(f)
    except Exception:                                # noqa: BLE001
        pass
    res = analyze(esg, peer)
    print("启用:", res.get("启用"), "| 统计:", json.dumps(res.get("统计", {}), ensure_ascii=False))
    print(res.get("文本块", "")[:5000])
