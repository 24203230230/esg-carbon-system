# -*- coding: utf-8 -*-
"""诊断：新旧数据在「现行口径」下的完整度/质量分布差异从哪来"""
import glob, json, os, sys, statistics
from collections import Counter
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, r'[PROJECT]')
import weights_logic as WL

RAW = r'[REPO]/data/baseline_105/raw_json'
JD = r'[DATA_ROOT]/ESG-JSON-格式提取结果'

L4S = [l for dim in [["碳总量","碳排放强度","范围一排放","范围二排放","范围三排放","碳排放抵消与同比变化","环保投资","碳管理体系建设","环境管理体系","能源类型和强度","能耗同比变化","节能目标与改造情况","能效提升对比度","供应商碳目标","供应商碳披露及评价要求","供应商碳培训及考核情况","供应商绿色采购标准","供应商激励","绿色物流","包装减量及回收情况","循环经济链塑造","高碳供应商占比","气候风险暴露供应商数量","碳合规风险事件发生率","供应链碳风险传导影响评估","碳排放披露范围","供应链碳披露层级","碳资产与交易信息披露比例","第三方核查覆盖率","ESG评级结果及行业排名","ISO14064/ISO50001认证","信息披露频次","监管强制披露要求符合性","交易所ESG披露指引符合性","历史信息披露更正次数","绿色信贷","绿色债券","碳金融衍生品参与情况","碳配额管理","CCER开发与储备","碳资产交易收益","碳资产保值增值策略"]] for l in dim]


def nodes_of(d):
    out = {}
    for dim, dv in (d.get('指标提取结果') or {}).items():
        if not isinstance(dv, dict):
            continue
        subs = dv.get('指标', dv)
        if not isinstance(subs, dict):
            continue
        for sub, sv in subs.items():
            if not isinstance(sv, dict):
                continue
            for l4, node in sv.items():
                if isinstance(node, dict):
                    out[l4] = node
    return out


def diag(name, files, loader):
    comp = Counter(); qual = Counter(); keyw = Counter()
    tot = 0; nonempty = 0; files_ok = 0
    per_file = []
    for fp in files:
        try:
            d = loader(fp)
        except Exception:
            continue
        ns = nodes_of(d)
        if not ns:
            continue
        files_ok += 1
        s = 0
        for l4 in L4S:
            n = ns.get(l4) or {}
            if not isinstance(n, dict):
                n = {}
            c = WL.calc_completeness(n); q = WL.calc_quality(n)
            comp[c] += 1; qual[q] += 1
            tot += 1; s += c + q
            keyw[tuple(sorted(n.keys()))] += 1
            if (n.get('值') or '').strip() not in ('', '未披露'):
                nonempty += 1
        per_file.append(s / len(L4S))
    print('=' * 74)
    print('%s  文件 %d 份 | node %d' % (name, files_ok, tot))
    print('  完整度分布 0/1/2/3 = %s' % [comp.get(i, 0) for i in range(4)])
    print('  质量分分布 0/1/2/3 = %s' % [qual.get(i, 0) for i in range(4)])
    print('  平均完整度 = %.2f | 平均质量 = %.2f | 平均基础分 = %.2f'
          % (sum(k * v for k, v in comp.items()) / max(1, tot),
             sum(k * v for k, v in qual.items()) / max(1, tot),
             (sum(k * v for k, v in comp.items()) + sum(k * v for k, v in qual.items())) / max(1, tot)))
    print('  「值」非空率 = %.1f%%' % (100.0 * nonempty / max(1, tot)))
    print('  每份平均基础分Σ42 = %.2f' % (sum(per_file) / max(1, len(per_file))))
    wsum = 0.0
    for l4 in L4S:
        pass
    print('  node 键组合 top3 = %s' % keyw.most_common(3))


diag('新提取（raw_json, v119b）',
     sorted(glob.glob(os.path.join(RAW, '*.json'))),
     lambda fp: json.load(open(fp, encoding='utf-8')))

diag('旧 JSON（ESG-JSON-格式提取结果）',
     sorted(glob.glob(os.path.join(JD, '*.json'))),
     lambda fp: json.load(open(fp, encoding='utf-8')))

# 加权 Σ 对比
def wsum(files, loader):
    tot = []
    for fp in files:
        try:
            d = loader(fp)
        except Exception:
            continue
        ns = nodes_of(d)
        if not ns:
            continue
        s = 0.0
        for l4 in L4S:
            n = ns.get(l4) or {}
            if not isinstance(n, dict):
                n = {}
            s += WL.calc_basic_score(n) * WL.GLOBAL_WEIGHTS.get(l4, 0)
        tot.append(s)
    return (sum(tot) / len(tot)) if tot else 0, len(tot)


print()
print('=' * 74)
a, na = wsum(sorted(glob.glob(os.path.join(RAW, '*.json'))),
             lambda fp: json.load(open(fp, encoding='utf-8')))
b, nb = wsum(sorted(glob.glob(os.path.join(JD, '*.json'))),
             lambda fp: json.load(open(fp, encoding='utf-8')))
print('加权基础分 Σ：新数据(%d 份) = %.3f   |   旧数据(%d 份) = %.3f' % (na, a, nb, b))
