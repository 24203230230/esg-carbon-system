# -*- coding: utf-8 -*-
"""量化 v118 证据校验对总分的影响程度（幻觉影响画像）。

只读；输出 _impact_report.txt / _impact_report.json

口径说明（关键假设，务必与结论一起读）：
  总分（0-10）= Σ(42个L4得分 × 全局权重)
  L4得分（0-10）= 基础分(0-6) + 对标分(0-4)
  被降级 ⇒ 该指标按「未披露」计：基础分 0，对标分 0~1
  降级前按「有值」计：
      情景-保守 原为定性披露 → 基础分 4 + 对标分 2 ≈ L4 6
      情景-定量 原为定量披露 → 基础分 6 + 对标分 3 ≈ L4 9
  Δ总分 = Σ(权重_i × ΔL4_i)
"""
import sys, os, json, glob, collections

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
RAW = os.path.join(BASE, 'raw_json')
W = r'[PROJECT]\weights.json'
HAL = os.path.join(HERE, '_hallucination_report.json')
TH = os.path.join(HERE, '_thousands_check.json')
OUT = os.path.join(HERE, '_impact_report.txt')

L = []


def p(s=''):
    print(s)
    L.append(s)


def main():
    weights = json.load(open(W, encoding='utf-8'))['全局权重']
    hal = json.load(open(HAL, encoding='utf-8'))
    th = json.load(open(TH, encoding='utf-8'))
    rows = hal['rows']

    miss_names = set()
    for cat in ('千分位误杀',):
        for x in th.get(cat, []):
            miss_names.add((x[0], x[1]))

    def w_of(ind):
        return weights.get(ind, 1.0 / 42)

    real = [r for r in rows if r['orig'].strip() not in ('未披露', '未提及', '', '-')]
    TP = [r for r in real if (r['co'], r['indicator']) not in miss_names]      # 真幻觉
    FP = [r for r in real if (r['co'], r['indicator']) in miss_names]          # 疑似误杀

    p('=' * 76)
    p('v118 证据强制校验 —— 幻觉拦截效果与影响程度量化')
    p('=' * 76)
    p()
    p('【一、拦截总量】')
    p('  校验指标数(checked) : %d' % hal['checked'])
    p('  未披露跳过(skipped) : %d' % hal['skipped'])
    p('  触发降级(degraded)  : %d  = %.2f%% of checked' % (hal['degraded'], 100.0 * hal['degraded'] / hal['checked']))
    p('    其中 原值有内容（真拦截）: %d = %.2f%%' % (len(real), 100.0 * len(real) / hal['checked']))
    p('    其中 原值本就未披露（空转）: %d' % hal['noop'])
    p('  受影响报告            : %d / 92 = %.1f%%'
      % (len(set(r['co'] for r in rows)), 100.0 * len(set(r['co'] for r in rows)) / 92))
    p()

    # 影响估算
    p('【二、若这些证据全部放行，总分会被抬高多少】（按指标权重加权）')
    p('  Δ总分 = Σ(权重 × ΔL4)；L4 落幅 6（保守，定性）~ 9（定量）')
    p()
    p('  %-22s %5s %10s %10s' % ('类别', '条数', '保守Δ总分', '定量Δ总分'))
    for tag, arr in (('全部真拦截', real), ('  真幻觉(应拦)', TP), ('  疑似误杀(误拦)', FP)):
        dw6 = sum(w_of(r['indicator']) * 6 for r in arr)
        dw9 = sum(w_of(r['indicator']) * 9 for r in arr)
        p('  %-22s %5d %10.3f %10.3f' % (tag, len(arr), dw6, dw9))
    p()

    # 单份报告影响
    p('【三、单份报告受影响幅度】（保守口径 ΔL4=6，累加该报告全部降级项）')
    byco = collections.defaultdict(list)
    for r in rows:
        byco[r['co']].append(r)
    rank = []
    for co, arr in byco.items():
        d6 = sum(w_of(r['indicator']) * 6 for r in arr)
        d9 = sum(w_of(r['indicator']) * 9 for r in arr)
        rank.append((co, len(arr), d6, d9, arr[0]['lang'], arr[0].get('industry', '')))
    rank.sort(key=lambda x: -x[2])
    p('  %-34s %4s %9s %9s %s' % ('报告', '降级', '保守Δ', '定量Δ', '语言'))
    for co, n, d6, d9, lg, ind in rank[:15]:
        p('  %-34s %4d %9.3f %9.3f %s' % (co[:34], n, d6, d9, lg))
    p()
    tot6 = sum(x[2] for x in rank)
    p('  38 份受影响报告合计影响（区间）: %.3f ~ %.3f 分' % (tot6, sum(x[3] for x in rank)))
    p('  平均每份受影响报告: %.3f ~ %.3f 分' % (tot6 / len(rank), sum(x[3] for x in rank) / len(rank)))
    p()

    # 按指标
    p('【四、降级指标 TOP12 与其权重】')
    cnt = collections.Counter(r['indicator'] for r in rows)
    p('  %-28s %5s %10s' % ('指标', '次数', '权重'))
    for k, v in cnt.most_common(12):
        p('  %-28s %5d %10.5f' % (k, v, weights.get(k, 0)))
    p()

    # 误杀清单
    p('【五、疑似误杀清单（数据真实、仅因千分位格式差异被判"无法定位"）】共 %d 条' % len(FP))
    for r in sorted(FP, key=lambda x: -(x['txt'] or 0)):
        p('  %-30s %-4s %-16s 文本=%-5s 原值=%r' % (r['co'][:30], r['lang'], r['indicator'], r['txt'], r['orig'][:44]))
    p()
    p('【六、真幻觉清单（原文确实查无此数）】共 %d 条（按文本命中升序）' % len(TP))
    for r in sorted(TP, key=lambda x: (x['txt'] if x['txt'] is not None else 9)):
        p('  %-30s %-4s %-16s 文本=%-5s 数字=%-5s 原值=%r' % (r['co'][:30], r['lang'], r['indicator'], r['txt'], r['num'], r['orig'][:40]))

    open(OUT, 'w', encoding='utf-8').write('\n'.join(L))
    p()
    p('→ %s' % OUT)


if __name__ == '__main__':
    main()
