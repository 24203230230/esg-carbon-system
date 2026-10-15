# -*- coding: utf-8 -*-
"""
同行对标检索层（Agent 1.5）—— P0 · D 方案（离线同行对标库）

职责
    给定「失分指标 + 失分类型」，从**离线**同行索引中检索出
    「同一指标，同行是怎么写的、披露了什么数字」→ 输出带出处的「外部事实块」，
    供智能体二（推理层）把诊断从「文本层」抬到「事实层」。

为什么要这一层
    改造前证据池只有 `报告自述 + 行业均值`，于是「报告未披露」只能推出
    「建议补充披露」这种程序性结论，无法区分「做了但没披露」与「确实没做」。
    本层补上第三个证据源：**同行的公开做法**（含披露率统计 + 原文片段）。

职责边界（对齐项目"算术层确定性 / 推理层语义"的双层原则）
    · 本层是**确定性检索**：规则匹配 + 排序，不做任何 LLM 概括、不做语义改写 → 结果可复现
    · 只回传索引里真实存在的原文片段 → 天然免疫幻觉（LLM 无法凭空生成"外部事实"）
    · 索引缺失 / 解析失败时**静默降级**，返回空事实块，完全不影响原有分析链路

规则式检索（对应计划书 §1.6 的「② 规则式」）
    | 失分类型   | 检索意图                     | 筛选偏好         |
    |-----------|------------------------------|-----------------|
    | 完整度失分 | 同行「披露了什么」→ 补披露清单  | 原文完整者优先    |
    | 质量失分   | 同行「如何量化」→ 量化口径模板  | 仅取定量样例      |
    | 对标失分   | 同行「领先做法」→ 对标复制     | 全部已披露样例    |
    排序：同行业 → 定量 → 原文更长（信息含量）
"""
import json
import os
import threading

# ============================== 配置 ==============================
INDEX_FILE = os.environ.get(
    'PEER_INDEX_FILE',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'peer_index.json'))
PEER_ENABLED = os.environ.get('PEER_RETRIEVAL', 'on').lower() not in ('0', 'off', 'false', 'no', '')
MAX_SAME = int(os.environ.get('PEER_MAX_SAME', '2'))      # 每指标同行业样例上限
MAX_CROSS = int(os.environ.get('PEER_MAX_CROSS', '1'))    # 每指标跨行业样例上限
VAL_CHARS = int(os.environ.get('PEER_VAL_CHARS', '60'))   # 「值」截断
QUOTE_CHARS = int(os.environ.get('PEER_QUOTE_CHARS', '150'))  # 「原文」截断
MAX_METRICS = 10                                          # 处理障碍度 Top N

_lock = threading.Lock()
_CACHE = {"index": None, "loaded": False, "error": None, "path": None}


# ============================== 索引加载 ==============================
def load_index(force: bool = False):
    """懒加载索引（模块级缓存，首次调用后常驻内存）。失败返回 None。"""
    with _lock:
        if _CACHE["loaded"] and _CACHE["path"] == INDEX_FILE and not force:
            return _CACHE["index"]
        _CACHE.update({"loaded": True, "path": INDEX_FILE})
        try:
            if not os.path.exists(INDEX_FILE):
                _CACHE["index"], _CACHE["error"] = None, f"索引文件不存在: {INDEX_FILE}"
            else:
                with open(INDEX_FILE, encoding='utf-8') as f:
                    _CACHE["index"] = json.load(f)
                _CACHE["error"] = None
        except Exception as e:                       # noqa: BLE001 — 任何异常都不允许冒泡到业务
            _CACHE["index"], _CACHE["error"] = None, str(e)
        return _CACHE["index"]


def index_status() -> dict:
    """给 /api/health 用的状态探针。"""
    idx = load_index()
    if not idx:
        return {"启用": PEER_ENABLED, "可用": False, "原因": _CACHE["error"] or "未加载"}
    meta = idx.get("meta", {})
    return {
        "启用": PEER_ENABLED,
        "可用": True,
        "公司数": meta.get("覆盖", {}).get("公司数", 0),
        "指标数": len(idx.get("by_metric", {})),
        "构建时间": meta.get("built_at", ""),
    }


# ============================== 工具 ==============================
def _cut(s, n: int) -> str:
    s = str(s or "").strip().replace("\n", " ").replace("\r", " ")
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"


def _same_industry(a: str, b: str) -> bool:
    """行业比对：任一为空 → 不算同行业（避免误判成"同行"）。"""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b or a in ("未知", "未匹配") or b in ("未知", "未匹配"):
        return False
    return a == b or a in b or b in a


def _scarcity(covered: int, total: int) -> str:
    if total <= 0:
        return "未知"
    r = covered / total
    if r < 0.15:
        return "高"
    if r < 0.50:
        return "中"
    return "低"


def _is_same_company(name: str, mine: str) -> bool:
    """排除企业自己（索引里通常没有，但做防御）。"""
    n, m = (name or "").strip(), (mine or "").strip()
    if not n or not m:
        return False
    if n == m:
        return True
    key = n[:4]
    return len(key) >= 3 and (key in m or m[:4] in n)


def _unwrap(data) -> dict:
    """兼容两种入参结构：

    1) 平铺增强 JSON（前端主链路）：{"报告元数据":…, "指标提取结果":…, "评分增强数据":…}
    2) 包裹体（e2e 脚本 / 部分调用方）：{"extracted": {…上述三段…}, "score_calculated":…}
    """
    if not isinstance(data, dict):
        return {}
    if "评分增强数据" in data or "报告元数据" in data:
        return data
    for key in ("extracted", "result", "data", "esg_json"):
        inner = data.get(key)
        if isinstance(inner, dict) and ("评分增强数据" in inner or "报告元数据" in inner):
            return inner
    return data


# ============================== 规则式检索 ==============================
def _pick_samples(samples: list, lose_type: str, my_ind: str, my_name: str):
    """按规则筛选 + 排序，返回 (同行业样例, 跨行业样例)。"""
    pool = [s for s in samples if not _is_same_company(s.get("公司", ""), my_name)]

    # 规则：质量失分 → 只看同行如何量化（定量优先，无定量再退回全部）
    if lose_type == "质量失分":
        quant = [s for s in pool if s.get("数据类型") in ("定量", "定量/定性")]
        if quant:
            pool = quant

    # 排序：中文优先（便于中文报告引用）→ 定量优先 → 原文更长（信息含量高）
    pool.sort(key=lambda s: (
        0 if s.get("语言", "CN") == "CN" else 1,
        0 if s.get("数据类型") in ("定量", "定量/定性") else 1,
        -len(s.get("原文", "") or ""),
    ))

    same = [s for s in pool if _same_industry(s.get("行业", ""), my_ind)]
    cross = [s for s in pool if not _same_industry(s.get("行业", ""), my_ind)]
    return same[:MAX_SAME], cross[:MAX_CROSS], len(same)


def _fmt_sample(s: dict) -> str:
    """单条样例 → 一行可读文本（含出处）。行业未知时不显示行业，避免「｜未知」噪音。"""
    val = _cut(s.get("值", ""), VAL_CHARS)
    quote = _cut(s.get("原文", ""), QUOTE_CHARS)
    单位 = (s.get("单位") or "").strip()
    body = quote or val
    if 单位 and val and 单位 not in val:
        body = f"{val}{单位}｜{quote}" if quote else f"{val}{单位}"
    出处 = s.get("报告") or "2025年度ESG报告"
    ind = (s.get("行业") or "").strip()
    tag = f"｜{ind}" if ind and ind not in ("未知", "未匹配") else ""
    return f"[{s.get('公司', '?')}{tag}] {body}（出处：{出处}）"


def build_external_facts(esg_json: dict, **kw) -> dict:
    """主入口：增强 JSON → 带出处的「外部事实块」。

    Args:
        esg_json: 增强 JSON（含 报告元数据 / 评分增强数据.障碍度Top10）
    Returns:
        {"启用": bool, "事实": [...], "文本块": str, "统计": {...}}
        任何异常都返回「启用=False + 原因」，绝不影响主流程。
    """
    try:
        if not PEER_ENABLED:
            return {"启用": False, "原因": "PEER_RETRIEVAL 已关闭", "事实": [], "文本块": "", "统计": {}}
        idx = load_index()
        if not idx:
            return {"启用": False, "原因": _CACHE["error"] or "索引不可用",
                    "事实": [], "文本块": "", "统计": {}}

        meta = idx.get("meta", {})
        by_metric = idx.get("by_metric", {})
        total = meta.get("覆盖", {}).get("公司数", 0) or 0
        built_at = meta.get("built_at", "")

        esg_json = _unwrap(esg_json)
        scored = (esg_json or {}).get("评分增强数据", {}) or {}
        my_ind = str(scored.get("行业", "") or "").strip()
        my_name = str(((esg_json or {}).get("报告元数据", {}) or {}).get("企业名称", "") or "").strip()
        top10 = scored.get("障碍度Top10") or []

        facts, lines, empty = [], [], []
        for item in top10[:MAX_METRICS]:
            if not isinstance(item, dict):
                continue
            metric = str(item.get("指标", "") or "").strip()
            lose = str(item.get("失分类型", "") or "").strip()
            entry = by_metric.get(metric)
            if not entry:
                empty.append(metric)
                continue

            covered = int(entry.get("全市场已披露", 0) or 0)
            ind_dist = entry.get("行业分布", {}) or {}
            same_cov = int(ind_dist.get(my_ind, 0) or 0) if my_ind else 0
            # v110 B-lite：此处**刻意不计算「同行业披露率」**。同行库的行业分桶（严格名称匹配，
            # 如化工新材料 10 家）与 weights.json 的行业样本（12 家）并非同一口径；若在此再算一个
            # 行业级比率，会引入第三个分母、加剧口径混乱。行业口径一律由 materiality / weights.json
            # 提供（文案里已标注为「本行业均值 X.XX（化工新材料 N 家）」）。
            rate = (covered / total * 100) if total else 0.0

            same, cross, same_avail = _pick_samples(entry.get("样本", []) or [], lose, my_ind, my_name)

            fact = {
                "指标": metric,
                "失分类型": lose,
                # v110 B-lite：键名一律带口径，避免「全市场数字」与「同行业数字」被混读
                "覆盖": {
                    "全市场已披露": covered,
                    "全市场样本": total,
                    "全市场披露率": round(rate, 1),      # 口径=全市场（分母 total）
                    "本企业行业": my_ind or "未知",        # 仅行业标签，非统计量
                    "同行业已披露家数": same_cov,          # 该指标在同行库本行业的已披露家数
                    "同行业可用样例": same_avail,          # 供引用采样的样例条数（与披露家数无关）
                },
                "稀缺度": _scarcity(covered, total),
                "同行业样例": same,
                "跨行业样例": cross,
            }
            facts.append(fact)

            # ---- 生成给 LLM 的文本块 ----
            head = (f"■ {metric} ｜ 失分类型：{lose}\n"
                    f"  覆盖：全市场 {covered}/{total} 家披露（{rate:.1f}%，全市场口径）")
            if my_ind and same_cov:
                head += f" ｜ 其中同行业「{my_ind}」{same_cov} 家"
            if same_avail:
                head += f" ｜ 同行业可用样例 {same_avail} 条"
            elif my_ind:
                head += " ｜ 同行业可用样本不足，本次以全市场对标"
            head += f" ｜ 稀缺度：{fact['稀缺度']}"
            lines.append(head)

            if same:
                for s in same:
                    lines.append("  · " + _fmt_sample(s))
            if cross:
                for s in cross:
                    lines.append("  · [跨行业] " + _fmt_sample(s))
            if not same and not cross:
                lines.append("  · （本次未检索到同行披露样例）"
                             + ("该指标全市场披露率偏低（全市场口径），属全市场尚未普及的议题。"
                                if fact["稀缺度"] == "高" else ""))

        if not facts:
            return {"启用": True, "语料规模": total, "构建时间": built_at,
                    "事实": [], "文本块": "", "统计": {"命中指标": 0, "未命中": empty}}

        header = (
            "【外部事实 · 离线同行对标库】\n"
            f"语料：{total} 家上市公司 ESG 报告原文（索引构建于 {built_at}）\n"
            "用途：核对「同一指标，同行是怎么写的、披露了什么数字」，把诊断从文本层抬到事实层。\n"
            "约束（必须遵守）：\n"
            "  1. 下列片段均为同行**已公开披露**的真实原文，引用时必须写明是哪家公司；不得改写、不得外推。\n"
            "  2. **未出现在此处的信息 = 本次未检索到**，禁止据此推断「该企业未开展相关工作」。\n"
            "  3. 本节所有披露率**均为全市场口径**（分母=同业库全部样本，非本行业）；"
            "某指标全市场披露率很低（稀缺度高）时，说明它在全市场范围尚未普及，"
            "提要求时应区分「行业普遍空白」与「本企业独有短板」。\n"
        )
        text = header + "\n" + "\n".join(lines)

        return {
            "启用": True,
            "语料规模": total,
            "构建时间": built_at,
            "事实": facts,
            "文本块": text,
            "统计": {
                "命中指标": len(facts),
                "未命中": empty,
                "同行业识别": my_ind or "未知",
                "文本块字符数": len(text),
            },
        }
    except Exception as e:                            # noqa: BLE001 — 检索层永不影响主链路
        return {"启用": False, "原因": f"检索层异常: {e}", "事实": [], "文本块": "", "统计": {}}


# ============================== 自测 ==============================
if __name__ == '__main__':
    import sys
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    os.environ.setdefault('PEER_INDEX_FILE',
                          os.path.join(os.path.dirname(os.path.abspath(__file__)), 'peer_index.json'))
    INDEX_FILE = os.environ['PEER_INDEX_FILE']

    print("索引状态:", json.dumps(index_status(), ensure_ascii=False, indent=2))

    # 用 batch_results 里的真实增强 JSON 试跑
    probe = os.environ.get("PEER_PROBE_JSON", "")
    if probe and os.path.exists(probe):
        with open(probe, encoding='utf-8') as f:
            raw = json.load(f)
        esg = (raw.get("result", {}) or {}).get("extracted", {}) or {}
        res = build_external_facts(esg)
        print("\n启用:", res["启用"], "| 统计:", res.get("统计"))
        print("\n---------- 文本块 ----------")
        print(res.get("文本块", "")[:4000])
