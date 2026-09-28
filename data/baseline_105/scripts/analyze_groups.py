# -*- coding: utf-8 -*-
"""归组分析：105 个旧 JSON 文件 / sample_pool 133 条 去重后各有多少「公司」"""
import sys, os, re, json, glob
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from collections import OrderedDict, Counter

JD = r'[DATA_ROOT]/ESG-JSON-格式提取结果'
POOL = r'[WORKSPACE]/_perf/sample_pool.json'
OUT = r'[REPO]/data/baseline_105/scripts/_analyze_groups.txt'
buf = []
def P(*a):
    s = ' '.join(str(x) for x in a)
    buf.append(s)

SUF = ['股份有限公司', '有限责任公司', '有限公司', '集团股份公司', '集团', '公司', '股份']
MOD = ['新材料', '科技', '实业', '发展', '控股', '投资', '能源', '环保', '智能', '精密',
       '电子', '生物', '光电', '装备', '制造', '技术', '国际', '国内', '联合']


def sq(s):
    s = re.sub(r'[（(][^）)]*[）)]', '', str(s or ''))
    return re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]', '', s)


def core(s, deep=True):
    s = sq(s)
    for x in SUF:
        s = s.replace(x, '')
    if deep:
        for x in MOD:
            s = s.replace(x, '')
    return s


# 手工别名：把「同一家公司的不同写法」归到同一 key
ALIAS = {
    '创新': '创新新材', '创新新材': '创新新材',
    '亿华通': '亿华通', '北京亿华通': '亿华通',
    '德州联合石油': '德州联合石油', '德石股份联合石油': '德州联合石油',
    '力芯微': '力芯微', '无锡力芯微': '力芯微',
    '中力': '中力股份', '浙江中力': '中力股份',
    '拓邦': '拓邦股份', '深圳拓邦': '拓邦股份',
    '上海商米': '上海商米', '上海晨光文具': '上海晨光文具',
    '孚能': '孚能科技', '帝王实业': '帝王实业控股',
    '江苏宏微': '江苏宏微', '志邦家居': '志邦家居',
    '振华风光': '振华风光',
}


def key_of(name):
    c = core(name)
    return ALIAS.get(c, c)


js = sorted(os.listdir(JD))
P('=' * 78)
P('一、旧 JSON 目录：%d 个文件' % len(js))
P('=' * 78)
g1 = OrderedDict()
for j in js:
    stem = os.path.splitext(j)[0]
    stem = re.sub(r'[（(]\d+[）)]$', '', stem)
    raw = re.split(r'-?20\d\d年', stem)[0]
    raw = re.sub(r'-?年份', '', raw)
    raw = re.sub(r'_ESG报告.*$', '', raw).rstrip('.-_')
    k = key_of(raw)
    g1.setdefault(k, []).append({'file': j, 'raw': raw})

dup1 = {k: v for k, v in g1.items() if len(v) > 1}
P('去重后公司数 = %d   ｜  含重复副本的公司 = %d 组' % (len(g1), len(dup1)))
P('')
P('--- 重复组明细 ---')
for k, v in dup1.items():
    P('  [%s]  %d 份' % (k, len(v)))
    for it in v:
        sz = os.path.getsize(os.path.join(JD, it['file']))
        P('        %8d B  %s' % (sz, it['file']))
P('')
P('--- 疑似异常命名（占位符/双点/无 _JSON 后缀）---')
for j in js:
    if '年份' in j or '..' in j or not j.endswith('_JSON.json'):
        P('     ', j)

pool = json.load(open(POOL, encoding='utf-8'))
P('')
P('=' * 78)
P('二、sample_pool.json：%d 条' % len(pool))
P('=' * 78)
g2 = OrderedDict()
for it in pool:
    g2.setdefault(key_of(it.get('公司')), []).append(it)
dup2 = {k: v for k, v in g2.items() if len(v) > 1}
P('去重后公司数 = %d   ｜  含多副本的公司 = %d 组' % (len(g2), len(dup2)))
P('')
P('--- 多副本明细（含语言/重复副本标记）---')
for k, v in dup2.items():
    P('  [%s]  %d 份' % (k, len(v)))
    for it in v:
        P('        %-4s 重复副本=%-5s 字符=%-8s %s' % (
            it.get('语言'), it.get('重复副本'), it.get('文本字符'), (it.get('file') or '')[:58]))
P('')
P('--- pool 行业分布（去重后按公司计）---')
ind = Counter()
for k, v in g2.items():
    ind[v[0].get('行业') or '未匹配'] += 1
for k, n in ind.most_common():
    P('  %-10s %d' % (k, n))
P('')
P('--- pool 判定非 OK（去重后）---')
for k, v in g2.items():
    it = v[0]
    if (it.get('判定') or 'OK') != 'OK':
        P('  %-12s 判定=%-8s 字符=%-8s %s' % (k, it.get('判定'), it.get('文本字符'), (it.get('file') or '')[:50]))
P('')
P('--- 旧 JSON 公司池 vs pool 公司池 ---')
only_json = sorted(set(g1) - set(g2))
only_pool = sorted(set(g2) - set(g1))
P('仅存在于旧 JSON（%d）：%s' % (len(only_json), ' / '.join(only_json)))
P('仅存在于 pool（%d）：%s' % (len(only_pool), ' / '.join(only_pool)))

open(OUT, 'w', encoding='utf-8').write('\n'.join(buf))
print('ANALYZE_DONE ->', OUT, len(buf), 'lines')
