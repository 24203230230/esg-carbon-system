# -*- coding: utf-8 -*-
"""读取回归结果（修正 PowerShell 重定向的编码：UTF-16 外壳 + GBK 误解码）"""
import sys, os, json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
HERE = os.path.dirname(os.path.abspath(__file__))


def read_ps(path):
    raw = open(path, 'rb').read()
    for outer in ('utf-16', 'utf-8', 'gbk'):
        try:
            t = raw.decode(outer)
            break
        except Exception:
            continue
    else:
        return ''
    # 若已是正常中文则直接返回；否则按 GBK 误解码回溯
    if '\ufffd' not in t:
        return t
    try:
        return t.encode('gbk', 'replace').decode('utf-8', 'replace')
    except Exception:
        return t


t = read_ps(os.path.join(HERE, '_regress_v118b_stdout.txt'))
print('=== 全文长度 %d ===' % len(t))
print(t[:1500])
print()
print('=' * 90)
i = t.find('交叉验证')
print(t[i:] if i > 0 else '(未找到交叉验证段)')

print()
print('=' * 90)
print('=== 结构化结果 _regress_v118b.json ===')
j = json.load(open(os.path.join(HERE, '_regress_v118b.json'), encoding='utf-8'))
print('放行条数:', len(j.get('newly_pass', [])), '| 仍拦:', j.get('still_block_n'),
      '| 无法判定:', j.get('unknown_n'))
print()
lang = {}
for r in j.get('newly_pass', []):
    lang[r['lang']] = lang.get(r['lang'], 0) + 1
print('放行条目语言分布:', lang)
print()
for r in j.get('newly_pass', []):
    print('%-34s %s %-16s 数字 %s→%s | 原值=%r'
          % (r['co'][:34], r['lang'], r['indicator'], r['old'][2], r['new'][2], r['val'][:50]))
