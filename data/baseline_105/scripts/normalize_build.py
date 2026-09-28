# -*- coding: utf-8 -*-
"""
B-4 前置：归一化 raw_json 的 build 口径。

背景：
    批处理脚本的 `extract_build` 是「启动时」从 /api/health 读的快照，因此跨部署批次
    会留下多个标号（本轮实测 v119b×78 / v120×14）。而 v119b → v120 → v121 的改动
    **只涉及内存管理**（并发闸门 / pdfminer 页级缓存释放），
    **完全不改变提取口径**（提示词、A2 混合控长、C1 繁简归一、C2 消歧义、v118 证据校验）。

做法：
    1) 原值移入 `runtime_build`（实际运行容器版本，保留可追溯）
    2) `extract_build` 统一写为提取口径版本 `v119b`
    3) 追加 `build_note` 说明差异性质
    4) 全部写回原文件（先备份到 _backup_meta/，只备份 _meta 原值，不动主数据）

用法：
    python normalize_build.py            # 执行
    python normalize_build.py --dry      # 只报告，不写盘
"""
import json
import os
import sys
import collections

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
RAW = os.path.join(BASE, 'raw_json')
BACKUP = os.path.join(BASE, '_backup_meta')

CALIBER = 'v119b'          # 提取口径版本（提示词 + A2 + C1 + C2 + 证据校验 v118）
NOTE = ('v120/v121 仅改内存管理（并发槽位闸门 + pdfminer 页级缓存释放），'
        '不改变提取口径与评分逻辑；故统一标 v119b（提取口径版本），'
        '实际运行容器版本见 runtime_build。')


def main():
    dry = '--dry' in sys.argv
    if not os.path.isdir(RAW):
        print('!! 找不到 raw_json:', RAW)
        return 1

    files = sorted(f for f in os.listdir(RAW) if f.endswith('.json'))
    print('待处理 =', len(files), '份')

    before = collections.Counter()
    changed = 0
    if not dry:
        os.makedirs(BACKUP, exist_ok=True)

    for fn in files:
        p = os.path.join(RAW, fn)
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        meta = d.get('_meta') or {}
        cur = meta.get('extract_build', '(缺失)')
        before[cur] += 1

        # 已经归一化过就跳过（幂等）
        if cur == CALIBER and meta.get('build_note') == NOTE:
            continue

        runtime = meta.get('runtime_build') or cur
        meta['runtime_build'] = runtime
        meta['extract_build'] = CALIBER
        meta['build_note'] = NOTE
        d['_meta'] = meta

        if not dry:
            # 备份原始 _meta（只在首次覆盖时写）
            bp = os.path.join(BACKUP, fn.replace('.json', '.meta.json'))
            if not os.path.exists(bp):
                with open(bp, 'w', encoding='utf-8') as f:
                    json.dump({'extract_build': cur}, f, ensure_ascii=False, indent=2)
            tmp = p + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        changed += 1

    print('归一化前 extract_build 分布 =', dict(before))
    print('%s = %d 份' % ('将改写' if dry else '已改写', changed))
    print('统一为 extract_build =', CALIBER, '| runtime_build 保留实际值')
    if not dry:
        print('原始 _meta 备份 =', BACKUP)
    return 0


if __name__ == '__main__':
    sys.exit(main())
