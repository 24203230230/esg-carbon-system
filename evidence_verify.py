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

判定规则：
  文本命中率 = 原文 6-gram 在报告全文 gram 集合中的命中比例
  通过 ⟺ 文本命中 ≥ 0.4  且  （原文无「长度≥3 的数字」或 数字命中 ≥ 0.8）
     · 表格型证据（标签与数值被排版分隔）会拉低文本命中，故靠数字分支兜住；
     · 但数字分支**不能单独放行**（否则虚构数字会漏网），故为「与」关系。

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

_NUM_PAT = re.compile(r'\d+(?:\.\d+)?')
_NON_WORD = re.compile(r'[^\w\u4e00-\u9fff]')
_BLANK = re.compile(r'[\s\u3000]')

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


def _norm_text(s):
    """文本口径归一化：繁→简 + 去一切非字词（容忍标点/空白/换行差异）"""
    return _NON_WORD.sub('', _to_simple(s or ''))


def _norm_keep_num(s):
    """数字口径归一化：繁→简 + 只去空白（**保留小数点**，供严格数字匹配）"""
    return _BLANK.sub('', _to_simple(s or ''))


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
        nums = [x for x in _NUM_PAT.findall(text or '') if len(x) >= 3]
        if not nums:
            return None
        hit = sum(1 for x in nums if x in self.flat_num)
        return hit / float(len(nums))

    def check(self, raw, val=''):
        """返回 (是否通过, 文本命中率, 数字命中率或 None)"""
        tr = self.text_rate(raw)
        if tr < MIN_TEXT_RATE:
            return False, tr, None
        nr = self.num_rate((raw or '') + ' ' + (val or ''))
        if nr is not None and nr < MIN_NUM_RATE:
            return False, tr, nr
        return True, tr, nr


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
        '阈值': {'文本命中下限': MIN_TEXT_RATE, '数字命中下限': MIN_NUM_RATE, 'ngram': NGRAM_N},
        '版本': 'v118',
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
