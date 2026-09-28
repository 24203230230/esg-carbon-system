# -*- coding: utf-8 -*-
"""v=118b 千分位归一 —— 真实数据 A/B 回归。

同一份 PDF 原文、同一批 112 条已降级条目，分别在
  A：EVIDENCE_THOUSANDS=0（旧行为，等价 v118）
  B：EVIDENCE_THOUSANDS=1（修复后，v118b）
下各跑一次，比较判定结果。

必须成立：
  ① 修复后「放行」的条目，应集中在千分位误杀；真幻觉**一条都不能被放行**（防漏放）；
  ② 修复后仍被拦的，与 A 一致（不引入新放行以外的影响）。
只读，不写任何数据。
"""
import sys, os, json, glob, re, importlib, collections, time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
EXTRACTOR = r'[PROJECT]'
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
RAW = os.path.join(BASE, 'raw_json')
sys.path.insert(0, EXTRACTOR)

os.environ.setdefault('EVIDENCE_MIN_REPORT_CHARS', '200')   # 与线上一致
import pypdfium2 as pdfium
import evidence_verify as ev

TH_CHECK = os.path.join(HERE, '_thousands_check.json')


def collect():
    man = json.load(open(os.path.join(BASE, 'manifest.json'), encoding='utf-8'))
    byco = {c['company']: c.get('pdf', '') for c in man['companies']}
    items = []
    for f in sorted(glob.glob(os.path.join(RAW, '*.json'))):
        d = json.load(open(f, encoding='utf-8'))
        m = d.get('_meta', {})
        for x in (d.get('证据校验') or {}).get('degraded') or []:
            items.append(dict(co=m.get('company', ''), lang=m.get('lang', ''),
                              indicator=x.get('指标', ''),
                              val=str(x.get('原值', '') or ''),
                              raw=str(x.get('原文字段', '') or ''),
                              old_txt=x.get('文本命中'), old_num=x.get('数字命中'),
                              pdf=byco.get(m.get('company', ''), '')))
    return items


def main():
    items = collect()
    print('已降级条目：%d 条，涉及报告 %d 份\n'
          % (len(items), len(set(i['co'] for i in items))))

    # ── 报告全文缓存（只读一次）
    cache, nopdf = {}, 0
    for it in items:
        p = it['pdf']
        if not p or not os.path.exists(p):
            nopdf += 1
            continue
        if p not in cache:
            try:
                doc = pdfium.PdfDocument(p)
                cache[p] = '\n'.join(doc[i2].get_textpage().get_text_range()
                                     for i2 in range(len(doc)))
            except Exception as e:
                cache[p] = ''
                print('  读 PDF 失败 %s: %s' % (os.path.basename(p), e))
    print('报告全文就绪 %d 份 | 无 PDF 条目 %d 条\n' % (len(cache), nopdf))

    def run_all(strip):
        os.environ['EVIDENCE_THOUSANDS'] = '1' if strip else '0'
        importlib.reload(ev)
        idx_cache = {}
        out = {}
        for it in items:
            txt = cache.get(it['pdf'])
            if not txt:
                out[(it['co'], it['indicator'])] = None
                continue
            if it['pdf'] not in idx_cache:
                idx_cache[it['pdf']] = ev.ReportIndex(txt)
            idx = idx_cache[it['pdf']]
            if not idx.ready:
                out[(it['co'], it['indicator'])] = None
                continue
            ok, tr, nr = idx.check(it['raw'], it['val'])
            out[(it['co'], it['indicator'])] = (ok, round(tr, 3),
                                                None if nr is None else round(nr, 3))
        return out

    t0 = time.time()
    print('跑 A（EVIDENCE_THOUSANDS=0，旧行为）...')
    A = run_all(False)
    print('跑 B（EVIDENCE_THOUSANDS=1，修复后）...')
    B = run_all(True)
    print('耗时 %.1fs\n' % (time.time() - t0))

    # ── 分类
    newly_pass, still_block, unk = [], [], []
    for it in items:
        k = (it['co'], it['indicator'])
        a, b = A.get(k), B.get(k)
        if a is None or b is None:
            unk.append(it)
            continue
        if (not a[0]) and b[0]:
            newly_pass.append((it, a, b))
        elif not b[0]:
            still_block.append((it, a, b))

    print('=' * 96)
    print('A/B 对照：修复后「放行」%d 条 | 仍拦 %d 条 | 无法判定 %d 条'
          % (len(newly_pass), len(still_block), len(unk)))
    print('=' * 96)

    print('\n【修复后放行的条目】')
    for it, a, b in newly_pass:
        print('  ○ %-30s %s %-16s 文本 %.3f→%.3f  数字 %s→%s'
              % (it['co'][:30], it['lang'], it['indicator'], a[1], b[1], a[2], b[2]))
        print('      原值=%r' % it['val'][:70])

    # ── 交叉验证：千分位误杀是否全部放行
    print()
    print('=' * 96)
    print('交叉验证：check_thousands 判定的「千分位误杀」是否已全部放行')
    print('=' * 96)
    try:
        bk = json.load(open(TH_CHECK, encoding='utf-8'))
        th_set = set((x[0], x[1]) for x in bk.get('千分位误杀', []))
        real_set = set((x[0], x[1]) for x in bk.get('真不在原文', []))
        np_set = set((it['co'], it['indicator']) for it, _, _ in newly_pass)
        sb_set = set((it['co'], it['indicator']) for it, _, _ in still_block)
        print('千分位误杀 %d 条 → 已放行 %d 条，未放行 %d 条'
              % (len(th_set), len(th_set & np_set), len(th_set - np_set)))
        for k in sorted(th_set - np_set):
            print('      ✗ 未放行：%s | %s' % (k[0][:34], k[1]))
        print()
        print('★ 安全校验：判为「真不在原文」的条目里，被放行的有多少？')
        leak = real_set & np_set
        print('   %d 条（必须为 0）' % len(leak))
        for k in sorted(leak):
            print('      ⚠️ 漏放：%s | %s' % (k[0][:34], k[1]))
        print()
        print('放行条目里不属于「千分位误杀」标签的 %d 条（需人工甄别是否合理）：'
              % len(np_set - th_set))
        for k in sorted(np_set - th_set):
            print('      ? %s | %s' % (k[0][:34], k[1]))
    except Exception as e:
        print('交叉验证失败：%s' % e)

    print()
    out = {'newly_pass': [{'co': it['co'], 'lang': it['lang'], 'indicator': it['indicator'],
                           'old': a, 'new': b, 'val': it['val']}
                          for it, a, b in newly_pass],
           'still_block_n': len(still_block), 'unknown_n': len(unk)}
    json.dump(out, open(os.path.join(HERE, '_regress_v118b.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print('→ _regress_v118b.json')
    os.environ['EVIDENCE_THOUSANDS'] = '1'


if __name__ == '__main__':
    main()
