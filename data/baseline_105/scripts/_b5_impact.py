# -*- coding: utf-8 -*-
"""
B-5 落库影响实测：同一份 92 家数据，只替换「行业基准库 / 全市场中位数」，
对比总分变化。全局权重一字不动。

用法：python _b5_impact.py
"""
import json
import os
import statistics
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(ROOT, 'raw_json')
MANIFEST = os.path.join(ROOT, 'manifest.json')
PATCH = os.path.join(ROOT, 'weights_patch.json')
EXTRACTOR = r'[PROJECT]'
OLD_W = os.path.join(EXTRACTOR, 'weights.json')

sys.path.insert(0, EXTRACTOR)
import weights_logic as WL          # noqa: E402

old_w = json.load(open(OLD_W, encoding='utf-8'))
new_p = json.load(open(PATCH, encoding='utf-8'))
man = json.load(open(MANIFEST, encoding='utf-8'))
by_no = {c['no']: c for c in man['companies']}

# patch 有意**不携带**全局权重（写的是说明文字），避免误覆盖权威源；
# 落库脚本 apply_baseline.py 只取「行业基准库 / 全市场中位数」两个键。
gw_old = old_w.get('全局权重') or {}
_gn = new_p.get('全局权重')
if isinstance(_gn, str):
    print('✓ patch 声明：全局权重 %s' % _gn)
else:
    assert _gn == gw_old, '意外：patch 携带了不同的全局权重！'
print('✓ 权威源全局权重 = %d 项，和 = %.6f' % (len(gw_old), sum(gw_old.values())))


def run(tag):
    """用当前 INDUSTRY_BASE/MEDIANS 给 92 家算分"""
    out = {}
    for fn in sorted(os.listdir(RAW)):
        if not fn.endswith('.json'):
            continue
        d = json.load(open(os.path.join(RAW, fn), encoding='utf-8'))
        no = (d.get('_meta') or {}).get('manifest_no')
        ind = (d.get('_meta') or {}).get('industry') or ''
        try:
            r = WL.score_all(d, ind, 'listed')
            out[no] = r.get('总分')
        except Exception as e:
            print('  !! %s 算分异常 %s' % (fn[:30], e))
            out[no] = None
    return out


# —— 旧基准 ——
WL.INDUSTRY_BASE = old_w.get('行业基准库') or {}
WL.MEDIANS = old_w.get('全市场中位数') or {}
s_old = run('old')

# —— 新基准 ——
WL.INDUSTRY_BASE = new_p.get('行业基准库') or {}
WL.MEDIANS = new_p.get('全市场中位数') or {}
s_new = run('new')

pairs = [(no, s_old.get(no), s_new.get(no)) for no in sorted(s_old) if s_old.get(no) is not None and s_new.get(no) is not None]
deltas = [n - o for _, o, n in pairs]
print()
print('样本对比 =', len(pairs), '家')
print('总分  旧均值 = %.3f | 新均值 = %.3f | 变化 = %+.3f' % (
    statistics.mean(o for _, o, _ in pairs), statistics.mean(n for _, _, n in pairs),
    statistics.mean(deltas)))
print('变化: 上升 %d / 下降 %d / 持平 %d' % (
    sum(1 for d in deltas if d > 1e-9), sum(1 for d in deltas if d < -1e-9),
    sum(1 for d in deltas if abs(d) <= 1e-9)))
print('极值: min %+.2f | max %+.2f | 中位 %+.2f' % (min(deltas), max(deltas), statistics.median(deltas)))

print()
print('=== 变化最大的 12 家 ===')
big = sorted(pairs, key=lambda x: -(x[2] - x[1]))[:12]
print('%-4s %-28s %7s %7s %7s' % ('#', '公司', '旧', '新', '差'))
for no, o, n in big:
    nm = (by_no.get(no) or {}).get('company', '?')[:26]
    print('%-4s %-28s %7.2f %7.2f %+7.2f' % (no, nm, o, n, n - o))

print()
print('=== 跨档检查（5.0/3.5 为档线附近）===')
def lv(s):
    if s >= 7: return '优秀'
    if s >= 5: return '良好'
    if s >= 3.5: return '基础'
    return '初始'
cross = [(no, o, n) for no, o, n in pairs if lv(o) != lv(n)]
print('跨档家数 =', len(cross))
for no, o, n in cross[:15]:
    nm = (by_no.get(no) or {}).get('company', '?')[:26]
    print('  #%-4s %-28s %.2f(%s) → %.2f(%s)' % (no, nm, o, lv(o), n, lv(n)))
print()
print('B5_IMPACT_DONE')
