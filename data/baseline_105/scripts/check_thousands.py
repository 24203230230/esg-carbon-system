# -*- coding: utf-8 -*-
"""验证「疑似误杀」的根因：千分位归一化缺失。

思路：对每条 degraded 记录，取「原值」中的数字，分别用【原样】和【加千分位】两种形式
在对应报告全文中检索：
  - 原样命中 ⇒ 不是千分位问题
  - 原样不中、加千分位中 ⇒ ★ 确凿误杀（数据真实，仅格式差异）
  - 两种都不中 ⇒ 真幻觉（数据在原文不存在）
只读，不改任何数据。
"""
import sys, os, re, json, glob

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import pypdfium2 as pdfium

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)                    # data/baseline_105
RAW = os.path.join(BASE, 'raw_json')
REPORT = os.path.join(HERE, '_hallucination_report.json')


def thousands(s):
    """给纯数字串加千分位：3480.32 -> 3,480.32"""
    neg = s.startswith('-')
    if neg:
        s = s[1:]
    if '.' in s:
        ip, dp = s.split('.', 1)
        dp = '.' + dp
    else:
        ip, dp = s, ''
    ip2 = re.sub(r'(?<=\d)(?=(\d{3})+$)', ',', ip)
    return ('-' if neg else '') + ip2 + dp


def main():
    rep = json.load(open(REPORT, encoding='utf-8'))
    rows = rep['rows']
    man = json.load(open(os.path.join(BASE, 'manifest.json'), encoding='utf-8'))
    byco = {}
    for c in man['companies']:
        byco[c['company']] = c.get('pdf', '')

    # 只验「疑似误杀」+「文本命中 0.6~0.8」的中间带，覆盖最可疑的
    target = [r for r in rows if (r['txt'] or 0) >= 0.6]
    print('待验条目（文本命中>=0.6）：%d\n' % len(target))

    cache = {}
    buckets = {'千分位误杀': [], '真不在原文': [], '原样即可命中': [], '无PDF': []}

    for r in sorted(target, key=lambda x: -(x['txt'] or 0)):
        co = r['co']
        pdf = byco.get(co, '')
        if not pdf or not os.path.exists(pdf):
            buckets['无PDF'].append((co, r['indicator'], r['orig'][:30]))
            continue
        if pdf not in cache:
            d = pdfium.PdfDocument(pdf)
            cache[pdf] = '\n'.join(d[i].get_textpage().get_text_range() for i in range(len(d)))
        full = cache[pdf]

        # 取出原值里的数字（长度>=3，含小数的保留）
        nums = re.findall(r'\d[\d,]*\.?\d*', r['orig'])
        nums = [n for n in nums if len(re.sub(r'\D', '', n)) >= 3]
        if not nums:
            continue

        hit_raw = hit_th = miss = 0
        miss_samples = []
        for n in nums:
            nn = n.replace(',', '')
            ok_raw = (n in full) or (nn in full)
            ok_th = (thousands(nn) in full)
            if ok_raw:
                hit_raw += 1
            elif ok_th:
                hit_th += 1
            else:
                miss += 1
                if len(miss_samples) < 2:
                    miss_samples.append(n)

        tag = '原样即可命中' if hit_raw == len(nums) else (
            '千分位误杀' if hit_th > 0 and miss == 0 else (
                '千分位误杀' if hit_th > 0 and hit_th >= hit_raw else '真不在原文'))
        buckets[tag].append((co, r['indicator'], r['txt'], r['num'], r['orig'][:40],
                             '%d/%d原样 %d/%d千分位 %d缺失%s' % (hit_raw, len(nums), hit_th, len(nums),
                                                                  miss, ' 样例:' + ','.join(miss_samples) if miss_samples else '')))
        print('%-26s %s %-16s 文本=%-5s 数字=%-5s | %s' % (co[:26], r['lang'], r['indicator'], r['txt'], r['num'], tag))
        print('      原值=%r' % r['orig'][:70])
        print('      %d/%d 原样命中 | %d/%d 千分位命中 | %d 两版皆无 %s'
              % (hit_raw, len(nums), hit_th, len(nums), miss, miss_samples))

    print()
    print('=' * 74)
    for k, v in buckets.items():
        print('【%s】%d 条' % (k, len(v)))
        for x in v:
            print('   ', x)
    json.dump(buckets, open(os.path.join(HERE, '_thousands_check.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print()
    print('→ _thousands_check.json')


if __name__ == '__main__':
    main()
