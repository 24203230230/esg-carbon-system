# -*- coding: utf-8 -*-
"""量化 v118 证据强制校验的拦截效果（幻觉程度画像）。

只读 raw_json/，不修改任何数据。
输出：_hallucination_report.json + stdout 报告
"""
import sys, os, json, glob, collections

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, '..', 'raw_json')
OUT = os.path.join(HERE, '_hallucination_report.json')


def band(t):
    if t is None:
        return 'n/a'
    if t >= 0.8:
        return 'A 高 >=0.8'
    if t >= 0.6:
        return 'B 中 0.6-0.8'
    if t >= 0.4:
        return 'C 低 0.4-0.6'
    return 'D 极低 <0.4'


def nband(n):
    if n is None:
        return '未记录(原文无长度>=3数字)'
    if n >= 0.8:
        return '>=0.8 达标'
    if n >= 0.5:
        return '0.5-0.8 卡阈值'
    return '<0.5 明显不符'


def main():
    files = sorted(glob.glob(os.path.join(RAW, '*.json')))
    rows = []
    checked = deg = skip = 0
    per_report = []
    for f in files:
        d = json.load(open(f, encoding='utf-8'))
        m = d.get('_meta', {})
        ev = d.get('证据校验') or {}
        checked += ev.get('checked', 0)
        skip += ev.get('skipped', 0)
        g = ev.get('degraded') or []
        deg += len(g)
        per_report.append(dict(
            company=m.get('company', ''), lang=m.get('lang'), industry=m.get('industry'),
            pdf_bytes=m.get('pdf_bytes', 0), checked=ev.get('checked', 0),
            degraded=len(g), skipped=ev.get('skipped', 0),
            total_score=(d.get('评分增强数据') or {}).get('总分'),
        ))
        for x in g:
            rows.append(dict(
                co=m.get('company', ''), lang=m.get('lang'), industry=m.get('industry'),
                pdf_bytes=m.get('pdf_bytes', 0),
                indicator=x.get('指标', ''), orig=str(x.get('原值', '')),
                txt=x.get('文本命中'), num=x.get('数字命中'),
                reason=x.get('原因', ''),
            ))

    print('样本报告数: %d' % len(per_report))
    print('校验指标数(checked): %d | 跳过(skipped): %d | 降级(degraded): %d' % (checked, skip, deg))
    print('总体降级率 = %d/%d = %.2f%%' % (deg, checked, 100.0 * deg / max(1, checked)))

    # 真拦截 vs 空转
    NOOP = ('未披露', '未提及', '', '-')
    real = [r for r in rows if r['orig'].strip() not in NOOP]
    noop = [r for r in rows if r['orig'].strip() in NOOP]
    print()
    print('真拦截(原值有内容): %d (%.2f%% of checked)' % (len(real), 100.0 * len(real) / max(1, checked)))
    print('空转(原值本就未披露): %d' % len(noop))

    # 分档
    print()
    print('=== 文本命中分档（全部 %d 条）===' % len(rows))
    for k, v in sorted(collections.Counter(band(r['txt']) for r in rows).items()):
        print('  %-16s %d (%.1f%%)' % (k, v, 100.0 * v / len(rows)))
    print()
    print('=== 数字命中分档 ===')
    for k, v in sorted(collections.Counter(nband(r['num']) for r in rows).items()):
        print('  %-28s %d' % (k, v))

    # 疑似误杀
    sus = [r for r in rows if (r['txt'] or 0) >= 0.8]
    print()
    print('=== ★ 疑似误杀候选：文本命中 >=0.8 仍被降级（%d 条）===' % len(sus))
    for r in sorted(sus, key=lambda x: -(x['txt'] or 0)):
        print('  %-26s %s %-14s 文本=%-6s 数字=%-6s 原值=%r'
              % (r['co'][:26], r['lang'], r['indicator'], r['txt'], r['num'], r['orig'][:44]))

    # 高危区：文本命中低 + 原值有内容 ⇒ 最可能是真幻觉
    print()
    print('=== ★ 高置信幻觉：文本命中 <0.4 且原值有实质内容（%d 条）===' % len([r for r in real if (r['txt'] or 0) < 0.4]))
    for r in [r for r in real if (r['txt'] or 0) < 0.4]:
        print('  %-26s %s %-14s 文本=%-6s 数字=%-6s 原值=%r'
              % (r['co'][:26], r['lang'], r['indicator'], r['txt'], r['num'], r['orig'][:44]))

    # 按报告聚类
    print()
    print('=== 按报告聚类（受影响报告 %d/%d = %.1f%%）==='
          % (len([p for p in per_report if p['degraded'] > 0]), len(per_report),
             100.0 * len([p for p in per_report if p['degraded'] > 0]) / len(per_report)))
    byco = collections.Counter(r['co'] for r in rows)
    for k, v in byco.most_common(15):
        rs = [r for r in rows if r['co'] == k]
        avg = sum((r['txt'] or 0) for r in rs) / len(rs)
        maxb = max((r['txt'] or 0) for r in rs)
        print('  %-34s 降级=%-3d 平均文本=%.3f 最高文本=%.3f %s'
              % (k[:34], v, avg, maxb, rs[0]['lang']))

    # 按语言 / 行业
    print()
    print('=== 按语言（分母为报告份数）===')
    lc = collections.Counter(p['lang'] for p in per_report)
    ld = collections.Counter(r['lang'] for r in rows)
    lchk = collections.Counter()
    for p in per_report:
        lchk[p['lang']] += p['checked']
    for lg in sorted(lc):
        print('  %s: 报告 %d 份 | checked %d | 降级 %d | 降级率 %.2f%%'
              % (lg, lc[lg], lchk[lg], ld.get(lg, 0), 100.0 * ld.get(lg, 0) / max(1, lchk[lg])))

    print()
    print('=== 降级指标 TOP12 ===')
    for k, v in collections.Counter(r['indicator'] for r in rows).most_common(12):
        print('  %-28s %d' % (k, v))

    # 原因分布
    print()
    print('=== 降级原因 ===')
    for k, v in collections.Counter(r['reason'] for r in rows).most_common():
        print('  %-30s %d' % (k, v))

    json.dump(dict(checked=checked, skipped=skip, degraded=deg,
                   real_intercept=len(real), noop=len(noop),
                   per_report=per_report, rows=rows),
              open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print()
    print('明细已落盘 → %s' % OUT)


if __name__ == '__main__':
    main()
