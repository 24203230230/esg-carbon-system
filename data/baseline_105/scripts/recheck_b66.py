# -*- coding: utf-8 -*-
"""B-6.6 全链路回归复核 —— 同一份报告重复 N 次，量化「分数一致性」与「语言/繁简对照」

目的（对应计划 §6.6）：
  1. 一致性：同一份报告跑 N 次，看总分极差、是否跨档、维度级差异落在哪一维。
  2. 语言对照：同一家公司的中/英双版报告（宏微）跑同一链路，看分数是否仍被
     截断系统性压低（A2 配额采样的验收判据）。
  3. 繁简：繁体报告（牧原）走 C1 繁简归一后的表现。

走**生产公网链路**（/api/extract_async + 轮询 /api/extract_status），与线上口径完全一致。
串行执行（不并发），避免服务器 OOM。

用法：
  python recheck_b66.py --pdf <路径> --label 牧原 --times 5
  python recheck_b66.py --batch            # 跑内置对照集

产出：scripts/_recheck/<label>/run<N>.json + _recheck/_summary.json/_summary.txt
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
import uuid
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.environ.get('ESG_BASE', 'http://localhost:8082')
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUTDIR = os.path.join(HERE, '_recheck')
READY_TIMEOUT = 7200
POST_TIMEOUT = 600

OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl.create_default_context()))
_t0 = time.time()
LOG = []

# 内置对照集（实验室里的三组：繁简 / 中英 / 稳定性）
BATCH = [
    ('牧原繁体', r'[HOME]\Downloads\牧原股份：2025年度环境、社会及治理（ESG）报告.pdf', 5),
    ('宏微中文', r'[DATA_ROOT]\ESG报告\补充的ESG报告\宏微科技：江苏宏微科技股份有限公司2025年度环境、社会和公司治理（ESG）报告--中文.pdf', 3),
    ('宏微英文', r'[DATA_ROOT]\ESG报告\补充的ESG报告\宏微科技：江苏宏微科技股份有限公司2025年度环境、社会和公司治理（ESG）报告--英文.pdf', 3),
]

LEVELS = [(9.0, '标杆'), (7.0, '先进'), (5.0, '发展中'), (3.0, '基础'), (1.0, '初始')]


LIVELOG = os.path.join(OUTDIR, '_live.log')


def log(msg):
    line = '[%6.0fs] %s' % (time.time() - _t0, msg)
    print(line, flush=True)
    LOG.append(line)
    try:
        os.makedirs(OUTDIR, exist_ok=True)
        with open(LIVELOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def grade_of(x):
    if x is None:
        return '?'
    for lo, nm in LEVELS:
        if x >= lo:
            return nm
    return '空白'


def _get(u, t=60):
    req = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'})
    with OPENER.open(req, timeout=t) as r:
        return r.read().decode('utf-8', 'replace')


def _post_file(url, path, extra, timeout=POST_TIMEOUT):
    b = uuid.uuid4().hex.encode()
    parts = []
    for k, v in extra.items():
        parts.append(b'--' + b + b'\r\nContent-Disposition: form-data; name="'
                     + k.encode() + b'"\r\n\r\n' + str(v).encode() + b'\r\n')
    data = open(path, 'rb').read()
    fn = os.path.basename(path).encode('utf-8')
    parts.append(b'--' + b + b'\r\nContent-Disposition: form-data; name="file"; filename="'
                 + fn + b'"\r\nContent-Type: application/pdf\r\n\r\n' + data + b'\r\n')
    parts.append(b'--' + b + b'--\r\n')
    req = urllib.request.Request(
        url, data=b''.join(parts), method='POST',
        headers={'Content-Type': 'multipart/form-data; boundary=' + b.decode(),
                 'User-Agent': 'Mozilla/5.0'})
    with OPENER.open(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


def _wait_healthy(max_wait=300):
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            d = json.loads(_get(BASE + '/api/health', t=20))
            if d.get('build'):
                return d
        except Exception:
            pass
        time.sleep(10)
    return None


def poll(tid, max_wait=READY_TIMEOUT):
    t0 = time.time()
    last_sig = None
    while time.time() - t0 < max_wait:
        try:
            snap = json.loads(_get('%s/api/extract_status/%s' % (BASE, tid)))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RuntimeError('TASK_LOST：任务 %s 不存在（容器疑似重启）' % tid[:12])
            if e.code in (502, 503, 504, 524):
                log('    ↳ 网关 %s，等服务恢复…' % e.code)
                _wait_healthy(max_wait=180)
                continue
            time.sleep(6)
            continue
        except Exception:
            time.sleep(6)
            continue
        st = snap.get('status')
        if st is None or snap.get('error'):
            # 服务端有时以 200 + {"error":...} 返回「任务不存在」⇒ 必须当致命错误
            raise RuntimeError('TASK_LOST：%s' % str(snap)[:120])
        sig = (snap.get('stage'), snap.get('current'), snap.get('total'))
        if sig != last_sig and sig[1]:
            last_sig = sig
            log('    ↳ %s %s/%s' % (sig[0], sig[1], sig[2]))
        if st in ('done', 'success', 'completed', 'failed', 'error'):
            return snap
        time.sleep(6)
    raise RuntimeError('poll 超时 %ds（task=%s）' % (max_wait, tid[:12]))


def run_once(pdf, outfile, company_type='listed', tries=3):
    last = None
    for attempt in range(1, tries + 1):
        try:
            return _run_once(pdf, outfile, company_type)
        except Exception as e:
            last = e
            log('    ! 第%d次失败：%s' % (attempt, str(e)[:140]))
            if attempt < tries:
                time.sleep(10)
    raise last


def _run_once(pdf, outfile, company_type='listed'):
    t0 = time.time()
    j = json.loads(_post_file(BASE + '/api/extract_async', pdf,
                              {'company_type': company_type}))
    tid = j.get('task_id')
    if not tid:
        raise RuntimeError('no task_id: %s' % str(j)[:200])
    log('  提交 task=%s' % tid[:12])
    snap = poll(tid)
    ext = (snap.get('result') or {}).get('extracted') or {}
    if not ext.get('指标提取结果'):
        raise RuntimeError('empty 指标提取结果 (status=%s, err=%s)'
                           % (snap.get('status'), str(snap.get('error'))[:120]))
    el = time.time() - t0
    payload = {'_meta': {
        'pdf': pdf, 'pdf_file': os.path.basename(pdf),
        'pdf_bytes': os.path.getsize(pdf), 'task_id': tid,
        'elapsed_s': round(el, 1),
        'fetched_at': time.strftime('%Y-%m-%d %H:%M:%S')}}
    payload.update(ext)
    os.makedirs(os.path.dirname(outfile), exist_ok=True)
    with open(outfile, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return payload, el


def extract_metrics(d):
    sd = d.get('评分增强数据') or {}
    dims = sd.get('维度得分') or {}
    return {
        '总分': sd.get('总分'),
        '等级': sd.get('评分等级'),
        '维度': {k: (v.get('得分') if isinstance(v, dict) else v) for k, v in dims.items()},
    }


def summarize(label, runs):
    """runs: [(总分, 等级, 维度dict)]"""
    tots = [r[0] for r in runs if r[0] is not None]
    if not tots:
        return {'label': label, 'n': len(runs), 'error': 'no valid runs'}
    grades = [grade_of(t) for t in tots]
    diffs = [round(max(tots) - min(tots), 3)]
    cross = len(set(grades)) > 1
    dim_names = sorted({k for r in runs for k in (r[2] or {})})
    dim_stat = {}
    for dn in dim_names:
        vals = [r[2].get(dn) for r in runs if r[2] and r[2].get(dn) is not None]
        if vals:
            dim_stat[dn] = {'min': min(vals), 'max': max(vals),
                            'range': round(max(vals) - min(vals), 3),
                            'vals': vals}
    return {'label': label, 'n': len(runs), 'totals': tots, 'grades': grades,
            'min': min(tots), 'max': max(tots), 'range': diffs[0],
            'cross_grade': cross, 'dims': dim_stat}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pdf')
    ap.add_argument('--label')
    ap.add_argument('--times', type=int, default=5)
    ap.add_argument('--batch', action='store_true')
    ap.add_argument('--type', default='listed')
    args = ap.parse_args()

    try:
        h = json.loads(_get(BASE + '/api/health'))
        log('线上 build=%s | baseline_version=%s | baseline_sample=%s'
            % (h.get('build'), h.get('baseline_version'), h.get('baseline_sample')))
    except Exception as e:
        log('health 读取失败: %s' % e)

    jobs = BATCH if args.batch else [(args.label or 'single', args.pdf, args.times)]

    allsum = []
    for label, pdf, times in jobs:
        if not os.path.exists(pdf):
            log('★ 跳过 %s：PDF 不存在 %s' % (label, pdf))
            continue
        mb = os.path.getsize(pdf) / 1048576
        log('=' * 70)
        log('▶ %s | %.1fMB | 跑 %d 次' % (label, mb, times))
        outdir = os.path.join(OUTDIR, label)
        runs = []
        for i in range(1, times + 1):
            outfile = os.path.join(outdir, 'run%d.json' % i)
            try:
                payload, el = run_once(pdf, outfile, args.type)
                m = extract_metrics(payload)
                runs.append((m['总分'], m['等级'], m['维度']))
                log('  run%d OK  %.0fs 总分=%s(%s) | %s'
                    % (i, el, m['总分'], m['等级'],
                       ', '.join('%s=%s' % (k, v) for k, v in (m['维度'] or {}).items())))
            except Exception as e:
                log('  run%d ★失败 %s' % (i, str(e)[:180]))
            time.sleep(3)
        s = summarize(label, runs)
        allsum.append(s)
        if 'error' not in s:
            log('  ⇒ %s：总分 %s | 极差 %.3f | 跨档 %s | 等级 %s'
                % (label, s['totals'], s['range'], s['cross_grade'], sorted(set(s['grades']))))
            for dn, st in s['dims'].items():
                log('      %-12s %s 极差=%.3f' % (dn, st['vals'], st['range']))

    os.makedirs(OUTDIR, exist_ok=True)
    with open(os.path.join(OUTDIR, '_summary.json'), 'w', encoding='utf-8') as f:
        json.dump({'summary': allsum, 'log': LOG}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUTDIR, '_summary.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(LOG))
    log('=' * 70)
    log('B66_RECHECK_DONE → %s' % OUTDIR)


if __name__ == '__main__':
    main()
