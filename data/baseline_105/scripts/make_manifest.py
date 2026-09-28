# -*- coding: utf-8 -*-
"""B-6.1 生成 manifest.json：105 个旧 JSON 文件 → 公司 / 行业 / 原始 PDF 全路径

匹配三路取并集（从严到宽）：
  A 路：xlsx「企业名称」+ JSON 名 → 按 PDF 文件名全称 core 匹配
  B 路：sample_pool「公司」简称 → 按 PDF 文件名简称匹配
  C 路：单字交集占比 > 0.7 兜底
行业标签优先取 `行业分类清单.xlsx`（权威），缺失回退 pool。
"""
import sys, os, re, json, glob
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from collections import OrderedDict, Counter

JD = r'[DATA_ROOT]/ESG-JSON-格式提取结果'
ROOT = r'[DATA_ROOT]/ESG报告'
POOL = r'[WORKSPACE]/_perf/sample_pool.json'
XL = r'[WORKSPACE]/行业分类清单.xlsx'
BASE = r'[REPO]/data/baseline_105'

SUF = ['股份有限公司', '有限责任公司', '有限公司', '集团股份公司', '集团', '公司', '股份']
MOD = ['新材料', '科技', '实业', '发展', '控股', '投资', '能源', '环保', '智能', '精密',
       '电子', '生物', '光电', '装备', '制造', '技术', '国际', '联合']

# 人工确认映射（PDF 文件名只有简称、无全称，自动匹配无法覆盖）
MANUAL = {
    '华电辽宁能源发展股份有限公司': '华电辽能：2025年度环境、社会和公司治理（ESG）报告.pdf',
    '同兴环保科技股份有限公司': '同兴科技：2025年度环境、社会和公司治理（ESG）报告.pdf',
    '广东天禾农资股份有限公司': '天禾股份：2025年度环境、社会和公司治理（ESG）报告.pdf',
    '广州普邦园林股份有限公司': '普邦股份：2025年度环境、社会和公司治理（ESG）报告.pdf',
    '德州联合石油科技股份有限公司': '德石股份：2025年度环境、社会和公司治理（ESG）报告（英文版）.pdf',
    '深圳市纺织（集团）股份有限公司': '深纺织Ａ：2025年环境、社会和公司治理（ESG）报告.pdf',
    '深圳沃特新材料股份有限公司（Shenzhen WOTE Advanced Materials Co., Ltd.）':
        '沃特股份：2025年环境、社会及公司治理（ESG）报告（中文）.pdf',
    '路德生物环保科技股份有限公司': '路德科技：2025年度环境、社会及治理（ESG）报告.pdf',
    # 以下用 JSON 文件名作 key（xlsx 未收录该公司）
    '振华风光-2025年_ESG报告_JSON (2).json':
        '振华风光：贵州振华风光半导体股份有限公司2025年度环境、社会及公司治理（ESG）报告.pdf',
}
# 已知真缺原始报告
MANUAL_MISS = {'纳思达股份有限公司'}
# 行业补丁（xlsx 未收录的公司）
INDUSTRY_PATCH = {'振华风光': '电子科技'}


def sq(s):
    """去括号（支持嵌套）+ 去非法字符"""
    s = str(s or '')
    for _ in range(5):
        t = re.sub(r'[（(][^（(）)]*[）)]', '', s)
        if t == s:
            break
        s = t
    return re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]', '', s)


def core(s, deep=True):
    """中英混排公司名 → 核心中文词（含括号内的中文）"""
    raw = str(s or '')
    zh = ''.join(re.findall(r'[\u4e00-\u9fff]+', raw))
    s = zh if zh else re.sub(r'[^0-9A-Za-z]', '', raw)
    for x in SUF:
        s = s.replace(x, '')
    if deep:
        for x in MOD:
            s = s.replace(x, '')
    return s


def lang_of(fn):
    """从文件名判断语言（目录名不可靠）"""
    if re.search(r'英文|English|_EN', fn, re.I):
        return 'EN'
    return 'CN'


# ---------- 1. 读 xlsx 权威行业表 ----------
xl_rows = []          # 每行 {name, file, industry}
try:
    import openpyxl
    wb = openpyxl.load_workbook(XL, data_only=True)
    ws = wb['行业分类清单']
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r and r[1]:
            xl_rows.append({'name': str(r[1]).strip(),
                            'file': str(r[2]).strip() if r[2] else '',
                            'industry': str(r[3]).strip() if r[3] else ''})
except Exception as e:
    print('XLSX_ERR', type(e).__name__, e)

xl_by_file = {r['file']: r for r in xl_rows if r['file']}
xl_by_core = {}
for r in xl_rows:
    xl_by_core.setdefault(core(r['name']), []).append(r)

# ---------- 2. 读 pool ----------
pool = json.load(open(POOL, encoding='utf-8'))
pool_by_core = {}
for it in pool:
    pool_by_core.setdefault(core(it.get('公司')), []).append(it)


def pool_pick(lst):
    """同公司多副本：优先 非重复副本 + 中文"""
    def k(x):
        return (bool(x.get('重复副本')), 0 if (x.get('语言') == 'CN') else 1,
                -(int(x.get('文本字符') or 0) if str(x.get('文本字符', '')).lstrip('-').isdigit() else 0))
    return sorted(lst, key=k)[0]


# ---------- 3. 枚举 PDF ----------
pdfs = []
for sub in ('中文版ESG报告', '英文版ESG报告', '补充的ESG报告'):
    for p in glob.glob(os.path.join(ROOT, sub, '*.pdf')):
        fn = os.path.basename(p)
        base = os.path.splitext(fn)[0]
        base = re.sub(r'[（(]\d+[）)]$', '', base)
        base = re.sub(r'(英文版|中文版)', '', base)
        parts = re.split(r'[：:]', base)
        short = parts[0]
        full = parts[1] if len(parts) > 1 else parts[0]
        full = re.split(r'20\d\d', full)[0]
        pdfs.append({'sub': sub, 'file': fn, 'path': p,
                     'short': sq(short), 'full': core(full), 'full6': core(full)[:6],
                     'lang': lang_of(fn)})

pdf_by_file = {}
for p in pdfs:
    pdf_by_file.setdefault(p['file'], p)


def stem_key(json_name):
    """'XX股份有限公司-2025年_ESG报告_JSON (2).json' → 'XX股份有限公司'"""
    stem = os.path.splitext(json_name)[0]
    stem = re.sub(r'[（(]\d+[）)]$', '', stem)
    raw = re.split(r'-?20\d\d年', stem)[0]
    return re.sub(r'-?年份', '', raw).replace('_ESG报告', '').rstrip('.-_')


def find_pdfs(json_name, xl_name=''):
    """返回候选列表 [(pdf, method), ...]；不截断，交给 pick_pdf 排序（中文优先）"""
    raw = stem_key(json_name)
    # 0) 人工白名单 —— 最高优先
    for mk in (xl_name, json_name, raw):
        if mk and mk in MANUAL:
            pf = pdf_by_file.get(MANUAL[mk])
            if pf:
                return [(pf, 'manual')]
    cands = [raw] + ([xl_name] if xl_name else [])
    cores = [c for c in (core(x) for x in cands) if c]
    shorts = [sq(x) for x in cands]
    hits = []

    for c in cores:                                   # A1 全称 core 相等（收集全部）
        for p in pdfs:
            if p['full'] and c == p['full']:
                hits.append((p, 'full-eq'))
    if not hits:
        for c in cores:                               # A2 核心词互含
            for p in pdfs:
                if len(c) >= 4 and p['full'] and (c in p['full'] or p['full'] in c):
                    hits.append((p, 'full-contain'))
    if not hits:
        for c in cores:                               # A3 前 6 字前缀
            for p in pdfs:
                if len(c) >= 6 and p['full6'] and (c[:6] in p['full'] or p['full6'] in c):
                    hits.append((p, 'full-prefix6'))
    if not hits:
        for k in shorts:                              # B 路 pool 简称
            for p in pdfs:
                if len(k) >= 3 and p['short'] and (k in p['short'] or p['short'] in k):
                    hits.append((p, 'pool-short'))
    if not hits:
        for c in cores:                               # B2 路 pool 公司名
            for k, lst in pool_by_core.items():
                if len(k) >= 3 and (k in c or c in k):
                    it = pool_pick(lst)
                    if it.get('pdf') and os.path.exists(it['pdf']):
                        hits.append(({'sub': os.path.basename(os.path.dirname(it['pdf'])),
                                      'file': it.get('file'), 'path': it['pdf'],
                                      'short': sq(it.get('公司')), 'full': k, 'full6': k[:6],
                                      'lang': it.get('语言') or lang_of(it.get('file') or '')},
                                     'pool-core'))
                    break
    if not hits:                                      # C 路 单字交集
        scored = []
        for c in cores:
            for p in pdfs:
                if not c or not p['full']:
                    continue
                a, b = set(c), set(p['full'])
                r = len(a & b) / max(1, min(len(a), len(b)))
                if r > 0.7:
                    scored.append((r, p))
        if scored:
            top = max(r for r, _ in scored)
            hits = [(p, 'ratio%.2f' % r) for r, p in scored if r >= top - 1e-9]
    return hits


_METHOD_PRI = {'manual': 0, 'full-eq': 1, 'full-contain': 2, 'full-prefix6': 3,
               'pool-short': 4, 'pool-core': 5}


def pick_pdf(hits):
    """方法精度优先 → 中文优先 → 补充/中文目录优先"""
    if not hits:
        return None, None
    def k(h):
        p, m = h
        return (_METHOD_PRI.get(m, 6),
                0 if p.get('lang') == 'CN' else 1,
                0 if p['sub'].startswith('补充') else
                1 if p['sub'].startswith('中文') else 2)
    return sorted(hits, key=k)[0]


# ---------- 4. 逐文件匹配 ----------
js = sorted(os.listdir(JD))
rows = []
for j in js:
    xr = xl_by_file.get(j)
    if not xr:      # xlsx 没列这个文件名 → 用 core 反查
        rk = stem_key(j)
        for c, lst in xl_by_core.items():
            if c and rk and (c in core(rk) or core(rk) in c):
                xr = lst[0]
                break
    p, how = pick_pdf(find_pdfs(j, xr['name'] if xr else ''))
    rows.append({
        'json_file': j,
        'company': (xr['name'] if xr else (core(stem_key(j)) or j)),
        'industry': (xr['industry'] if xr else ''),
        'industry_src': 'xlsx' if xr else '',
        'pdf': (p['path'] if p else None),
        'pdf_dir': (p['sub'] if p else ''),
        'pdf_file': (p['file'] if p else ''),
        'lang': (p.get('lang', '') if p else ''),
        'pdf_exists': bool(p and os.path.exists(p['path'])),
        'match': how,
    })

# 行业兜底：xlsx 缺失/无效 → pool（跳过"未匹配"）→ 补丁
BAD_IND = {'', '未匹配', '未知', '待定', '?', '-', '无'}
for r in rows:
    if r['industry'].strip() in BAD_IND:
        r['industry'] = ''
        r['industry_src'] = ''
    if not r['industry']:
        for k, lst in pool_by_core.items():
            ind = (lst[0].get('行业') or '').strip()
            if ind in BAD_IND:
                continue
            if k and (k in core(r['company']) or core(r['company']) in k):
                r['industry'] = ind
                r['industry_src'] = 'pool'
                break
    if not r['industry']:
        for k, v in INDUSTRY_PATCH.items():
            if k in r['company'] or k in core(r['company']):
                r['industry'] = v
                r['industry_src'] = 'patch'
                break

# ---------- 5. 去重组 ----------
groups = OrderedDict()
for r in rows:
    key = os.path.normcase(r['pdf_file']) if r['pdf'] else ('NOPDF:' + core(r['company']))
    groups.setdefault(key, []).append(r)
for gi, (key, lst) in enumerate(groups.items(), 1):
    # primary = pdf 存在 → 中文优先 → 补充/中文目录优先 → 文件名长（信息全）
    def pk(x):
        return (not x['pdf_exists'],
                0 if x['lang'] == 'CN' else 1,
                0 if x['pdf_dir'].startswith('补充') else
                1 if x['pdf_dir'].startswith('中文') else 2,
                -len(x['json_file']))
    lst.sort(key=pk)
    for i, r in enumerate(lst):
        r['dup_group'] = gi
        r['is_primary'] = (i == 0)
        r['dup_index'] = i + 1

# ---------- 6. 落盘 ----------
out = {
    'generated': '2026-09-27',
    'conf_version': 'v119b',
    'extract_build': None,      # B-2 批量提取完成后回填（记录提取时的 /api/health build）
    'source': {'json_dir': JD, 'pdf_root': ROOT, 'industry_xlsx': XL},
    'stats': {},
    'companies': [{'no': i + 1, 'company': lst[0]['company'], 'industry': lst[0]['industry'],
                   'industry_src': lst[0]['industry_src'], 'pdf': lst[0]['pdf'],
                   'pdf_dir': lst[0]['pdf_dir'], 'pdf_file': lst[0]['pdf_file'],
                   'lang': lst[0]['lang'],
                   'pdf_exists': lst[0]['pdf_exists'], 'match': lst[0]['match'],
                   'json_files': [x['json_file'] for x in lst],
                   'dup_count': len(lst)}
                  for i, lst in enumerate(groups.values())],
    'files': rows,
}
gs = out['companies']
st = out['stats']
st['json_files_total'] = len(rows)
st['company_unique'] = len(gs)
st['pdf_located'] = sum(1 for g in gs if g['pdf_exists'])
st['pdf_missing'] = sum(1 for g in gs if not g['pdf_exists'])
st['dup_groups'] = sum(1 for g in gs if g['dup_count'] > 1)
st['match_methods'] = dict(Counter(g['match'] for g in gs))
st['industry_src'] = dict(Counter(g['industry_src'] for g in gs))
st['industry_dist'] = dict(Counter(g['industry'] or '未匹配' for g in gs))
st['pdf_dir_dist'] = dict(Counter(g['pdf_dir'] for g in gs))
st['lang_dist'] = dict(Counter(g['lang'] or '?' for g in gs))

os.makedirs(BASE, exist_ok=True)
json.dump(out, open(os.path.join(BASE, 'manifest.json'), 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1)

# ---------- 7. 人类可读复核清单 ----------
buf = []
def P(*a): buf.append(' '.join(str(x) for x in a))
P('=' * 96)
P('manifest 复核清单   JSON 文件 %d → 公司 %d   已定位 PDF %d   缺口 %d'
  % (st['json_files_total'], st['company_unique'], st['pdf_located'], st['pdf_missing']))
P('=' * 96)
P('匹配方式 =', st['match_methods'])
P('行业来源 =', st['industry_src'])
P('PDF 目录 =', st['pdf_dir_dist'])
P('语言 =', st['lang_dist'])
P('')
P('行业分布（唯一公司计）:')
for k, n in sorted(st['industry_dist'].items(), key=lambda x: -x[1]):
    P('   %-10s %d' % (k, n))
P('')
P('-' * 100)
P('%-3s %-30s %-8s %-4s %-12s %-3s %s' % ('#', '公司', '行业', '语', '匹配', '份', 'PDF'))
P('-' * 100)
for g in gs:
    flag = '' if g['pdf_exists'] else '  ❌缺'
    P('%-3d %-30s %-8s %-4s %-12s %-3d %s%s' % (
        g['no'], g['company'][:28], g['industry'] or '?', g['lang'] or '?',
        g['match'] or '?', g['dup_count'],
        (g['pdf_file'] or '(未定位)')[:56], flag))
    if g['dup_count'] > 1:
        for jf in g['json_files']:
            P('      └ %s' % jf)
P('')
P('=== 缺口明细（未定位 PDF）===')
for g in gs:
    if not g['pdf_exists']:
        P('  #%d %s   json=%s' % (g['no'], g['company'], g['json_files']))
open(os.path.join(BASE, 'scripts', '_manifest_check.txt'), 'w', encoding='utf-8').write('\n'.join(buf))
print('MADE', os.path.join(BASE, 'manifest.json'))
print('CHECK', len(buf), 'lines')
print('公司 %d | 定位 %d | 缺 %d | 重复组 %d'
      % (st['company_unique'], st['pdf_located'], st['pdf_missing'], st['dup_groups']))
