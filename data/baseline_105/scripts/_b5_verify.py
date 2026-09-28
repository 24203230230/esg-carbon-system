# -*- coding: utf-8 -*-
import json, os, sys, statistics
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
RAW = os.path.join(ROOT, 'raw_json')
EX = r'[PROJECT]'
sys.path.insert(0, EX)
import weights_logic as WL
print('已加载 weights.json 版本 =', WL.WEIGHTS.get('版本'), '| 行业基准库键数 =', len(WL.INDUSTRY_BASE))
tot=[]; lv={}
for fn in sorted(os.listdir(RAW)):
    if not fn.endswith('.json'): continue
    d = json.load(open(os.path.join(RAW, fn), encoding='utf-8'))
    ind = (d.get('_meta') or {}).get('industry') or ''
    r = WL.score_all(d, ind, 'listed')
    tot.append(r['总分']); lv[r.get('评分等级')] = lv.get(r.get('评分等级'),0)+1
print('92 家总分: 均值=%.3f 中位=%.3f min=%.2f max=%.2f' % (statistics.mean(tot), statistics.median(tot), min(tot), max(tot)))
print('等级分布 =', lv)
