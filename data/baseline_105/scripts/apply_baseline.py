# -*- coding: utf-8 -*-
"""B-5 把重算结果落到 esg_extractor/weights.json

规则：
  - 替换 `行业基准库` 与 `全市场中位数`
  - **`全局权重` 一字不动**（来自 AHP 词频，与本轮无关）—— 会做一致性断言
  - 旧文件先备份到 `esg_extractor/_backup_weights_pre_B/`

用法：
  python apply_baseline.py --dry     # 只对比，不写
  python apply_baseline.py           # 落库
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PATCH = os.path.join(ROOT, 'weights_patch.json')
EXTRACTOR = r'[PROJECT]'
WEIGHTS = os.path.join(EXTRACTOR, 'weights.json')
BACKUP_DIR = os.path.join(EXTRACTOR, '_backup_weights_pre_B')

# 镜像副本（仓库）——⚠️ 必须是仓库根，不是 esg_extractor/ 子目录
# （2026-09-28 修正：原值写成 .../esg-carbon-system/esg_extractor/weights.json，
#   导致仓库根的 weights.json 一直是旧版、镜像落到了错误的子目录）
MIRRORS = [r'[REPO]/weights.json']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true')
    args = ap.parse_args()

    patch = json.load(open(PATCH, encoding='utf-8'))
    old = json.load(open(WEIGHTS, encoding='utf-8'))

    new_lib = patch['行业基准库']
    new_med = patch['全市场中位数']
    print('样本量 =', patch['样本量'], '| 口径 =', patch['口径'])
    print('新基准库键 =', list(new_lib.keys()))
    print('新中位数指标数 =', len(new_med))

    # ---- 一致性断言：全局权重必须和旧版完全一致 ----
    old_gw = old.get('全局权重', {})
    assert old_gw, '旧 weights.json 的 全局权重 为空，中止'
    assert len(old_gw) == 42, '旧 全局权重 不是 42 项（实际 %d）' % len(old_gw)

    # ---- 指标名对齐检查 ----
    sample_ind = list((new_lib.get('【全市场均值】') or {}).get('指标均值', {}).keys())
    missing = [k for k in old_gw if k not in sample_ind]
    extra = [k for k in sample_ind if k not in old_gw]
    print('指标名对齐：旧权重独有 %d 个 %s | 新均值独有 %d 个 %s'
          % (len(missing), missing[:5], len(extra), extra[:5]))
    if missing:
        print('⚠️ 新基准库缺少旧权重里的指标（下游会取不到均值 → 走兜底）:', missing)

    new = dict(old)
    new['行业基准库'] = new_lib
    new['全市场中位数'] = new_med
    new['版本'] = 'v2.1（B 阶段重算）'
    new['创建日期'] = '2026-09-27'
    src = dict(old.get('数据源') or {})
    src['行业基准库'] = 'baseline_105/raw_json（%d 家，v119b 重提取）+ weights_logic.calc_basic_score' % patch['样本量']
    src['全市场中位数'] = '同上'
    new['数据源'] = src

    # ---- 对比打印 ----
    diff = patch.get('全市场均值对比_旧vs新', {})
    chg = [(k, v['旧'], v['新'], v['差']) for k, v in diff.items() if v['差'] is not None]
    chg.sort(key=lambda x: abs(x[3]), reverse=True)
    print()
    print('全市场均值 变动最大的 12 项：')
    print('  %-28s %8s %8s %8s' % ('指标', '旧', '新', '差'))
    for k, o, n, d in chg[:12]:
        print('  %-28s %8s %8s %+8.2f' % (k[:28], o, n, d))
    if chg:
        import statistics
        print('  变动中位 = %.2f | 平均 = %.2f | 上升 %d 项 / 下降 %d 项'
              % (statistics.median([x[3] for x in chg]),
                 sum(x[3] for x in chg) / len(chg),
                 sum(1 for x in chg if x[3] > 0), sum(1 for x in chg if x[3] < 0)))

    if args.dry:
        print()
        print('[DRY] 未写入')
        return

    # ---- 备份 + 写入 ----
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    bkp = os.path.join(BACKUP_DIR, 'weights_%s.json' % stamp)
    shutil.copy2(WEIGHTS, bkp)
    print()
    print('已备份旧版 →', bkp)

    with open(WEIGHTS, 'w', encoding='utf-8') as f:
        json.dump(new, f, ensure_ascii=False, indent=1)

    for m in MIRRORS:
        try:
            os.makedirs(os.path.dirname(m), exist_ok=True)
            shutil.copy2(WEIGHTS, m)
            print('已同步镜像 →', m)
        except Exception as e:
            print('镜像同步失败', m, e)

    # ---- 读回校验 ----
    chk = json.load(open(WEIGHTS, encoding='utf-8'))
    print()
    print('=== 写后校验 ===')
    print('  全局权重 == 旧版 ?', chk['全局权重'] == old_gw)
    print('  行业基准库键数 =', len(chk['行业基准库']))
    print('  全市场中位数项数 =', len(chk['全市场中位数']))
    print('  版本 =', chk.get('版本'))
    print('APPLY_DONE')


if __name__ == '__main__':
    main()
