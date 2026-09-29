# -*- coding: utf-8 -*-
"""证据强制校验（v=118，方案 A）
================================================================
目的：**任何拿到分的指标，其「原文」必须能在报告全文中定位到。**

为什么需要：
  v117 之后，算分层的占位符漏洞已关闭；但提取层仍可能给出
  「看起来合理、报告里却没有」的原文（幻觉），而只要原文非空就会被算分层
  当作披露计分 —— 这是评分虚高的最危险来源。
  本模块把「可溯源」变成硬约束，同时也让每一条得分可审计。

设计约束（严格遵守"不碰原逻辑"）：
  1. **只降级、不新增/不改写**：不通过的节点把 值/原文/年份 清空 ⇒ 视为未披露；
  2. **不改动调用方任何既有逻辑**：不通过则原样返回，任何异常都被吞掉；
  3. **可关停**：环境变量 EVIDENCE_VERIFY=0 立即回到 v117 行为；
  4. **可调参**：阈值均可用环境变量覆盖，便于按需放宽/收紧；
  5. **只处理"有原文"的节点**：原文为空的节点保持原行为（本研究未观察到
     该情形的误判样本，保守起见不动）。

算法（三个必要设计，均经实测验证）：
  ① **繁简归一化**：报告常为繁体而模型按提示词输出简体。
     实测同一批原文：不转命中率 0.021（几乎全灭）→ 转简后 0.800。
  ② **全文索引必须稠密（step=1）**：若生成索引时也用稀疏步长，
     原文在全文中的起始偏移与采样偏移不齐时会整段假性漏命中。
  ③ **数字做严格匹配**：不去小数点（`1234.56` 不能退化成 `123456`），
     否则虚构数字会被报告里不相邻的数字拼接骗过。
  ④ **千分位归一（v=118b 补）**：比对前两侧都去掉千分位分隔符
     （报告写 `3,480.32` ↔ 模型按提示词常写 `3480.32`）。原实现只有"严格子串匹配"
     ⇒ 查询串不含逗号、索引里含逗号 ⇒ 判为「原文无数字证据」，把**真实数据**误降级。
     实测 92 份样本中此类误拦 21 条（占真拦截的 25%），英文报告为主（英文数字普遍带千分位）。
     该归一化**不削弱严格性**：小数点仍不去、数字仍须整体命中
     （`1234` 不会因去逗号而被 `1,235` 命中）。

判定规则（v=124b）：
  文本命中率 = 原文 6-gram 在报告全文 gram 集合中的命中比例
  通路 A：文本命中 ≥ 0.4  且  （原文无「长度≥3 的数字」或 数字命中 ≥ 0.8）
     · 表格型证据（标签与数值被排版分隔）会拉低文本命中，故靠数字分支兜住；
     · 但数字分支**不能单独放行**（否则虚构数字会漏网），故为「与」关系。
  通路 B：**「值」**里的数字 ≥ 0.8 命中，且 文本命中 ≥ 0.25
         且 「值」至少含 1 个**位数 ≥3** 的实数（_has_long_number）
     · 「值」才是被打分的数据；它确实在报告里 ⇒ 该条读的是真数据；
     · 原文只是上下文（常含模型自附引用与多年度数字），不应一票否决；
     · 位数门槛挡住 `1.8`/`2.0` 这类"报告里到处都有"的短数字蒙混过关；
     · 虚构数值（模型自己编的数）依旧过不了 ⇒ 防幻觉主干未动。

  v=124b 另补两处**只提精度、不提风险**的归一化（比对前两侧同做）：
  ⑤ 模型自附引用剥离：`（第21页 环境维度-应对气候变化）` / `(p.73)` 不是报告原文，
     却把 6-gram 压到阈值以下（上海机场范围一：0.333 → 过）。删的是「非报告内容」。
  ⑥ 作用域标签同义：`範圍 1` ≡ `範圍一` ≡ `Scope 1` → `范围1`（详见 _canon_scope）。

实测（牧原 81 页繁体报告）：
  真实样本 146 项 → 通过率 100%（0 误伤）
  编造 4 例（套话拼接/虚构数字/概念嫁接/空泛自述）→ 4/4 拦住
  性能：建索引约 174ms（8.5 万字符），逐项校验约 0.14ms
"""
import logging
import os
import re

logger = logging.getLogger('evidence_verify')

try:
    import zhconv
    _HAS_ZHCONV = True
except Exception:            # 依赖缺失 → 直接跳过校验（绝不阻断主流程）
    _HAS_ZHCONV = False

try:
    from weights_logic import EMPTY_VALUES as _PLACEHOLDER_VALUES
except Exception:
    _PLACEHOLDER_VALUES = ('', '未披露', '无', '未提及', 'N/A', '不适用')


def _env_flag(name, default):
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() not in ('0', 'false', 'no', 'off', '')


# ---- 配置（均可用环境变量覆盖）----
ENABLED = _env_flag('EVIDENCE_VERIFY', True)
NGRAM_N = int(os.environ.get('EVIDENCE_NGRAM', '6') or 6)
MIN_TEXT_RATE = float(os.environ.get('EVIDENCE_MIN_TEXT', '0.4') or 0.4)
MIN_NUM_RATE = float(os.environ.get('EVIDENCE_MIN_NUM', '0.8') or 0.8)
MIN_REPORT_CHARS = int(os.environ.get('EVIDENCE_MIN_REPORT_CHARS', '200') or 200)
# v=118b：千分位归一（两侧都去分隔符后再比对）。EVIDENCE_THOUSANDS=0 可回到 v118 行为。
STRIP_THOUSANDS = _env_flag('EVIDENCE_THOUSANDS', True)
# v=124b：剥掉模型自附的页/章节引用（见 _strip_citations）。=0 回到 v118b 行为。
STRIP_CITATIONS = _env_flag('EVIDENCE_STRIP_CITATIONS', True)
# v=124b：作用域标签同义归一（范围一 ≡ 范围1 ≡ Scope 1，见 _canon_scope）。=0 关闭。
LABEL_NORM = _env_flag('EVIDENCE_LABEL_NORM', True)
# v=124b：通路 B —— 「值」的数字全命中时放宽文本阈值（设 -1 关闭该通路）。
RELAX_TEXT_RATE = float(os.environ.get('EVIDENCE_RELAX_TEXT', '0.25') or -1)

_NUM_PAT = re.compile(r'\d+(?:\.\d+)?')
_NON_WORD = re.compile(r'[^\w\u4e00-\u9fff]')
_BLANK = re.compile(r'[\s\u3000]')
# 千分位逗号：仅当「数字 , 恰好3位数字，且其后不再接数字」时才认定为千分位。
#   '3,480.32' → 去 ；'2,200,430' → 去 ；'1234,5678' → 保留（非千分位）；'1,23' → 保留
_THOUSANDS = re.compile(r'(?<=\d),(?=\d{3}(?!\d))')


def _strip_thousands(s):
    """去掉数字中的千分位逗号（**只动千分位**，不碰小数点与其它标点）。

    v=118b 修复点：报告原文 `3480.32` 与模型输出可能一个带分隔符、一个不带，
    严格子串匹配会把真实数据判成幻觉（详见文件头「算法 ④」）。
    两侧都做同样归一化即可对齐，且不改变判定阈值与逻辑结构。
    """
    return _THOUSANDS.sub('', s or '')


def _has_long_number(s):
    """「值」里是否存在**位数 ≥3 的实数**（小数点与千分位不计入位数）。

    v=124b：通路 B 的前置门槛。为什么不能省：
      `1.8`、`2.0` 这类短数字在十几万字的报告里**随处可能出现**，
      拿它当"硬证据"等于给了模型一个随便蒙中的机会。
      实测（92 份样本）：加上该门槛只损失 1 条放行（五粮液「环保投资 1.8」），
      但通路 B 从此只接受"有实质位数"的数量。
    """
    return any(len(re.sub(r'\D', '', x)) >= 3
               for x in _NUM_PAT.findall(_strip_thousands(s or '')))


# 降级时写入的占位值（与算分层的"未披露"口径一致）
DEGRADE_VALUE = '未披露'


def _to_simple(s):
    """繁 → 简（失败原样返回）"""
    if not _HAS_ZHCONV or not s:
        return s
    try:
        return zhconv.convert(s, 'zh-cn')
    except Exception:
        return s


# ---- v=124b 归一化①：模型自附引用 ----
# 实测（2026-09-29）：上海机场「范围一排放」原文 =
#   '范围一 25,166.80 tCO2e（第21页 环境维度-应对气候变化）'
# 尾部的「（第21页 …）」是模型给评委看的定位信息、**报告原文里没有**，
# 却把 6-gram 命中率从「能过」压到 0.333（阈值 0.4）⇒ 真数据被判成幻觉。
# 剥掉它只删除「非报告内容」，不放松任何判据 ⇒ 只提精度、不提风险。
_CITE_BRACKET_IN = r'[（(\[【〔]'
_CITE = re.compile(
    r'[（(\[【〔][^（()）\[\]【】〔〕]{0,80}?'
    r'(?:第\s*\d+\s*页|[Pp]{1,2}\.?\s*\d+)'
    r'[^（()）\[\]【】〔〕]{0,80}?[)）\]】〕]'
)

# ---- v=124b 归一化②：作用域标签同义 ----
# 报告排版惯用阿拉伯数字（「溫室氣體排放總量（範圍 1,2,3）」），
# 模型按指标名写中文数字（「范围一排放」）——概念完全等价，6-gram 却直接归零。
# 两侧都归一到「范围N」，英文 Scope 一并收编。
_SCOPE_MAP = {'一': '1', '二': '2', '三': '3', '四': '4', '五': '5', '六': '6'}
_SCOPE = re.compile(r'(?:范围|范畴|scope)\s*([一二三四五六123456])', re.I)


def _strip_citations(s):
    """剥掉「（第N页 …）」「(p.73)」这类模型自附引用"""
    return _CITE.sub(' ', s or '')


def _canon_scope(s):
    """范围一 / 范围 1 / Scope 1 → 范围1"""
    return _SCOPE.sub(lambda m: '范围' + _SCOPE_MAP.get(m.group(1), m.group(1)), s or '')


def _pre(s):
    """比对前的公共预处理：繁→简 + 引用剥离 + 标签归一"""
    s = _to_simple(s or '')
    if STRIP_CITATIONS:
        s = _strip_citations(s)
    if LABEL_NORM:
        s = _canon_scope(s)
    return s


def _norm_text(s):
    """文本口径归一化：繁→简 + 去一切非字词（容忍标点/空白/换行差异）"""
    return _NON_WORD.sub('', _pre(s))


def _norm_keep_num(s):
    """数字口径归一化：繁→简 + 只去空白（**保留小数点**，供严格数字匹配）"""
    return _BLANK.sub('', _pre(s))


class ReportIndex(object):
    """报告全文索引（一次构建，42 个指标共用）"""

    __slots__ = ('flat_text', 'grams', 'flat_num', 'ready')

    def __init__(self, report_text):
        self.ready = False
        self.flat_text = ''
        self.grams = set()
        self.flat_num = ''
        f = _norm_text(report_text)
        if len(f) < MIN_REPORT_CHARS:
            return
        n = NGRAM_N
        self.flat_text = f
        # ⚠️ 索引必须稠密（step=1），否则与原文采样偏移不齐会整段漏命中
        self.grams = set(f[i:i + n] for i in range(len(f) - n + 1))
        self.flat_num = _norm_keep_num(report_text)
        if STRIP_THOUSANDS:
            # 索引侧同样去千分位，与 num_rate 的查询侧对齐（v=118b）
            self.flat_num = _strip_thousands(self.flat_num)
        self.ready = True

    def text_rate(self, raw):
        r = _norm_text(raw)
        if not r:
            return 0.0
        n = NGRAM_N
        if len(r) < n:
            return 1.0 if r in self.flat_text else 0.0
        gs = [r[i:i + n] for i in range(len(r) - n + 1)]
        hit = 0
        for g in gs:
            if g in self.grams:
                hit += 1
        return hit / float(len(gs))

    def num_rate(self, text):
        """原文/值中「长度≥3 的数字」的整体命中率；无此类数字时返回 None"""
        src = text or ''
        if STRIP_THOUSANDS:
            # 先去掉千分位：否则 `2,200,430` 会被 _NUM_PAT 切成 ['2','200','430'] 而漏判
            src = _strip_thousands(src)
        nums = [x for x in _NUM_PAT.findall(src) if len(x) >= 3]
        if not nums:
            return None
        hit = sum(1 for x in nums if x in self.flat_num)
        return hit / float(len(nums))

    def check(self, raw, val=''):
        """返回 (是否通过, 文本命中率, 数字命中率或 None)

        两条通路（v=124b 新增 B）：
          A（原判据，不变） 文本命中 ≥ MIN_TEXT_RATE
                            且（原文无「长度≥3 的数字」或 数字命中 ≥ MIN_NUM_RATE）
          B（新增）        「值」里的长数字≥MIN_NUM_RATE 命中，且 文本命中 ≥ RELAX_TEXT_RATE

        为什么要 B：实测 92 份里 36 条真数据被数字分支拦下，原因是模型在**原文**里
        顺手抄了报告的多年度/派生数字（如 'Total GHG Emissions 10,268.32 11,178.42
        10,905.91'，只有最后一年是本次要的「值」），其中个别年份因表格排版在解析结果里
        对不上 ⇒ 数字命中掉到 0.5~0.75 < 0.8 ⇒ 整条被否。
        但**「值」才是真正被打分的数据**：只要它确实出现在报告里，就说明这一条读的是真数据；
        「原文」只是给评委看的上下文，不该被当成一票否决的理由。
        ⚠️ 通路 B 不放弃文本判据（仍要 ≥ RELAX_TEXT_RATE），且「值」必须本身含长数字，
           虚构数值（模型自己编的数）依旧过不了 —— 防幻觉的主干未动。
        """
        tr = self.text_rate(raw)
        nr = self.num_rate((raw or '') + ' ' + (val or ''))
        if tr >= MIN_TEXT_RATE and (nr is None or nr >= MIN_NUM_RATE):
            return True, tr, nr
        if RELAX_TEXT_RATE >= 0 and tr >= RELAX_TEXT_RATE and _has_long_number(val):
            vn = self.num_rate(val or '')
            if vn is not None and vn >= MIN_NUM_RATE:
                return True, tr, (nr if nr is not None else vn)
        return False, tr, nr


def _iter_leaves(doc):
    """遍历 4 维度 / L3 子域 / L4 叶子（与原结构完全一致，只读不建）"""
    if not isinstance(doc, dict):
        return
    dims = doc.get('指标提取结果')
    if not isinstance(dims, dict):
        return
    for dim, dv in dims.items():
        if not isinstance(dv, dict):
            continue
        subs = dv.get('指标')
        if not isinstance(subs, dict):
            continue
        for sub, sv in subs.items():
            if not isinstance(sv, dict):
                continue
            for l4, leaf in sv.items():
                if isinstance(leaf, dict):
                    yield l4, leaf


def _is_placeholder(s):
    return (s or '').strip() in _PLACEHOLDER_VALUES


def verify_document(doc, report_text):
    """对整份提取结果做证据校验，就地降级不通过的节点。

    返回 (doc, report)；report 供审计与前端展示：
      {enabled, reason, checked, failed, skipped, degraded:[{指标,值,文本命中,数字命中,原因}], 阈值}
    """
    report = {
        'enabled': bool(ENABLED),
        'reason': '',
        'checked': 0,
        'failed': 0,
        'skipped': 0,
        'degraded': [],
        '阈值': {'文本命中下限': MIN_TEXT_RATE, '数字命中下限': MIN_NUM_RATE, 'ngram': NGRAM_N,
                 '通路B文本下限': RELAX_TEXT_RATE,
                 '引用剥离': bool(STRIP_CITATIONS), '标签归一': bool(LABEL_NORM)},
        '版本': 'v124b',
    }

    if not ENABLED:
        report['reason'] = 'disabled'
        return doc, report
    if not _HAS_ZHCONV:
        report['reason'] = 'zhconv_missing'
        logger.warning('证据校验跳过：zhconv 未安装（繁体报告将无法定位证据）')
        return doc, report

    try:
        idx = ReportIndex(report_text)
    except Exception as e:                      # 索引构建异常 → 不校验
        report['reason'] = 'index_error:%s' % type(e).__name__
        logger.warning('证据校验跳过：索引构建异常 %s', e)
        return doc, report

    if not idx.ready:
        report['reason'] = 'report_too_short'
        return doc, report

    try:
        for name, leaf in _iter_leaves(doc):
            raw = str(leaf.get('原文') or '').strip()
            val = str(leaf.get('值') or '').strip()

            # 本来就无内容（占位符）→ 不参与校验，保持原行为
            if _is_placeholder(val) and _is_placeholder(raw):
                report['skipped'] += 1
                continue
            # 原文为空、仅有值 → 保持原行为（保守：不新增未经验证的降级）
            if not raw:
                report['skipped'] += 1
                continue

            report['checked'] += 1
            ok, tr, nr = idx.check(raw, val)
            if ok:
                continue

            leaf['值'] = DEGRADE_VALUE
            leaf['原文'] = ''
            leaf['年份'] = ''
            report['failed'] += 1
            report['degraded'].append({
                '指标': name,
                '原值': val[:60],
                '原文字段': raw[:120],
                '文本命中': round(tr, 3),
                '数字命中': (round(nr, 3) if nr is not None else None),
                '原因': ('原文无数字证据' if tr >= MIN_TEXT_RATE else '原文无法在报告中定位'),
            })
    except Exception as e:                      # 任何意外 → 记录但不抛出
        report['reason'] = 'loop_error:%s' % type(e).__name__
        logger.warning('证据校验过程异常（已保留已完成部分）: %s', e)

    return doc, report
