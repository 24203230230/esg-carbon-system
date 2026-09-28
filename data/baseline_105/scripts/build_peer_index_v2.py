# -*- coding: utf-8 -*-
"""B-5 重建 peer_index.json（离线同行对标索引）

与 tools/build_peer_index.py 的区别：
  - 数据源：raw_json/（v119b 新提取 92 家），不再是桌面旧 JSON + batch_results
  - 行业标签：直接读 manifest.json（xlsx 权威表 + 人工补丁），不再靠公司名猜
  - 算法与原脚本保持一致（只收"已披露"样例 / 中文优先 / 定量优先 / 原文更完整优先）

产物：agent2/peer_index.json（同时写 esg_extractor 与 esg-carbon-system 两处）
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(ROOT, 'raw_json')
MANIFEST = os.path.join(ROOT, 'manifest.json')
OUTS = [
    r'[PROJECT]/agent2/peer_index.json',
    r'[REPO]/agent2/peer_index.json',
]

MAX_SAMPLES_PER_METRIC = 40
MAX_QUOTE_CHARS = 300
MIN_QUOTE_CHARS = 8

_LOG = []


def emit(msg=''):
    print(msg)
    _LOG.append(str(msg))


def clean_company(name):
    s = (name or '').strip()
    s = re.sub(r'[（(][^）)]*[A-Za-z][^）)]*[）)]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s or (name or '').strip()


def norm_company(name):
    s = (name or '').strip()
    for n in ['股份有限公司', '有限责任公司', '有限公司', '-', '－', ' ']:
        s = s.replace(n, '')
    return s.strip()


def detect_lang(text):
    t = str(text or '')
    if not t.strip():
        return 'CN'
    cn = len(re.findall(r'[\u4e00-\u9fff]', t))
    en = len(re.findall(r'[A-Za-z]', t))
    if cn == 0 and en > 0:
        return 'EN'
    if (cn + en) > 0 and cn / (cn + en) < 0.35:
        return 'EN'
    return 'CN'


def extract_l4(dims):
    if not isinstance(dims, dict):
        return
    for dim, dv in dims.items():
        if not isinstance(dv, dict):
            continue
        area = dv.get('指标', dv)
        if not isinstance(area, dict):
            continue
        for sub, sv in area.items():
            if not isinstance(sv, dict):
                continue
            for l4, leaf in sv.items():
                if not isinstance(leaf, dict):
                    continue
                if '值' not in leaf and '原文' not in leaf:
                    continue
                yield dim, sub, l4, leaf


def collect():
    man = json.load(open(MANIFEST, encoding='utf-8'))
    meta_by_no = {c['no']: c for c in man['companies']}
    companies = {}
    stat = Counter()
    fails = []

    for fp in sorted(glob.glob(os.path.join(RAW, '*.json'))):
        try:
            d = json.load(open(fp, encoding='utf-8'))
        except Exception as e:
            stat['解析失败'] += 1
            fails.append((os.path.basename(fp), str(e)[:120]))
            continue
        m = d.get('_meta') or {}
        no = m.get('manifest_no')
        c = meta_by_no.get(no) or {}
        company = clean_company(m.get('company') or c.get('company') or '')
        industry = (m.get('industry') or c.get('industry') or '未知').strip() or '未知'
        key = norm_company(company)
        if not key:
            continue
        rec = companies.setdefault(key, {
            '公司': company, '行业': industry, '源': 'raw_json',
            '文件': os.path.basename(fp),
            '报告': str((d.get('报告元数据') or {}).get('报告名称', '') or '').strip()[:80],
            '指标': {},
        })
        if rec['行业'] in ('', '未知') and industry:
            rec['行业'] = industry
        for dim, sub, l4, leaf in extract_l4(d.get('指标提取结果')):
            val = str(leaf.get('值', '') or '').strip()
            raw = str(leaf.get('原文', '') or '').strip()
            dt = '未披露' if (val in ('', '未披露', '无') or val.startswith('未披露')) else (
                '定量' if re.search(r'\d', val) else '定性')
            rec['指标'].setdefault(l4, {
                '维度': dim, '子域': sub, '值': val[:400],
                '单位': str(leaf.get('单位', '') or '').strip()[:40],
                '原文': raw[:MAX_QUOTE_CHARS],
                '数据类型': dt,
                '语言': detect_lang((raw or val) + ' ' + rec['报告']),
            })
            stat['条目'] += 1
        stat['文件'] += 1
    return companies, stat, fails


def build_index(companies):
    by_metric = defaultdict(lambda: {'样本': [], '行业分布': Counter()})
    all_l4 = Counter()
    dim_of = {}
    for key, rec in companies.items():
        industry = rec['行业']
        for l4, info in rec['指标'].items():
            all_l4[l4] += 1
            dim_of.setdefault(l4, (info['维度'], info['子域']))
            val, raw = info['值'], info['原文']
            # 只收已披露样例：判据以「值」字段为唯一真相来源
            disclosed = not (val in ('', '未披露', '无') or val.startswith('未披露'))
            if not disclosed:
                continue
            if len(raw) < MIN_QUOTE_CHARS and len(val) < MIN_QUOTE_CHARS:
                continue
            by_metric[l4]['样本'].append({
                '公司': rec['公司'], '行业': industry, '报告': rec.get('报告', ''),
                '源': rec['源'], '值': val[:400], '单位': info['单位'],
                '原文': raw, '数据类型': info['数据类型'],
                '语言': info.get('语言', 'CN'),
            })
            by_metric[l4]['行业分布'][industry] += 1

    out = {}
    for l4, blob in by_metric.items():
        samples = blob['样本']
        samples.sort(key=lambda s: (
            0 if s.get('语言') == 'CN' else 1,
            0 if s['数据类型'] in ('定量', '定量/定性') else 1,
            -len(s['原文']),
        ))
        kept = samples[:MAX_SAMPLES_PER_METRIC]
        dim, sub = dim_of.get(l4, ('', ''))
        out[l4] = {
            '维度': dim, '子域': sub,
            '全市场已披露': len(samples),
            '行业分布': dict(blob['行业分布'].most_common()),
            '样本': kept,
        }
    for l4 in all_l4:      # 零披露指标补键
        if l4 not in out:
            dim, sub = dim_of.get(l4, ('', ''))
            out[l4] = {'维度': dim, '子域': sub, '全市场已披露': 0, '行业分布': {}, '样本': []}
    meta = {
        'built_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        '数据源': 'baseline_105/raw_json（v119b 重提取）',
        '参数': {'每指标上限': MAX_SAMPLES_PER_METRIC, '原文截断': MAX_QUOTE_CHARS},
        '覆盖': {
            '公司数': len(companies),
            '指标数': len(out),
            '行业分布': dict(Counter(r['行业'] for r in companies.values()).most_common()),
        },
    }
    return {'meta': meta, 'by_metric': out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='')
    ap.add_argument('--dry', action='store_true')
    args = ap.parse_args()

    companies, stat, fails = collect()
    emit('=' * 70)
    emit('【采集统计】')
    emit('  文件 %d | L4 条目 %d | 解析失败 %d' % (stat['文件'], stat['条目'], stat['解析失败']))
    emit('  公司数 = %d' % len(companies))
    ind = Counter(r['行业'] for r in companies.values())
    emit('【行业分布】')
    for k, v in ind.most_common():
        emit('  %-10s %d' % (k, v))
    for fn, err in fails:
        emit('  ✗ %s -> %s' % (fn, err))

    index = build_index(companies)
    bm = index['by_metric']
    emit('【索引】指标数 = %d' % len(bm))
    emit('  已披露家数 Top10:')
    for l4, b in sorted(bm.items(), key=lambda kv: -kv[1]['全市场已披露'])[:10]:
        emit('    %-24s %d' % (l4, b['全市场已披露']))
    emit('  已披露家数 Bottom10（稀缺=对标价值最高）:')
    for l4, b in sorted(bm.items(), key=lambda kv: kv[1]['全市场已披露'])[:10]:
        emit('    %-24s %d' % (l4, b['全市场已披露']))

    if args.dry:
        emit('[DRY] 未落盘')
        return
    txt = json.dumps(index, ensure_ascii=False, separators=(',', ':'))
    outs = [args.out] if args.out else OUTS
    for o in outs:
        os.makedirs(os.path.dirname(o), exist_ok=True)
        with open(o, 'w', encoding='utf-8') as f:
            f.write(txt)
        emit('[OK] 写入 %s' % o)
    emit('     体积 %.1f KB' % (len(txt.encode('utf-8')) / 1024))
    emit('PEER_INDEX_V2_DONE')


if __name__ == '__main__':
    main()
