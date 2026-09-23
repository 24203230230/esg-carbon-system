# -*- coding: utf-8 -*-
"""
外部信息检索层（Agent 1.5 · A 方案）—— 联网检索企业公开事实

职责
    给定「企业名称 + 失分指标」，去**公网**检索该企业的公开信息
    （公告原文、披露文件、外部报道）→ 输出带来源与日期的「外部事实块」，
    供智能体二把诊断从「报告自述层」抬到「公开事实层」。

为什么要有这一层（与 D 方案的边界）
    · D 方案（peer_retrieval.py）= 离线同行对标库，回答
      「**别的公司**是怎么写的」→ 解决「行业惯例 / 普遍空白」。
    · A 方案（本模块）= 在线企业检索，回答
      「**这家公司自己**做过什么、说过什么」→ 解决「做了但没披露」。
    两者互补：D 给横向基准，A 给纵向事实。缺了 A，诊断只能停在
    「建议补充披露」，无法区分「做了没披露」与「确实没做」。

数据源分层（免费优先，付费可插拔）
    ┌ 免费层（无需 Key，实测可用，权威度高）
    │   · eastmoney  东方财富公告：公司公告列表 + **公告正文全文**
    │   · cninfo     巨潮资讯网：全站公告全文检索（证监会指定披露平台）
    └ 付费层（配了 Key 才启用，自动跳过未配置项）
        · bocha     博查 Web Search（DeepSeek 官方联网搜索供应商）
        · qianfan   百度千帆 AI 搜索（100 次/天免费额度）
        · zhipu     智谱 Web Search
        · tavily    Tavily Search

检索质量的关键（v2 经验）
    第一版直接用「企业名 + 指标关键词」查巨潮，结果全是 2008~2019 年的
    《信息披露事务管理制度》这类**泛化老文件**，真正有用的 ESG 报告被埋掉。
    因此本版引入三道工序：
      ① **专属检索式**：每个指标配 `query`（用于检索）+ `match`（用于命中判定），
         两者分离，避免「信息披露」这种泛词污染结果；
      ② **相关度打分**：主题词命中 / ESG 强相关词 / 时效 / 文件类型综合加权；
      ③ **时效与类型过滤**：剔除 10 年前的制度类文件，同一文件只留最新版。

设计约束
    1. **零编造**：只回传真实抓取的标题/正文/日期，不做任何 LLM 概括与改写；
       字段拿不到就留空，绝不用推测填充。
    2. **不阻塞主链路**：网络异常 / 超时 / 解析失败一律静默降级返回空事实块；
       并发 + 超时 + 结果上限，单次调用总耗时控制在数秒内。
    3. **可复现**：检索规则由配置表写死，不依赖 LLM 临时决定查什么。

环境变量
    WEB_SEARCH          on/off，默认 on
    SEARCH_TIMEOUT      单请求超时秒数，默认 6
    SEARCH_MAX_FACTS    对外输出的事实条数上限，默认 12
    SEARCH_MAX_DETAIL   抓取公告正文的条数上限（正文较大，需限流），默认 4
    SEARCH_MIN_YEAR     早于此年份的制度类文件直接丢弃，默认 2015
    BOCHA_API_KEY / QIANFAN_API_KEY / ZHIPU_API_KEY / TAVILY_API_KEY
"""
import datetime
import json
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================== 配置 ==============================
WEB_ENABLED = os.environ.get('WEB_SEARCH', 'on').lower() not in ('0', 'off', 'false', 'no', '')
TIMEOUT = float(os.environ.get('SEARCH_TIMEOUT', '6'))
MAX_FACTS = int(os.environ.get('SEARCH_MAX_FACTS', '12'))
MAX_DETAIL = int(os.environ.get('SEARCH_MAX_DETAIL', '4'))
MAX_BODY_CHARS = int(os.environ.get('SEARCH_BODY_CHARS', '400'))
MAX_TITLE_CHARS = int(os.environ.get('SEARCH_TITLE_CHARS', '70'))
MIN_YEAR = int(os.environ.get('SEARCH_MIN_YEAR', '2015'))
WORKERS = int(os.environ.get('SEARCH_WORKERS', '6'))

BOCHA_KEY = os.environ.get('BOCHA_API_KEY', '').strip()
QIANFAN_KEY = os.environ.get('QIANFAN_API_KEY', '').strip()
ZHIPU_KEY = os.environ.get('ZHIPU_API_KEY', '').strip()
TAVILY_KEY = os.environ.get('TAVILY_API_KEY', '').strip()

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')

# 显式绕开系统代理：校园网 HTTP_PROXY 会污染出站请求（历史坑）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ============================== 指标检索规则表（规则式②） ==============================
# query = 送给搜索引擎的检索词（要具体，避免泛词）
# match = 判定「这条结果是否与该指标相关」的主题词
# 对应计划书 §1.6 的「② 规则式」：查什么词写死在配置表里，结果可复现。
METRIC_SPEC = {
    # —— 企业内部碳管理 ——
    "碳总量": {"query": ["温室气体排放", "碳排放"], "match": ["碳排放", "温室气体", "碳总量", "双碳", "碳达峰"]},
    "碳排放强度": {"query": ["碳排放强度"], "match": ["碳排放强度", "碳强度", "碳排放"]},
    "范围一排放": {"query": ["温室气体排放"], "match": ["范围一", "范围1", "温室气体", "碳排放"]},
    "范围二排放": {"query": ["温室气体排放"], "match": ["范围二", "范围2", "温室气体", "碳排放"]},
    "范围三排放": {"query": ["范围三", "价值链排放"], "match": ["范围三", "范围3", "价值链", "供应链排放"]},
    "碳排放抵消与同比变化": {"query": ["碳抵消", "绿证"], "match": ["碳抵消", "碳中和", "碳汇", "绿证", "绿电"]},
    "环保投资": {"query": ["环保投入", "污染防治"], "match": ["环保投入", "环保投资", "环保资金", "污染防治", "环保"]},
    "碳管理体系建设": {"query": ["碳管理", "碳达峰碳中和"], "match": ["碳管理", "双碳", "碳达峰", "碳中和", "气候"]},
    "环境管理体系": {"query": ["环境管理体系认证"], "match": ["环境管理体系", "ISO14001", "环保合规", "环境认证"]},
    "能源类型和强度": {"query": ["能源消耗", "综合能耗"], "match": ["能源消耗", "综合能耗", "能源强度", "节能", "能源"]},
    "能耗同比变化": {"query": ["能源消耗", "节能量"], "match": ["能耗", "节能量", "能源消耗", "节能"]},
    "节能目标与改造情况": {"query": ["节能改造", "能效提升"], "match": ["节能", "能效", "技改", "改造"]},
    "能效提升对比度": {"query": ["能效提升", "节能技改"], "match": ["能效", "节能", "技改"]},
    # —— 供应链碳管理 ——
    "供应商碳目标": {"query": ["绿色供应链", "供应商碳减排"], "match": ["供应商", "供应链", "绿色采购", "责任采购"]},
    "供应商碳披露及评价要求": {"query": ["绿色供应链管理"], "match": ["供应商", "供应链", "绿色采购", "采购"]},
    "供应商碳培训及考核情况": {"query": ["供应商管理"], "match": ["供应商", "供应链", "培训", "考核"]},
    "供应商绿色采购标准": {"query": ["绿色采购", "责任采购"], "match": ["绿色采购", "责任采购", "采购", "供应商"]},
    "供应商激励": {"query": ["绿色供应商"], "match": ["供应商", "激励", "供应链"]},
    "绿色物流": {"query": ["绿色物流", "绿色运输"], "match": ["绿色物流", "物流", "运输", "绿色包装"]},
    "包装减量及回收情况": {"query": ["包装回收", "绿色包装"], "match": ["包装", "回收", "循环"]},
    "循环经济链塑造": {"query": ["循环经济", "资源回收"], "match": ["循环经济", "资源回收", "废旧", "余料", "再利用"]},
    "高碳供应商占比": {"query": ["供应链碳排放"], "match": ["供应商", "供应链", "碳排放"]},
    "气候风险暴露供应商数量": {"query": ["气候风险"], "match": ["气候风险", "供应链风险", "风险"]},
    "碳合规风险事件发生率": {"query": ["环境处罚", "环境违法"], "match": ["环境处罚", "环保处罚", "环境违法", "碳合规"]},
    "供应链碳风险传导影响评估": {"query": ["供应链碳风险"], "match": ["供应链", "碳风险", "气候风险"]},
    # —— 透明度 ——
    "碳排放披露范围": {"query": ["温室气体排放核算"], "match": ["碳排放", "温室气体", "核算", "披露范围"]},
    "供应链碳披露层级": {"query": ["供应链碳披露"], "match": ["供应链", "范围三", "披露"]},
    "碳资产与交易信息披露比例": {"query": ["碳资产", "碳交易"], "match": ["碳资产", "碳交易", "碳配额", "CCER", "绿证"]},
    "第三方核查覆盖率": {"query": ["碳核查", "温室气体核查"], "match": ["碳核查", "温室气体核查", "第三方核查", "鉴证"]},
    "ESG评级结果及行业排名": {"query": ["ESG评级", "ESG报告"], "match": ["ESG", "评级", "可持续发展"]},
    "ISO14064/ISO50001认证": {"query": ["ISO14064", "ISO50001能源管理体系"], "match": ["ISO14064", "ISO50001", "能源管理体系", "温室气体核查"]},
    "信息披露频次": {"query": ["可持续发展报告", "ESG报告"], "match": ["ESG报告", "可持续发展报告", "社会责任报告", "信息披露"]},
    "监管强制披露要求符合性": {"query": ["可持续发展报告", "信息披露管理制度"], "match": ["可持续发展报告", "信息披露", "ESG报告", "治理"]},
    "交易所ESG披露指引符合性": {"query": ["可持续发展报告", "ESG披露"], "match": ["可持续发展报告", "ESG", "披露", "指引"]},
    "历史信息披露更正次数": {"query": ["更正公告", "补充公告"], "match": ["更正", "补充", "信息披露"]},
    # —— 金融化情况 ——
    "绿色信贷": {"query": ["绿色信贷", "绿色贷款"], "match": ["绿色信贷", "绿色贷款", "绿色金融", "授信"]},
    "绿色债券": {"query": ["绿色债券", "债务融资工具"], "match": ["绿色债券", "债券", "融资", "可持续挂钩"]},
    "碳金融衍生品参与情况": {"query": ["碳金融"], "match": ["碳金融", "碳期货", "碳衍生品", "碳交易"]},
    "碳配额管理": {"query": ["碳配额", "碳排放权"], "match": ["碳配额", "碳排放权", "碳市场", "碳交易"]},
    "CCER开发与储备": {"query": ["CCER", "自愿减排"], "match": ["CCER", "自愿减排", "碳资产"]},
    "碳资产交易收益": {"query": ["碳资产", "碳交易收益"], "match": ["碳资产", "碳交易", "收益"]},
    "碳资产保值增值策略": {"query": ["碳资产管理"], "match": ["碳资产", "碳管理"]},
}

# ESG 强相关词：出现在标题里说明这份文件与碳/环境直接相关
ESG_STRONG = ["碳", "ESG", "可持续", "气候", "环境", "环保", "绿色", "节能", "社会责任",
              "生态", "污染防治", "能源管理", "循环经济"]
# 制度 / 治理类文件：有制度价值，但通常不是「披露数据」的证据
GOVERNANCE = ["管理制度", "管理办法", "议事规则", "工作细则", "内部控制", "工作制度", "公司章程"]
# 明显是「附件 / 摘要 / 更正」等次要文件，降权
SECONDARY = ["摘要", "附件", "英文版", "意见", "法律意见书", "核查意见", "述职报告"]


def metric_spec(metric: str) -> dict:
    """取指标的检索规则；未收录的指标退化用指标名本身。"""
    m = str(metric or '').strip()
    spec = METRIC_SPEC.get(m)
    if spec:
        return spec
    return {"query": [m] if m else [], "match": [m] if m else []}


# ============================== 状态探针 ==============================
def provider_status() -> dict:
    """给 /api/health 用：哪些 provider 可用。"""
    return {
        "启用": WEB_ENABLED,
        "免费层": ["东方财富公告", "巨潮资讯检索"],
        "付费层已配置": [n for n, k in (
            ("博查", BOCHA_KEY), ("百度千帆", QIANFAN_KEY),
            ("智谱", ZHIPU_KEY), ("Tavily", TAVILY_KEY)) if k],
        "超时秒": TIMEOUT,
    }


# ============================== HTTP 工具 ==============================
def _http(url, *, data=None, headers=None, timeout=None):
    """统一 GET/POST，返回文本；失败抛异常（由上层捕获降级）。"""
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    req = urllib.request.Request(url, data=body, headers=h)
    with _OPENER.open(req, timeout=timeout or TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _json(url, **kw):
    return json.loads(_http(url, **kw))


def _clean(s, n=None) -> str:
    """清洗文本：去 HTML 标签、压空白、可选截断。"""
    s = str(s or "")
    s = re.sub(r"<[^>]{0,40}>", "", s)          # 去标签（含巨潮的 <em>）
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = " ".join(s.split())
    if n and len(s) > n:
        s = s[:n] + "…"
    return s


def _cut(s, n: int) -> str:
    s = str(s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _year_of(date_str: str) -> int:
    m = re.match(r"(\d{4})", str(date_str or ""))
    return int(m.group(1)) if m else 0


def _norm_title(t: str) -> str:
    """标题归一化：去企业名前缀与年份括号，用于识别「同一份文件的不同年份版本」。"""
    t = re.sub(r"^[^：:]{2,20}[：:]", "", str(t or ""))     # 去「国电南瑞：」前缀
    t = re.sub(r"[（(]\s*\d{4}\s*年?\s*\d*月?\s*[)）]", "", t)  # 去「(2013年7月)」
    t = re.sub(r"\d{4}\s*年?", "", t)
    return re.sub(r"\s+", "", t)


# ============================== 免费层 1：东方财富公告 ==============================
def em_announcements(stock_code: str, limit: int = 60) -> list:
    """东方财富：按股票代码取公告列表。返回 [{标题, art_code, 日期, 分类}]。"""
    if not stock_code:
        return []
    url = ("https://np-anotice-stock.eastmoney.com/api/security/ann"
           "?sr=-1&page_size=%d&page_index=1&ann_type=A&client_source=web"
           "&stock_list=%s" % (min(limit, 100), urllib.parse.quote(str(stock_code))))
    j = _json(url)
    out = []
    for it in ((j.get("data") or {}).get("list") or []):
        cats = [c.get("column_name", "") for c in (it.get("columns") or [])]
        out.append({
            "标题": _clean(it.get("title", "")),
            "art_code": it.get("art_code", ""),
            "日期": (it.get("notice_date") or "")[:10],
            "分类": "、".join([c for c in cats if c]),
            "来源": "东方财富公告",
        })
    return out


def em_announcement_body(art_code: str, limit: int = MAX_BODY_CHARS) -> str:
    """东方财富：取单条公告正文（去标签后截断）。"""
    if not art_code:
        return ""
    url = ("https://np-cnotice-stock.eastmoney.com/api/content/ann"
           "?art_code=%s&client_source=web&page_index=1" % urllib.parse.quote(str(art_code)))
    j = _json(url)
    body = (j.get("data") or {}).get("notice_content") or ""
    return _clean(re.sub(r"^.*?公告编号[：:]\s*\S+", "", _clean(body)), limit)


# ============================== 免费层 2：巨潮资讯网 ==============================
def cninfo_search(keyword: str, limit: int = 10, isfulltext: bool = True) -> list:
    """巨潮资讯网（证监会指定披露平台）：公告全文检索。

    返回 [{标题, 公司, 代码, 日期, pdf, 来源}]；同时可用于「企业名 → 股票代码」反查。
    """
    kw = str(keyword or "").strip()
    if not kw:
        return []
    url = ("http://www.cninfo.com.cn/new/fulltextSearch/full?searchkey=%s"
           "&sdate=&edate=&isfulltext=%s&sortName=nothing&sortType=desc&pageNum=1"
           % (urllib.parse.quote(kw), "true" if isfulltext else "false"))
    j = _json(url)
    out = []
    for a in (j.get("announcements") or [])[:limit]:
        ts = a.get("announcementTime")
        ds = ""
        if ts:
            try:
                ds = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
            except Exception:                        # noqa: BLE001
                ds = ""
        out.append({
            "标题": _clean(a.get("announcementTitle", ""), MAX_TITLE_CHARS),
            "公司": _clean(a.get("secName", "")),
            "代码": _clean(a.get("secCode", "")),
            "日期": ds,
            "pdf": ("http://static.cninfo.com.cn/" + a["adjunctUrl"]) if a.get("adjunctUrl") else "",
            "来源": "巨潮资讯网",
        })
    return out


def resolve_stock_code(company: str) -> str:
    """企业名称 → 股票代码（借道巨潮检索；失败返回空串）。"""
    try:
        hits = cninfo_search(company, limit=5, isfulltext=False)
        name0 = re.sub(r"[（(].*?[)）]", "", str(company or ""))
        for suf in ("股份有限公司", "有限公司", "集团"):
            name0 = name0.replace(suf, "")
        for h in hits:
            if not h.get("代码"):
                continue
            nm = h.get("公司", "")
            if not name0 or name0[:3] in nm or nm[:3] in name0:
                return h["代码"]
        return hits[0]["代码"] if hits else ""
    except Exception:                                # noqa: BLE001
        return ""


# ============================== 付费层（可插拔） ==============================
def bocha_search(query: str, limit: int = 8) -> list:
    """博查 Web Search API（DeepSeek 官方联网搜索供应商）。"""
    if not BOCHA_KEY:
        return []
    payload = json.dumps({"query": query, "count": limit, "summary": True,
                          "freshness": "noLimit"}).encode()
    req = urllib.request.Request("https://api.bochaai.com/v1/web-search", data=payload,
                                 headers={"Authorization": "Bearer " + BOCHA_KEY,
                                          "Content-Type": "application/json"})
    with _OPENER.open(req, timeout=TIMEOUT) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    pages = (((j.get("data") or {}).get("webPages") or {}).get("value")) or []
    return [{
        "标题": _clean(p.get("name", ""), MAX_TITLE_CHARS),
        "正文": _clean(p.get("snippet") or p.get("summary") or "", MAX_BODY_CHARS),
        "日期": (p.get("datePublished") or "")[:10],
        "URL": p.get("url", ""),
        "来源": "博查搜索",
    } for p in pages[:limit]]


def qianfan_search(query: str, limit: int = 8) -> list:
    """百度千帆 AI 搜索（免费额度 100 次/天）。"""
    if not QIANFAN_KEY:
        return []
    payload = json.dumps({"messages": [{"role": "user", "content": query}],
                          "search_source": "baidu_search_v2",
                          "resource_type_filter": [{"type": "web", "top_k": limit}]}).encode()
    req = urllib.request.Request("https://qianfan.baidubce.com/v2/ai_search/web_search",
                                 data=payload,
                                 headers={"Authorization": "Bearer " + QIANFAN_KEY,
                                          "Content-Type": "application/json"})
    with _OPENER.open(req, timeout=TIMEOUT) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    return [{
        "标题": _clean(r.get("title", ""), MAX_TITLE_CHARS),
        "正文": _clean(r.get("content", ""), MAX_BODY_CHARS),
        "日期": (r.get("date") or "")[:10],
        "URL": r.get("url", ""),
        "来源": "百度千帆搜索",
    } for r in (j.get("references") or [])[:limit]]


def zhipu_search(query: str, limit: int = 8) -> list:
    """智谱 Web Search API。"""
    if not ZHIPU_KEY:
        return []
    payload = json.dumps({"search_engine": "search_std", "search_query": query,
                          "count": limit}).encode()
    req = urllib.request.Request("https://open.bigmodel.cn/api/paas/v4/web_search",
                                 data=payload,
                                 headers={"Authorization": "Bearer " + ZHIPU_KEY,
                                          "Content-Type": "application/json"})
    with _OPENER.open(req, timeout=TIMEOUT) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    return [{
        "标题": _clean(it.get("title", ""), MAX_TITLE_CHARS),
        "正文": _clean(it.get("content", ""), MAX_BODY_CHARS),
        "日期": (it.get("publish_date") or "")[:10],
        "URL": it.get("link", ""),
        "来源": "智谱搜索",
    } for it in (j.get("search_result") or [])[:limit]]


def tavily_search(query: str, limit: int = 8) -> list:
    """Tavily Search API（免费 1000 次/月）。"""
    if not TAVILY_KEY:
        return []
    payload = json.dumps({"api_key": TAVILY_KEY, "query": query,
                          "max_results": limit, "include_answer": False}).encode()
    req = urllib.request.Request("https://api.tavily.com/search", data=payload,
                                 headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=TIMEOUT) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    return [{
        "标题": _clean(it.get("title", ""), MAX_TITLE_CHARS),
        "正文": _clean(it.get("content", ""), MAX_BODY_CHARS),
        "日期": (it.get("published_date") or "")[:10],
        "URL": it.get("url", ""),
        "来源": "Tavily搜索",
    } for it in (j.get("results") or [])[:limit]]


PAID_PROVIDERS = [("博查搜索", bocha_search), ("百度千帆搜索", qianfan_search),
                  ("智谱搜索", zhipu_search), ("Tavily搜索", tavily_search)]
PAID_CONFIGURED = bool(BOCHA_KEY or QIANFAN_KEY or ZHIPU_KEY or TAVILY_KEY)


def paid_search(query: str, limit: int = 6) -> list:
    """依次尝试已配置的付费 provider，任一成功即返回（避免重复花钱）。"""
    for _name, fn in PAID_PROVIDERS:
        try:
            res = fn(query, limit)
            if res:
                return res
        except Exception:                            # noqa: BLE001 — 单 provider 失败不影响其它
            continue
    return []


# ============================== 相关度打分与排序 ==============================
def _score(item: dict, match: list, metric: str) -> float:
    """相关度打分：主题命中 + ESG 强相关 + 时效 + 文件类型。"""
    title = str(item.get("标题") or "")
    s = 0.0

    # ① 主题词命中（核心）
    if match:
        hits = sum(1 for k in match if k and k in title)
        s += min(hits, 3) * 3.0
    if metric and metric in title:
        s += 2.0

    # ② ESG 强相关
    if any(k in title for k in ESG_STRONG):
        s += 2.0

    # ③ 时效（近 3 年最相关；越老越减）
    y = _year_of(item.get("日期", ""))
    ty = datetime.date.today().year
    if y:
        if y >= ty - 1:
            s += 3.0
        elif y >= ty - 3:
            s += 2.0
        elif y >= ty - 6:
            s += 0.5
        elif y < MIN_YEAR:
            s -= 6.0
        else:
            s -= 1.5

    # ④ 文件类型
    if any(k in title for k in GOVERNANCE):
        s -= 1.0
    if any(k in title for k in SECONDARY):
        s -= 1.5
    if "报告" in title:
        s += 1.0
    if str(item.get("来源")) == "东方财富公告":
        s += 0.5   # 东财是近期公告，时效更可靠

    # ⑤ 无正文的条目标题信息量低，略降
    if not item.get("正文"):
        s -= 0.5
    return s


def _dedup_keep_newest(items: list) -> list:
    """标题归一化后去重，同一文件只保留最新一版。"""
    best = {}
    for it in items:
        k = _norm_title(it.get("标题", ""))[:22]
        old = best.get(k)
        if old is None or _year_of(it.get("日期", "")) > _year_of(old.get("日期", "")):
            best[k] = it
    return list(best.values())


def _relevance_filter(items: list, min_score: float = 1.0) -> list:
    """剔除低相关条目；若全被剔除则返回原列表（宁少勿错 vs 宁缺勿空，取后者兜底）。"""
    kept = [it for it in items if it.get("_score", 0) >= min_score]
    return kept if kept else items


# ============================== 主入口 ==============================
def _gather(company: str, stock_code: str, metrics: list) -> dict:
    """并发抓取：公告列表 + 每指标巨潮定向检索 + 付费层通用检索。"""
    tasks = {}
    if stock_code:
        tasks["em_list"] = lambda: em_announcements(stock_code, limit=60)
    for m in metrics[:5]:
        qs = metric_spec(m).get("query") or []
        if qs:
            tasks["cn_" + m] = (lambda q=qs[0]: cninfo_search("%s %s" % (company[:6], q), limit=8))
    if PAID_CONFIGURED:
        tasks["paid_profile"] = lambda: paid_search("%s 主营业务 碳管理 ESG" % company, 5)
        for m in metrics[:2]:
            qs = metric_spec(m).get("query") or []
            if qs:
                tasks["paid_" + m] = (lambda q=qs[0], mm=m: [
                    dict(it, _指标=mm) for it in paid_search("%s %s" % (company, q), 4)])

    results = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fn): k for k, fn in tasks.items()}
        for f in as_completed(futs):
            k = futs[f]
            try:
                results[k] = f.result() or []
            except Exception:                        # noqa: BLE001
                results[k] = []
    return results


def build_web_facts(esg_json: dict, **kw) -> dict:
    """主入口：增强 JSON → 企业公开事实块（联网检索）。

    Args:
        esg_json: 增强 JSON（含 报告元数据 / 评分增强数据.障碍度Top10）
    Returns:
        {"启用", "事实", "文本块", "统计"}；任何异常返回启用=False，绝不影响主链路。
    """
    try:
        if not WEB_ENABLED:
            return {"启用": False, "原因": "WEB_SEARCH 已关闭", "事实": [], "文本块": "", "统计": {}}

        esg_json = _unwrap(esg_json)
        meta = (esg_json or {}).get("报告元数据", {}) or {}
        scored = (esg_json or {}).get("评分增强数据", {}) or {}
        company = str(meta.get("企业名称") or "").strip()
        if not company:
            return {"启用": False, "原因": "缺企业名称，无法联网检索",
                    "事实": [], "文本块": "", "统计": {}}
        metrics = [str(o.get("指标", "")).strip()
                   for o in (scored.get("障碍度Top10") or [])
                   if isinstance(o, dict) and o.get("指标")]

        stock_code = str(kw.get("stock_code") or "").strip() or resolve_stock_code(company)
        results = _gather(company, stock_code, metrics)
        anns = results.get("em_list") or []

        # ---- 1) 东财近期公告：按指标主题词筛（带正文，时效最好） ----
        picked = []
        for m in metrics:
            match = metric_spec(m).get("match") or []
            for a in anns:
                if any(k and k in (a.get("标题", "") + a.get("分类", "")) for k in match):
                    picked.append(dict(a, _指标=m))
        picked = _dedup_keep_newest(picked)
        picked.sort(key=lambda x: _year_of(x.get("日期", "")), reverse=True)
        picked = picked[:MAX_DETAIL + 3]

        # ---- 2) 抓取命中公告正文（限流，正文是信息密度最高的部分） ----
        targets = picked[:MAX_DETAIL]
        if targets:
            with ThreadPoolExecutor(max_workers=min(4, len(targets))) as ex:
                futs = {ex.submit(em_announcement_body, p.get("art_code", "")): p
                        for p in targets if p.get("art_code")}
                for f in as_completed(futs):
                    p = futs[f]
                    try:
                        p["正文"] = f.result()
                    except Exception:            # noqa: BLE001
                        p["正文"] = ""
        for p in picked:
            p.setdefault("正文", "")

        # ---- 3) 巨潮指标定向检索 ----
        cn_items = []
        for m in metrics[:5]:
            for it in (results.get("cn_" + m) or []):
                cn_items.append(dict(it, _指标=m))
        cn_items = _dedup_keep_newest(cn_items)

        # ---- 4) 付费层 ----
        paid_items = list(results.get("paid_profile") or [])
        for m in metrics[:2]:
            paid_items.extend(results.get("paid_" + m) or [])

        # ---- 5) 统一打分 + 排序 + 截断 ----
        pool = []
        for it in picked:
            it["_group"] = "公告"
        for it in cn_items:
            it["_group"] = "披露文件"
        for it in paid_items:
            it["_group"] = "外部报道"
        seen = set()
        for it in picked + cn_items + paid_items:
            key = _norm_title(it.get("标题", ""))[:22]
            if key in seen:
                continue
            seen.add(key)
            it["_score"] = round(_score(it, metric_spec(it.get("_指标", "")).get("match") or [],
                                        it.get("_指标", "")), 1)
            pool.append(it)

        pool = _relevance_filter(pool, min_score=1.0)
        pool.sort(key=lambda x: (-x["_score"], -_year_of(x.get("日期", ""))))
        facts = pool[:MAX_FACTS]

        if not facts:
            return {"启用": True, "原因": "联网检索未命中相关公开信息",
                    "事实": [], "文本块": "",
                    "统计": {"公司": company, "代码": stock_code or "未识别",
                            "公告总数": len(anns), "命中": 0}}

        # ---- 6) 生成给 LLM 的文本块 ----
        lines = []
        lines.append("【外部事实 · 企业公开信息（联网检索）】")
        lines.append("企业：%s%s ｜ 检索时间：%s ｜ 数据源：巨潮资讯网（证监会指定披露平台）、东方财富" % (
            company, ("（股票代码 %s）" % stock_code) if stock_code else _now_tail(), _now()))
        lines.append("用途：核对「该企业**自己**公开做过什么、说过什么」。"
                     "与下方同行对标库（别的公司怎么做）互补：一个给纵向事实，一个给横向基准。")
        lines.append("约束（必须遵守）：")
        lines.append("  1. 下列条目的标题/正文/日期均为**真实抓取**的公开信息；"
                     "引用时必须写明出处（文件名 + 日期），不得改写其中数字。")
        lines.append("  2. **未出现在此处的信息 = 本次未检索到**，禁止据此推断"
                     "「该企业未开展相关工作」；也不得凭空补充任何外部事实。")
        lines.append("  3. 若某条只有标题没有正文，只能引用**标题本身陈述的事实**"
                     "（例：已发布《XX办法》），不得推测标题之外的内容。")
        lines.append("  4. 检索到的多为**公告与制度文件**，它们能证明「企业已建立某项制度/已开展某动作」，"
                     "但通常不含碳数据本身；不要把它们当作指标数值的依据。")
        lines.append("")

        by_source = {}
        for i, f in enumerate(facts, 1):
            src = f.get("来源", "公开信息")
            by_source[src] = by_source.get(src, 0) + 1
            head = "[%d]%s %s" % (i, ("（%s）" % f["_指标"]) if f.get("_指标") else "",
                                  f.get("标题") or "（无标题）")
            meta_bits = [x for x in (f.get("日期"), src, f.get("分类")) if x]
            if meta_bits:
                head += " ｜ " + " ｜ ".join(meta_bits)
            lines.append(head)
            if f.get("正文"):
                lines.append("    正文摘录：" + _cut(f["正文"], MAX_BODY_CHARS))
            link = f.get("pdf") or f.get("URL") or ""
            if link:
                lines.append("    原文链接：" + link)

        text = "\n".join(lines)
        return {
            "启用": True,
            "公司": company,
            "代码": stock_code,
            "事实": facts,
            "文本块": text,
            "统计": {
                "公司": company,
                "代码": stock_code or "未识别",
                "公告总数": len(anns),
                "命中": len(facts),
                "来源分布": by_source,
                "抓取正文": sum(1 for f in facts if f.get("正文")),
                "相关度区间": [facts[-1]["_score"], facts[0]["_score"]] if facts else [],
                "文本块字符数": len(text),
                "付费层": PAID_CONFIGURED,
            },
        }
    except Exception as e:                            # noqa: BLE001 — 检索层永不影响主链路
        return {"启用": False, "原因": "联网检索异常: %s" % e,
                "事实": [], "文本块": "", "统计": {}}


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _now_tail() -> str:
    return ""


def _unwrap(data) -> dict:
    """兼容平铺增强 JSON 与 {extracted:{…}} 包裹体（与 peer_retrieval 保持一致）。"""
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
    print("provider_status =", json.dumps(provider_status(), ensure_ascii=False))
