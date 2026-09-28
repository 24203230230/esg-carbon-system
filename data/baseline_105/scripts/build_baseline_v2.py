# -*- coding: utf-8 -*-
"""B-4 基准库重建（现行口径）

与旧 build_baseline.py 的区别：
  1. 数据源：raw_json/ 的新提取结果（v119b 链路），不是旧 JSON
  2. 判分口径：**直接调用 weights_logic.calc_basic_score()**，杜绝"两套口径"
  3. 行业标签：读 manifest.json（xlsx 权威表 + 人工补丁）
  4. 新增输出 weights_patch.json（含新旧差异对照）

产出：
  excel/行业基准库.xlsx     ← Sheet: 行业基准库 / 行业分布总览 / 降级说明 / 原始行业均值
  excel/基础分明细.xlsx     ← 每公司 × 42 指标基础分
  weights_patch.json        ← 可直接替换 weights.json 的 行业基准库 / 全市场中位数（全局权重不动）
"""
import glob
import json
import os
import statistics
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(ROOT, 'raw_json')
MANIFEST = os.path.join(ROOT, 'manifest.json')
EXCEL = os.path.join(ROOT, 'excel')
PATCH = os.path.join(ROOT, 'weights_patch.json')
EXTRACTOR = r'[PROJECT]'
OLD_WEIGHTS = os.path.join(EXTRACTOR, 'weights.json')

sys.path.insert(0, EXTRACTOR)
import weights_logic as WL          # noqa: E402

MIN_SAMPLE = 10

L4_MAP = {
    "企业内部碳管理": {
        "碳排放": ["碳总量", "碳排放强度", "范围一排放", "范围二排放", "范围三排放", "碳排放抵消与同比变化"],
        "环境治理": ["环保投资", "碳管理体系建设", "环境管理体系"],
        "能源消耗与节能方案": ["能源类型和强度", "能耗同比变化", "节能目标与改造情况", "能效提升对比度"]},
    "供应链碳管理": {
        "供应商碳管理": ["供应商碳目标", "供应商碳披露及评价要求", "供应商碳培训及考核情况", "供应商绿色采购标准"],
        "供应链协同减排": ["供应商激励", "绿色物流", "包装减量及回收情况", "循环经济链塑造"],
        "供应链风险管控": ["高碳供应商占比", "气候风险暴露供应商数量", "碳合规风险事件发生率", "供应链碳风险传导影响评估"]},
    "透明度": {
        "披露范围与完整性": ["碳排放披露范围", "供应链碳披露层级", "碳资产与交易信息披露比例"],
        "披露质量与可信度": ["第三方核查覆盖率", "ESG评级结果及行业排名", "ISO14064/ISO50001认证", "信息披露频次"],
        "披露合规性": ["监管强制披露要求符合性", "交易所ESG披露指引符合性", "历史信息披露更正次数"]},
    "金融化情况": {
        "碳金融": ["绿色信贷", "绿色债券", "碳金融衍生品参与情况"],
        "碳资产管理": ["碳配额管理", "CCER开发与储备", "碳资产交易收益", "碳资产保值增值策略"]}}

INDICATORS = [l4 for dim in L4_MAP for sub in L4_MAP[dim] for l4 in L4_MAP[dim][sub]]


def flatten_nodes(dims):
    """指标提取结果 → {L4指标: node}（容错：'指标' 中间层可有可无）"""
    out = {}
    for dim, dv in (dims or {}).items():
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


def main():
    man = json.load(open(MANIFEST, encoding='utf-8'))
    by_no = {c['no']: c for c in man['companies']}

    files = sorted(glob.glob(os.path.join(RAW, '*.json')))
    rows = []
    bad = []
    for fp in files:
        try:
            d = json.load(open(fp, encoding='utf-8'))
        except Exception as e:
            bad.append((os.path.basename(fp), 'JSON解析失败 %s' % e))
            continue
        meta = d.get('_meta') or {}
        nodes = flatten_nodes(d.get('指标提取结果'))
        if not nodes:
            bad.append((os.path.basename(fp), '无 指标提取结果'))
            continue
        comp = {}
        qual = {}
        base = {}
        for ind in INDICATORS:
            n = nodes.get(ind)
            if not isinstance(n, dict):
                n = {}
            comp[ind] = WL.calc_completeness(n)
            qual[ind] = WL.calc_quality(n)
            base[ind] = comp[ind] + qual[ind]
        rows.append({
            'file': os.path.basename(fp),
            'company': meta.get('company') or '',
            'industry': meta.get('industry') or '',
            'lang': meta.get('lang') or '',
            'no': meta.get('manifest_no') or 0,
            'build': meta.get('extract_build') or '',
            'comp': comp, 'qual': qual, 'base': base,
            'n_ind': sum(1 for i in INDICATORS if base[i] > 0),
        })

    print('读取 raw_json = %d 份 | 可用 %d 份 | 异常 %d 份' % (len(files), len(rows), len(bad)))
    for b in bad:
        print('   ✗', b[0], '->', b[1])
    if not rows:
        print('NO_DATA'); return

    # ---------- 聚合 ----------
    all_base = {ind: [r['base'][ind] for r in rows] for ind in INDICATORS}
    all_market = {ind: round(sum(v) / len(v), 2) for ind, v in all_base.items()}
    medians = {ind: round(statistics.median(v), 2) for ind, v in all_base.items()}

    gidx = {}
    for r in rows:
        gidx.setdefault(r['industry'] or '未分类', []).append(r)

    ind_stats = {}
    for ind_name, grp in gidx.items():
        n = len(grp)
        raw_mean = {i: round(sum(x['base'][i] for x in grp) / n, 2) for i in INDICATORS}
        downgrade = n < MIN_SAMPLE
        ind_stats[ind_name] = {
            'n': n,
            'raw_mean': raw_mean,
            'downgrade': downgrade,
            # 与旧库同构：样本不足时写全市场均值（运行时也会同样降级，行为零变化）
            'used_mean': all_market if downgrade else raw_mean,
            'source': ('全市场均值（样本不足%d，降级）' % MIN_SAMPLE) if downgrade else '行业均值',
        }

    # ---------- weights_patch.json ----------
    old = {}
    try:
        old = json.load(open(OLD_WEIGHTS, encoding='utf-8'))
    except Exception as e:
        print('旧 weights.json 读取失败:', e)

    new_base_lib = {}
    for k, v in ind_stats.items():
        new_base_lib[k] = {'样本数': v['n'], '基准来源': v['source'], '指标均值': v['used_mean']}
    new_base_lib['【全市场均值】'] = {
        '样本数': len(rows), '基准来源': '全市场', '指标均值': all_market}

    diff = {}
    old_all_m = (old.get('行业基准库') or {}).get('【全市场均值】') or {}
    old_am = (old_all_m or {}).get('指标均值', {}) if isinstance(old_all_m, dict) else {}
    for ind in INDICATORS:
        ov = old_am.get(ind)
        nv = all_market.get(ind)
        diff[ind] = {'旧': ov, '新': nv,
                     '差': (round(nv - ov, 2) if isinstance(ov, (int, float)) else None)}

    patch = {
        '生成时间': '2026-09-27',
        '口径': 'weights_logic.calc_basic_score（完整度+质量，0-6）',
        '样本量': len(rows),
        '全局权重': '不变（来自 AHP 词频，与本轮无关）',
        '行业基准库': new_base_lib,
        '全市场中位数': medians,
        '全市场均值对比_旧vs新': diff,
    }
    with open(PATCH, 'w', encoding='utf-8') as f:
        json.dump(patch, f, ensure_ascii=False, indent=1)
    print('已写', PATCH)

    # ---------- Excel ----------
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    hf = Font(name='Microsoft YaHei', bold=True, size=10, color='FFFFFF')
    hfl = PatternFill(start_color='2B579A', end_color='2B579A', fill_type='solid')
    df = Font(name='Microsoft YaHei', size=10, color='333333')
    bdr = Border(left=Side(style='thin', color='CCCCCC'), right=Side(style='thin', color='CCCCCC'),
                 top=Side(style='thin', color='CCCCCC'), bottom=Side(style='thin', color='CCCCCC'))
    ca = Alignment(horizontal='center', vertical='center')

    def hc(ws, r, c, v):
        cl = ws.cell(row=r, column=c, value=v)
        cl.font = hf; cl.fill = hfl; cl.alignment = ca; cl.border = bdr

    def dc(ws, r, c, v, center=False):
        cl = ws.cell(row=r, column=c, value=v)
        cl.font = df; cl.border = bdr
        if center:
            cl.alignment = ca

    os.makedirs(EXCEL, exist_ok=True)
    keys = INDICATORS

    wb = openpyxl.Workbook()
    ws1 = wb.active; ws1.title = '行业基准库'
    heads = ['行业', '样本数', '基准来源'] + keys
    for ci, h in enumerate(heads, 1):
        hc(ws1, 1, ci, h)
        ws1.column_dimensions[get_column_letter(ci)].width = 22 if ci <= 3 else 13
    ri = 2
    for i in sorted(ind_stats.keys()):
        v = ind_stats[i]
        dc(ws1, ri, 1, i); dc(ws1, ri, 2, v['n'], True); dc(ws1, ri, 3, v['source'])
        for ci, k in enumerate(keys, 4):
            dc(ws1, ri, ci, v['used_mean'][k], True)
        ri += 1
    dc(ws1, ri, 1, '【全市场均值】'); dc(ws1, ri, 2, len(rows), True); dc(ws1, ri, 3, '全样本综合')
    for ci, k in enumerate(keys, 4):
        dc(ws1, ri, ci, all_market[k], True)
    ws1.freeze_panes = 'D2'

    ws2 = wb.create_sheet('行业分布总览')
    for ci, h in enumerate(['行业', '样本数', '对标基准', '判定'], 1):
        hc(ws2, 1, ci, h)
        ws2.column_dimensions[get_column_letter(ci)].width = [16, 10, 26, 40][ci - 1]
    for ri2, i in enumerate(sorted(ind_stats, key=lambda x: -ind_stats[x]['n']), 2):
        v = ind_stats[i]
        dc(ws2, ri2, 1, i); dc(ws2, ri2, 2, v['n'], True)
        dc(ws2, ri2, 3, v['source'])
        dc(ws2, ri2, 4, '样本不足%d家，降级用全市场均值' % MIN_SAMPLE if v['downgrade'] else '样本充足，可直接对标')
    ws2.freeze_panes = 'A2'

    ws4 = wb.create_sheet('原始行业均值')
    ws4['A1'] = '未降级的真实行业均值（供审计；weights.json 中样本<10 的行业会按运行时规则降级为全市场均值）'
    ws4['A1'].font = Font(name='Microsoft YaHei', bold=True, size=10)
    for ci, h in enumerate(['行业', '样本数'] + keys, 1):
        hc(ws4, 2, ci, h)
        ws4.column_dimensions[get_column_letter(ci)].width = 22 if ci <= 2 else 13
    for ri3, i in enumerate(sorted(ind_stats), 3):
        v = ind_stats[i]
        dc(ws4, ri3, 1, i); dc(ws4, ri3, 2, v['n'], True)
        for ci, k in enumerate(keys, 3):
            dc(ws4, ri3, ci, v['raw_mean'][k], True)
    ws4.freeze_panes = 'C3'

    ws3 = wb.create_sheet('降级说明')
    for line in [
        '分层对标降级规则', '',
        '阈值：行业样本量 < %d 家 → 降级为全市场 %d 家均值' % (MIN_SAMPLE, len(rows)),
        '', '原因：小样本行业均值方差过大，对标说服力不足',
        '', '本表由 build_baseline_v2.py 生成，判分口径 = weights_logic.calc_basic_score',
        '（完整度 0-3 + 质量 0-3 = 基础分 0-6），与线上评分同源。']:
        ws3.append([line])

    p1 = os.path.join(EXCEL, '行业基准库.xlsx')
    wb.save(p1)

    wb2 = openpyxl.Workbook(); ws = wb2.active; ws.title = '基础分明细'
    dheads = ['序号', '企业名', '行业', '语言', '得分指标数'] + keys
    _w = [6, 30, 12, 6, 11] + [9] * len(keys)
    for ci, h in enumerate(dheads, 1):
        hc(ws, 1, ci, h)
        ws.column_dimensions[get_column_letter(ci)].width = _w[ci - 1]
    for ri4, r in enumerate(sorted(rows, key=lambda x: x['no']), 2):
        dc(ws, ri4, 1, r['no'], True); dc(ws, ri4, 2, r['company'][:40])
        dc(ws, ri4, 3, r['industry']); dc(ws, ri4, 4, r['lang'], True)
        dc(ws, ri4, 5, r['n_ind'], True)
        for ci, k in enumerate(keys, 6):
            dc(ws, ri4, ci, r['base'][k], True)
    ws.freeze_panes = 'F2'
    p2 = os.path.join(EXCEL, '基础分明细.xlsx')
    wb2.save(p2)

    # ---------- 汇总 ----------
    nz = [r['n_ind'] for r in rows]
    print('-' * 70)
    print('样本量 = %d 家' % len(rows))
    print('行业数 = %d（其中样本<10 降级 %d 个）'
          % (len(ind_stats), sum(1 for v in ind_stats.values() if v['downgrade'])))
    print('每份得分指标数：中位 %d / 均值 %.1f' % (statistics.median(nz), sum(nz) / len(nz)))
    tot = [sum(r['base'][k] * WL.GLOBAL_WEIGHTS.get(k, 0) for k in INDICATORS) for r in rows]
    print('加权基础分 Σ：全市场均值 %.3f' % (sum(tot) / len(tot)))
    print('产出：', p1)
    print('产出：', p2)
    print('产出：', PATCH)
    print('BASELINE_V2_DONE')


if __name__ == '__main__':
    main()
