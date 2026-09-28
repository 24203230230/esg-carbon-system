# -*- coding: utf-8 -*-
"""对 v=118b 修复后「放行」的条目做原文抽验。

判据：修复后 num_rate==1.0 ⇒ 「原文+值」里的每个数字都能在报告全文中定位。
本脚本把这些数字逐个在原文里打印上下文，用铁证区分
  「真实数据被格式差异误杀」（放行正确） vs 「模型编造」（放行错误）。
只读。
"""
import sys, os, json, re

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import pypdfium2 as pdfium

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
NUM = re.compile(r'\d[\d,]*\.?\d*')


def main():
    man = json.load(open(os.path.join(BASE, 'manifest.json'), encoding='utf-8'))
    byco = {c['company']: c.get('pdf', '') for c in man['companies']}
    j = json.load(open(os.path.join(HERE, '_regress_v118b.json'), encoding='utf-8'))
    rows = j['newly_pass']

    # 优先抽验：被标记争议的 + EN/CN 各取代表
    focus = [r for r in rows if '松井' in r['co']]
    focus += [r for r in rows if '格力' in r['co'] and r['indicator'] == '碳总量']
    focus += [r for r in rows if '常州长青' in r['co'] and r['indicator'] == '碳总量']
    focus += [r for r in rows if r['lang'] == 'CN'][:2]

    cache = {}
    for r in focus:
        pdf = byco.get(r['co'], '')
        if not pdf or not os.path.exists(pdf):
            print('!! 无 PDF：%s' % r['co'])
            continue
        if pdf not in cache:
            d = pdfium.PdfDocument(pdf)
            cache[pdf] = '\n'.join(d[i].get_textpage().get_text_range() for i in range(len(d)))
        full = cache[pdf]

        print('=' * 96)
        print('%s | %s | %s' % (r['co'], r['lang'], r['indicator']))
        print('  原值：%s' % r['val'][:90])
        nums = [n for n in NUM.findall(r['val']) if len(re.sub(r'\D', '', n)) >= 3]
        print('  待验数字（%d 个）：%s' % (len(nums), nums))
        for n in nums:
            bare = n.replace(',', '')
            th = re.sub(r'(?<=\d)(?=(\d{3})+$)', ',', bare.split('.')[0]) + \
                 ('.' + bare.split('.')[1] if '.' in bare else '')
            hit_bare = bare in full
            hit_th = th in full
            # 取上下文
            ctx = ''
            for probe in (th, bare, n):
                m = re.search(re.escape(probe), full)
                if m:
                    ctx = full[max(0, m.start() - 90):m.start() + 70].replace('\n', ' | ')
                    break
            print('    %-14s 裸数字命中=%-5s 千分位命中=%-5s' % (n, hit_bare, hit_th))
            if ctx:
                print('        …%s…' % ctx)

    print()
    print('=' * 96)
    print('全部 %d 条放行记录的数字整体命中情况（由回归脚本记录 new[2]=num_rate）' % len(rows))
    bad = [r for r in rows if (r['new'][2] or 0) < 1.0]
    print('  num_rate == 1.0：%d 条' % (len(rows) - len(bad)))
    print('  num_rate < 1.0 ：%d 条' % len(bad))
    for r in bad:
        print('     ? %-30s %s %-16s num=%s 原值=%r'
              % (r['co'][:30], r['lang'], r['indicator'], r['new'][2], r['val'][:50]))


if __name__ == '__main__':
    main()
