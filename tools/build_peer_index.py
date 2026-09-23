# -*- coding: utf-8 -*-
"""
构建「离线同行对标索引」—— P0 · D 方案（离线同行对标库）

背景：
  老师反馈根因分析"只说明文本情况"，因为证据池只有 `报告自述 + 行业均值`，
  诊断无法区分「做了但没披露」和「确实没做」。
  本脚本把手上的同行提取结果做成**离线倒排索引**，让 Agent2 能查到
  「同一指标，同行是怎么写的、披露了什么数字」→ 把诊断从文本层抬到事实层。

数据源：
  A. 桌面 `ESG-JSON-格式提取结果/*.json`（105 个，纯提取，含 原文 + 页码）
  B. `_perf/batch_results/*.json`        （30 个，提取 + 评分增强）

行业来源：
  `_perf/sample_pool.json`（133 家 PDF 清单，含「行业」字段）→ 公司名前缀匹配

产物：
  `peer_index.json` —— 按 L4 指标名倒排的同行披露样例 + 覆盖统计（静态文件，运行时零依赖）

用法：
  python build_peer_index.py --dry       # 只打印统计，不落盘
  python build_peer_index.py --out <path>
"""
import argparse
import glob
import json
import os
import sys
import io
from collections import Counter, defaultdict
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

_LOG = []


def emit(msg=""):
    """统一输出：既打印，也缓存待落盘（避免 PowerShell 管道二次编码乱码）。"""
    print(msg)
    _LOG.append(str(msg))

# ============================== 路径配置 ==============================
DESKTOP_BATCH = r"[DATA_ROOT]\ESG-JSON-格式提取结果"
PERF_BATCH = r"[WORKSPACE]/_perf\batch_results"
SAMPLE_POOL = r"[WORKSPACE]/_perf\sample_pool.json"

# ============================== 索引参数（体积控制） ==============================
MAX_SAMPLES_PER_METRIC = 40     # 每指标最多保留多少家同行样例
MAX_QUOTE_CHARS = 300           # 单条「原文」截断长度
MIN_QUOTE_CHARS = 8             # 短于此长度的原文视为无效

# ============================== 归一化 ==============================
DIM_ORDER = ["企业内部碳管理", "供应链碳管理", "透明度", "金融化情况"]

# 公司名去噪（用于跨源对齐同名企业）
NOISE = ["股份有限公司", "有限责任公司", "有限公司", "集团", "控股", "科技", "股份", "（", "(", " "]


def norm_company(name: str) -> str:
    """公司名归一化：去后缀噪音，用于跨源去重与匹配。"""
    s = (name or "").strip()
    for n in ["股份有限公司", "有限责任公司", "有限公司", "-", "－"]:
        s = s.replace(n, "")
    return s.strip()


_EN_BRACKET = None


def detect_lang(text: str) -> str:
    """判断片段语言：中文占比 < 20% 视为英文（数据源里混有英文版 ESG 报告）。

    检索时优先中文样例 —— 建议用中文写，引用英文片段会让报告读起来割裂。
    """
    import re
    t = str(text or "")
    if not t.strip():
        return "CN"
    cn = len(re.findall(r'[\u4e00-\u9fff]', t))
    en = len(re.findall(r'[A-Za-z]', t))
    if cn == 0 and en > 0:
        return "EN"
    if (cn + en) > 0 and cn / (cn + en) < 0.35:
        return "EN"
    return "CN"


def clean_company(name: str) -> str:
    """清洗公司名：剥掉带英文的括号注释（英文名/缩写），保留中文主体。

    例：「上海盛剑科技股份有限公司（Shanghai Shengjian Technology Co., Ltd.）」
        → 「上海盛剑科技股份有限公司」
    纯中文括号（如「孚能科技（赣州）股份有限公司」）保留，因为它属于法定名称。
    """
    import re
    s = (name or "").strip()
    s = re.sub(r"[（(][^）)]*[A-Za-z][^）)]*[）)]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or (name or "").strip()


def extract_l4(指标提取结果: dict):
    """遍历 4 维度 → 子域 → L4 指标 → 叶子 dict。

    两批数据结构一致：
      {维度: {"指标": {子域: {L4名: {值, 单位?, 年份?, 原文, 数据类型?}}}, "未覆盖指标": []}}
    """
    if not isinstance(指标提取结果, dict):
        return
    for 维度, dv in 指标提取结果.items():
        if not isinstance(dv, dict):
            continue
        指标区 = dv.get("指标", dv)  # 容错：部分版本可能少一层「指标」
        if not isinstance(指标区, dict):
            continue
        for 子域, sv in 指标区.items():
            if not isinstance(sv, dict):
                continue
            for l4名, leaf in sv.items():
                if not isinstance(leaf, dict):
                    continue
                if "值" not in leaf and "原文" not in leaf:
                    continue
                yield 维度, 子域, l4名, leaf


def build_industry_map():
    """从 sample_pool.json 建「公司简称/全名 → 行业」映射。"""
    m = {}
    if not os.path.exists(SAMPLE_POOL):
        return m
    try:
        with open(SAMPLE_POOL, encoding="utf-8") as f:
            pool = json.load(f)
    except Exception as e:
        print(f"[WARN] sample_pool 读取失败: {e}")
        return m
    for it in pool if isinstance(pool, list) else []:
        公司 = (it.get("公司") or "").strip()
        行业 = (it.get("行业") or "").strip()
        fname = it.get("file") or ""
        有效 = 行业 not in ("", "未匹配", "未知", "无")
        if 有效 and 公司:
            m[公司] = 行业
        # 用文件名里的全名（"简称：全名...pdf"）补一条全名索引
        if 有效 and "：" in fname:
            全名 = fname.split("：", 1)[1]
            for suf in [".pdf", "2025年", "2025年度", "（ESG）报告", "ESG报告"]:
                全名 = 全名.replace(suf, "")
            if len(全名) >= 4:
                m[全名] = 行业
    return m


def match_industry(company: str, industry_map: dict) -> str:
    """行业匹配：清洗后 全名精确 → 全名/简称前缀包含 → 未知。

    注：宁可留「未知」，也不做关键词猜测行业 —— 猜错会把「同行业对标」引到别的行业，
    对评委而言是硬伤。未知时检索层会自动降级为「全市场对标」，仍然有效。
    """
    c = clean_company(company)
    if not c:
        return "未知"
    if c in industry_map:
        return industry_map[c]
    for k, v in industry_map.items():
        if len(k) >= 3 and (c.startswith(k) or k.startswith(c)):
            return v
    key = c[:4]
    for k, v in industry_map.items():
        if len(key) >= 3 and key in k:
            return v
    return "未知"


# ============================== 采集 ==============================
def collect(dry: bool = False):
    """两批数据 → {公司: {行业, 源, 指标: [...]}}"""
    industry_map = build_industry_map()
    companies = {}          # key: 归一化公司名
    stat = Counter()
    fails = []

    def add(company, 行业, 源, meta, 提取结果, fname):
        company = clean_company(company)
        报告名 = str(meta.get("报告名称", "") or "")
        key = norm_company(company)
        if not key:
            return
        rec = companies.get(key)
        if rec is None:
            rec = companies[key] = {
                "公司": company, "行业": 行业, "源": 源, "文件": fname,
                "报告": str(meta.get("报告名称", "") or "").strip()[:80],
                "指标": {}
            }
        else:
            # 同源多文件：保留信息更全的那份（指标数更多）
            stat["重复公司"] += 1
            if 行业 != "未知" and rec["行业"] == "未知":
                rec["行业"] = 行业
        for 维度, 子域, l4名, leaf in extract_l4(提取结果):
            值 = str(leaf.get("值", "") or "").strip()
            原文 = str(leaf.get("原文", "") or "").strip()
            数据类型 = str(leaf.get("数据类型", "") or "").strip()
            if not 数据类型:
                数据类型 = "未披露" if (值 in ("未披露", "", "无") or 值.startswith("未披露")) else "定性"
            rec["指标"].setdefault(l4名, {
                "维度": 维度, "子域": 子域, "值": 值[:400],
                "单位": str(leaf.get("单位", "") or "").strip()[:40],
                "原文": 原文[:MAX_QUOTE_CHARS],
                "数据类型": 数据类型,
                "语言": detect_lang((原文 or 值) + " " + 报告名),
            })
            stat["条目"] += 1

    # ---- 源 A：桌面 105 家（纯提取）----
    fa = sorted(glob.glob(os.path.join(DESKTOP_BATCH, "*.json")))
    for fp in fa:
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            stat["A解析失败"] += 1
            fails.append(("A", os.path.basename(fp), str(e)[:120]))
            emit(f"[WARN] A 解析失败 {os.path.basename(fp)}: {e}")
            continue
        md = d.get("报告元数据", {}) or {}
        company = md.get("企业名称") or os.path.basename(fp).split("-")[0].split("_")[0]
        行业 = (md.get("所属行业") or "").strip() or match_industry(company, industry_map)
        add(company, 行业, "desktop", md, d.get("指标提取结果", {}), os.path.basename(fp))
        stat["A文件"] += 1

    # ---- 源 B：batch_results 30 家（含评分）----
    fb = sorted(glob.glob(os.path.join(PERF_BATCH, "*.json")))
    for fp in fb:
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            stat["B解析失败"] += 1
            fails.append(("B", os.path.basename(fp), str(e)[:120]))
            emit(f"[WARN] B 解析失败 {os.path.basename(fp)}: {e}")
            continue
        ext = (d.get("result", {}) or {}).get("extracted", {}) or {}
        md = ext.get("报告元数据", {}) or {}
        company = md.get("企业名称") or (d.get("item", {}) or {}).get("公司", "")
        行业 = (md.get("所属行业") or "").strip() or (d.get("item", {}) or {}).get("行业", "") or "未知"
        if 行业 == "未匹配":
            行业 = match_industry(company, industry_map)
        add(company, 行业, "batch", md, ext.get("指标提取结果", {}), os.path.basename(fp))
        stat["B文件"] += 1

    return companies, stat, industry_map, fails


# ============================== 构索引 ==============================
def build_index(companies: dict):
    by_metric = defaultdict(lambda: {"样本": [], "行业分布": Counter()})
    all_l4 = Counter()
    dim_of = {}

    for key, rec in companies.items():
        行业 = rec["行业"]
        for l4名, info in rec["指标"].items():
            all_l4[l4名] += 1
            dim_of.setdefault(l4名, (info["维度"], info["子域"]))
            值 = info["值"]
            原文 = info["原文"]
            # 只收「已披露」的样例 —— 失分企业缺的正是这个
            # 【A 修复 v109】判据以「值」字段为唯一真相来源，移除「原文兜底」路径。
            # 旧版：披露 = not(值判未披露) or (原文 and not 原文.startswith("报告未") and len(原文) >= MIN_QUOTE_CHARS)
            #   原意是用「原文」捞回定性披露；但实际模板句是「报告**中**未提及XX相关内容」，
            #   多一个「中」字使 startswith("报告未") 失配 → 排除失效；此时 OR 前半已判 False，
            #   整条被误计为「已披露」（实测 889 条；绿色信贷「同行覆盖 20.6%」虚高 10 倍即源于此）。
            # 实测该 889 条中 842 条含否定词（「未提」占 629），余者多为英文 No/not 否定句，
            #   含「但/然而」转折者 79 条 —— 语义均属「该指标本体未披露」，故不再用原文兜底。
            披露 = not (值 in ("", "未披露", "无") or 值.startswith("未披露"))
            if not 披露:
                continue
            if len(原文) < MIN_QUOTE_CHARS and len(值) < MIN_QUOTE_CHARS:
                continue
            by_metric[l4名]["样本"].append({
                "公司": rec["公司"],
                "行业": 行业,
                "报告": rec.get("报告", ""),
                "源": rec["源"],
                "值": 值[:400],
                "单位": info["单位"],
                "原文": 原文,
                "数据类型": info["数据类型"],
                "语言": info.get("语言", "CN"),
            })
            by_metric[l4名]["行业分布"][行业] += 1

    out = {}
    for l4名, blob in by_metric.items():
        samples = blob["样本"]
        # 排序：中文优先 → 定量优先 → 原文更完整（信息含量高）
        samples.sort(key=lambda s: (
            0 if s.get("语言") == "CN" else 1,
            0 if s["数据类型"] in ("定量", "定量/定性") else 1,
            -len(s["原文"]),
        ))
        kept = samples[:MAX_SAMPLES_PER_METRIC]
        维度, 子域 = dim_of.get(l4名, ("", ""))
        out[l4名] = {
            "维度": 维度,
            "子域": 子域,
            "全市场已披露": len(samples),
            "行业分布": dict(blob["行业分布"].most_common()),
            "样本": kept,
        }

    # 【A 修复 v109 配套】为「全市场零披露」的指标补键。
    # 旧版只收已披露样例 ⇒ 全程无人披露的指标在 by_metric 里没有键 ⇒ _peer_rate 返回 rate=None
    #   ⇒ judge_applicability 退化成「本次未取到同行覆盖数据，依据较弱」（等级降为「可选」），
    #     反而丢掉了「同行覆盖 0%、行业均值 0 → 行业尚未普遍建立的新兴能力（判据 L4）」这一最有价值的结论。
    # 实测 v109 修复后有 5 个指标落入此情形（碳资产交易收益 / 碳金融衍生品参与情况 / 高碳供应商占比 /
    #   气候风险暴露供应商数量 / 碳合规风险事件发生率），全部集中在「金融化情况」与「供应链碳管理」，
    #   它们是体系中最稀缺、对标价值最高的一批，必须能被正确表达。
    # 注：只补键、不改样本（样本仍只收已披露的），故 全市场已披露 = 0 是对真实分布的准确表达；
    #     all_l4 与体系 42 项实测完全对齐（无多余、无缺失）。
    for _l4 in all_l4:
        if _l4 in out:
            continue
        _dim, _sub = dim_of.get(_l4, ("", ""))
        out[_l4] = {
            "维度": _dim,
            "子域": _sub,
            "全市场已披露": 0,
            "行业分布": {},
            "样本": [],
        }

    meta = {
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "参数": {"每指标上限": MAX_SAMPLES_PER_METRIC, "原文截断": MAX_QUOTE_CHARS},
        "覆盖": {
            "公司数": len(companies),
            "指标数": len(out),
            "行业分布": dict(Counter(r["行业"] for r in companies.values()).most_common()),
        },
    }
    return {"meta": meta, "by_metric": out}


# ============================== MAIN ==============================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=r"[PROJECT]\agent2\peer_index.json")
    ap.add_argument("--log", default=r"[WORKSPACE]/_perf\peer_build.log")
    ap.add_argument("--dry", action="store_true", help="只打印统计，不落盘")
    args = ap.parse_args()

    companies, stat, industry_map, fails = collect(args.dry)
    emit("=" * 70)
    emit("【采集统计】")
    emit(f"  A 桌面批次文件: {stat['A文件']}  |  B 批次文件: {stat['B文件']}")
    emit(f"  解析失败: A={stat['A解析失败']} B={stat['B解析失败']}  |  同名企业合并: {stat['重复公司']}")
    emit(f"  sample_pool 行业映射条目: {len(industry_map)}")
    emit(f"  汇总公司数: {len(companies)}  |  L4 条目总数: {stat['条目']}")
    if fails:
        emit("\n【解析失败明细】")
        for src, fn, err in fails:
            emit(f"  [{src}] {fn} -> {err}")

    ind = Counter(r["行业"] for r in companies.values())
    emit(f"\n【行业分布（{len(ind)} 个）】")
    for k, v in ind.most_common():
        emit(f"  {k}: {v}")
    matched = sum(v for k, v in ind.items() if k not in ("未知", "未匹配"))
    emit(f"  行业匹配率: {matched}/{len(companies)} = {matched * 100 // max(1, len(companies))}%")
    unknown = [r["公司"] for r in companies.values() if r["行业"] in ("未知", "未匹配")]
    if unknown:
        emit(f"  未匹配行业企业（{len(unknown)}）: {', '.join(unknown[:40])}")

    index = build_index(companies)
    index["meta"]["解析失败"] = fails
    bm = index["by_metric"]
    emit(f"\n【索引】收录指标数: {len(bm)}")
    emit("\n  已披露家数 Top15（数字越大 = 越普及，越小 = 越稀缺/越有对标价值）:")
    for l4名, b in sorted(bm.items(), key=lambda kv: -kv[1]["全市场已披露"])[:15]:
        emit(f"    {l4名}（{b['维度']}）: 已披露 {b['全市场已披露']} 家")
    emit("\n  已披露家数 Bottom10（稀缺指标 = 对标证据最稀缺、最需要外部检索补位）:")
    for l4名, b in sorted(bm.items(), key=lambda kv: kv[1]["全市场已披露"])[:10]:
        emit(f"    {l4名}（{b['维度']}）: 已披露 {b['全市场已披露']} 家")

    if args.dry:
        emit("\n[DRY] 未落盘。")
        with open(args.log, "w", encoding="utf-8") as f:
            f.write("\n".join(_LOG))
        return

    txt = json.dumps(index, ensure_ascii=False, separators=(",", ":"))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(txt)
    emit(f"\n[OK] 写入 {args.out}")
    emit(f"     体积 {len(txt.encode('utf-8')) / 1024:.1f} KB")
    with open(args.log, "w", encoding="utf-8") as f:
        f.write("\n".join(_LOG))


if __name__ == "__main__":
    main()
