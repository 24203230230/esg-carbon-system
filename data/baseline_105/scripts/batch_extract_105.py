# -*- coding: utf-8 -*-
"""B-2 批量重提取（走生产公网链路，断点续跑）—— v2（抗 OOM / 快速失败）

v2 修复（2026-09-27，B-3 卡死事件复盘）：
  1. ⚠️ 真根因＝**服务端 OOM**：服务器仅 ~2GB 内存，旧版 `--workers 3` 同时上传
     兰剑智能 98.4MB（扫描件、OCR 60 页）+ 德石股份 50.8MB + 振德医疗 33.3MB
     → Flask `file.read()` 全量入内存 + pdfplumber + RapidOCR → `dmesg` 记录
     `Out of memory: Killed process (python) anon-rss:1.6GB`，
     **esg-extractor 容器被重启（RestartCount=1）→ 内存态 EXTRACT_TASKS 清空**。
  2. 旧版 `poll()` 解析 404 时走 `except Exception: sleep(6); continue`，
     把「任务不存在（服务端重启）」当成「还没好」傻等 1800s ⇒ 超时后重试再 OOM，
     形成**死循环**（实测静默卡 20 分钟）。v2：404 立即判 `TaskLost` 快速失败。
  3. 并发改为「**内存预算闸门**」：池大小 2；**≥30MB 的文件独占全部槽位**
     （`_EXCL` 互斥），杜绝「多个重任务同时驻留」。单份最大 98.4MB 实测只占 ~350MB，
     配合 baseline ~590MB 远低于 1935MB。
  4. 新增**心跳线程**：每 45s 打印在跑任务的服务端进度（含 `OCR 12/60`、`阶段`），
     长任务不再「静默」。
  5. 失败清单落 `scripts/_batch_extract_fails.json`，可直接喂 `--only`。

用法：
  python batch_extract_105.py --limit 10          # 试点 10 份
  python batch_extract_105.py                     # 全量（92 份，断点续跑）
  python batch_extract_105.py --workers 1         # 串行（最保守）
  python batch_extract_105.py --only 宏微,创新新材  # 只跑指定公司（模糊匹配）
  python batch_extract_105.py --order lang        # 排序：size(默认)/lang/no
  python batch_extract_105.py --dry               # 只列待跑清单，不提交

产出：
  raw_json/<序号>_<公司>.json       —— {_meta, 报告元数据, 指标提取结果, 评分增强数据, 证据校验}
  scripts/_batch_extract.log        —— 运行日志
  scripts/_batch_extract_fails.json —— 失败清单
  末尾回填 manifest.json 的 extract_build
"""
import argparse
import json
import os
import re
import ssl
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.environ.get('ESG_BASE', 'https://[DEPLOY_HOST]')
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(ROOT, 'raw_json')
MANIFEST = os.path.join(ROOT, 'manifest.json')
LOGP = os.path.join(HERE, '_batch_extract.log')
FAILP = os.path.join(HERE, '_batch_extract_fails.json')

# ── 内存预算闸门参数 ────────────────────────────────────────────────
POOL = 2                                   # 并发槽位总数（默认，可 --workers 覆盖）
EXCL_MB = 30                               # ≥ 该体积的文件独占「全部」槽位
# ⚠️ 必须 > 服务端「单份最长耗时」。实测 60 页扫描件 OCR 在 2 核机上可达
#    3900s（65s/页），旧值 1800s 会在服务端仍在正常跑时判超时 ⇒ 重传 ⇒ 新任务
#    排队等独占位 ⇒ 再次超时，形成「任务堆积 + 永远跑不完」的死循环。
READY_TIMEOUT = 7200                       # poll 单任务最长等待（秒）
POST_TIMEOUT = 1800                        # 单次上传/提交超时（秒）

OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl.create_default_context()))
_lk = threading.Lock()
_t0 = time.time()

_SLOTS = threading.Semaphore(POOL)
_EXCL = threading.Lock()
_INFLIGHT = {}
_IL_LOCK = threading.Lock()
_STOP = threading.Event()


class TaskLost(Exception):
    """服务端查不到该 task_id（几乎总是容器重启丢失了内存态任务表）"""


def log(msg):
    line = '[%6.0fs] %s' % (time.time() - _t0, msg)
    with _lk:
        print(line, flush=True)
        with open(LOGP, 'a', encoding='utf-8') as f:
            f.write(line + '\n')


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


def safe_name(s, n=60):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', str(s or '')).strip(' .')
    return s[:n] or 'unnamed'


def out_path(rec):
    return os.path.join(RAW, '%03d_%s.json' % (rec['no'], safe_name(rec['company'])))


def _need_slots(nbytes):
    """≥EXCL_MB 的文件独占全部槽位，其余占 1 个"""
    return POOL if nbytes >= EXCL_MB * 1024 * 1024 else 1


def _gate_enter(n):
    if n >= POOL:
        _EXCL.acquire()
    for _ in range(n):
        _SLOTS.acquire()


def _gate_exit(n):
    for _ in range(n):
        _SLOTS.release()
    if n >= POOL:
        _EXCL.release()


def _il_set(key, **kw):
    with _IL_LOCK:
        _INFLIGHT.setdefault(key, {}).update(kw)


def _il_drop(key):
    with _IL_LOCK:
        _INFLIGHT.pop(key, None)


def _heartbeat_loop():
    """每 45s 打印在跑任务（含服务端 OCR/阶段进度），长任务不再静默"""
    while not _STOP.wait(45):
        with _IL_LOCK:
            items = [(k, dict(v)) for k, v in _INFLIGHT.items()]
        if not items:
            continue
        for k, v in items:
            el = time.time() - v.get('t_start', time.time())
            ph = v.get('phase', '?')
            st, cur, tot = v.get('stage'), v.get('current'), v.get('total')
            extra = ''
            if cur and tot:
                extra = ' %s %s/%s' % (st or '', cur, tot)
            elif st:
                extra = ' %s' % st
            log('  · 在跑 #%-3d %-26s | %-6s%s | 已 %4.0fs'
                % (v.get('no', 0), str(v.get('company', ''))[:26], ph, extra, el))


def _wait_healthy(max_wait=300):
    """等 /api/health 恢复 200（容器被 OOM 重启后会有几十秒空窗）"""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            d = json.loads(_get(BASE + '/api/health', t=20))
            if d.get('build'):
                return d.get('build')
        except Exception:
            pass
        time.sleep(10)
    return None


def poll(tid, key, max_wait=READY_TIMEOUT):
    t0 = time.time()
    queued_since = None
    last_sig = None
    while time.time() - t0 < max_wait:
        try:
            snap = json.loads(_get('%s/api/extract_status/%s' % (BASE, tid)))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # 任务表里没有 = 容器重启过（内存态），立刻失败，别傻等
                raise TaskLost('服务端 404：任务 %s 已不存在（容器疑似重启）' % tid[:12])
            if e.code in (502, 503, 504, 524):
                # 网关错误 = 容器正在 OOM 重启。等健康恢复后继续轮询：
                # 重启会清空内存态任务表 ⇒ 下一轮必然 404 ⇒ 由 TaskLost 走重传。
                _il_set(key, phase='网关%s等服务' % e.code)
                _wait_healthy(max_wait=180)
                continue
            time.sleep(6)
            continue
        except Exception:
            time.sleep(6)
            continue

        st = snap.get('status')
        # 把服务端进度透出给心跳
        _il_set(key, stage=snap.get('stage'), current=snap.get('current'),
                total=snap.get('total'))
        sig = (snap.get('stage'), snap.get('current'), snap.get('total'))
        if sig != last_sig and sig[1]:
            last_sig = sig
            log('    ↳ #%s %s %s/%s' % (tid[:8], sig[0], sig[1], sig[2]))

        if st in ('done', 'success', 'completed', 'failed', 'error'):
            return snap

        if st == 'queued':
            if queued_since is None:
                queued_since = time.time()
            elif time.time() - queued_since > 600:
                raise RuntimeError('任务排队 >600s 未开始（服务端疑似异常）')
        time.sleep(6)
    raise RuntimeError('poll 超时 %ds（task=%s，服务端可能仍在跑）' % (max_wait, tid[:12]))


def run_one(rec, build, nslots):
    out = out_path(rec)
    key = '%d' % rec['no']
    # 断点续跑
    if os.path.exists(out):
        try:
            d = json.load(open(out, encoding='utf-8'))
            if d.get('指标提取结果'):
                return ('skip', rec, 0.0, None)
        except Exception:
            pass
    if not os.path.exists(rec['pdf']):
        return ('fail', rec, 0.0, 'pdf missing')

    _gate_enter(nslots)
    last = None
    try:
        for attempt in range(1, 4):
            t0 = time.time()
            _il_set(key, no=rec['no'], company=rec['company'],
                    t_start=t0, phase='上传', stage=None, current=None, total=None)
            try:
                j = json.loads(_post_file(BASE + '/api/extract_async', rec['pdf'],
                                          {'company_type': 'listed'}))
                tid = j.get('task_id')
                if not tid:
                    raise RuntimeError('no task_id: %s' % str(j)[:200])
                _il_set(key, phase='提取', tid=tid)
                snap = poll(tid, key)
                ext = (snap.get('result') or {}).get('extracted') or {}
                if not ext.get('指标提取结果'):
                    raise RuntimeError('empty 指标提取结果 (status=%s, err=%s)'
                                       % (snap.get('status'), str(snap.get('error'))[:80]))
                el = time.time() - t0
                payload = {'_meta': {
                    'manifest_no': rec['no'], 'company': rec['company'],
                    'industry': rec['industry'], 'lang': rec['lang'],
                    'pdf': rec['pdf'], 'pdf_file': rec['pdf_file'],
                    'pdf_bytes': os.path.getsize(rec['pdf']),
                    'extract_build': build, 'task_id': tid,
                    'elapsed_s': round(el, 1),
                    'fetched_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                }}
                payload.update(ext)
                tmp = out + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, ensure_ascii=False, indent=1)
                os.replace(tmp, out)
                return ('ok', rec, el, ext)
            except TaskLost as e:
                last = 'TaskLost: %s' % e
                log('  ! #%d %s 第%d次失败 %s' % (rec['no'], rec['company'][:16],
                                                  attempt, last[:150]))
                _il_set(key, phase='等服务恢复')
                b = _wait_healthy()
                log('    ↳ 服务恢复: build=%s' % b)
                time.sleep(8)
            except Exception as e:
                last = '%s: %s' % (type(e).__name__, e)
                log('  ! #%d %s 第%d次失败 %s' % (rec['no'], rec['company'][:16],
                                                  attempt, last[:150]))
                code = getattr(e, 'code', None)
                if code in (502, 503, 504, 524):
                    _il_set(key, phase='网关%s等服务' % code)
                    b = _wait_healthy(max_wait=180)
                    log('    ↳ 网关 %s → 服务恢复: build=%s' % (code, b))
                time.sleep(8)
    finally:
        _il_drop(key)
        _gate_exit(nslots)
    return ('fail', rec, 0.0, last)


def order_todo(cands, mode):
    if mode == 'no':
        return sorted(cands, key=lambda c: c['no'])
    if mode == 'lang':
        cn = [c for c in cands if c['lang'] == 'CN']
        en = [c for c in cands if c['lang'] != 'CN']
        out = []
        for i in range(max(len(cn), len(en))):
            if i < len(cn):
                out.append(cn[i])
            if i < len(en):
                out.append(en[i])
        return out
    # size：小→大（最大化完成数；最大的留在最后，且闸门保证其独占）
    def _sz(c):
        try:
            return os.path.getsize(c['pdf'])
        except Exception:
            return 0
    return sorted(cands, key=_sz)


def main():
    global POOL, EXCL_MB
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--workers', type=int, default=2, help='并发槽位总数（默认 2）')
    ap.add_argument('--excl-mb', type=int, default=30, help='≥该体积独占全部槽位')
    ap.add_argument('--order', default='size', choices=['size', 'lang', 'no'])
    ap.add_argument('--poll', type=int, default=READY_TIMEOUT, help='单任务最长等待秒')
    ap.add_argument('--only', default='')
    ap.add_argument('--dry', action='store_true')
    args = ap.parse_args()

    POOL = max(1, args.workers)
    EXCL_MB = args.excl_mb

    # 日志轮转（必须在任何 log() 之前，否则表头会被搬进 .prev）
    hist = LOGP
    if os.path.exists(hist):
        try:
            os.replace(hist, hist + '.prev')
        except Exception:
            pass

    man = json.load(open(MANIFEST, encoding='utf-8'))
    comps = man['companies']

    todo = [c for c in comps if c['pdf_exists']]
    if args.only:
        keys = [x.strip() for x in args.only.split(',') if x.strip()]
        todo = [c for c in todo if any(k in c['company'] for k in keys)]
    todo = order_todo(todo, args.order)
    if args.limit:
        todo = todo[:args.limit]

    try:
        build = json.loads(_get(BASE + '/api/health')).get('build')
    except Exception as e:
        build = 'unknown(%s)' % e

    log('=' * 78)
    log('批量提取 v2 启动 | BASE=%s | build=%s | 槽位=%d（≥%dMB 独占）| 待跑 %d 份'
        % (BASE, build, POOL, EXCL_MB, len(todo)))
    log('=' * 78)

    missing = [c for c in comps if not c['pdf_exists']]
    for c in missing:
        log('  ⚠ 跳过（无原始 PDF）: %s' % c['company'])

    if args.dry:
        for i, c in enumerate(todo, 1):
            mb = os.path.getsize(c['pdf']) / 1048576 if os.path.exists(c['pdf']) else 0
            log('  %3d. #%-3d [%s] %7.1fMB %-32s %s'
                % (i, c['no'], c['lang'], mb, c['company'][:32], c['pdf_file'][:40]))
        return

    os.makedirs(RAW, exist_ok=True)

    hb = threading.Thread(target=_heartbeat_loop, daemon=True)
    hb.start()

    stat = {'ok': 0, 'skip': 0, 'fail': 0}
    fails = []
    with ThreadPoolExecutor(max_workers=POOL) as ex:
        futs = {ex.submit(run_one, c, build,
                          _need_slots(os.path.getsize(c['pdf'])
                                      if os.path.exists(c['pdf']) else 0)): c
                for c in todo}
        for k, fu in enumerate(as_completed(futs), 1):
            kind, rec, el, extra = fu.result()
            stat[kind] += 1
            if kind == 'ok':
                sd = (extra.get('评分增强数据') or {})
                log('[%d/%d] OK  #%-3d %-30s %5.0fs 总分=%s 等级=%s'
                    % (k, len(todo), rec['no'], rec['company'][:28], el,
                       sd.get('总分'), sd.get('评分等级')))
            elif kind == 'skip':
                log('[%d/%d] 跳过（已有）#%-3d %s' % (k, len(todo), rec['no'], rec['company'][:28]))
            else:
                fails.append({'no': rec['no'], 'company': rec['company'],
                              'pdf': rec['pdf'], 'err': str(extra)[:300]})
                log('[%d/%d] ★失败 #%-3d %s -> %s'
                    % (k, len(todo), rec['no'], rec['company'][:28], str(extra)[:120]))

    _STOP.set()
    log('-' * 78)
    log('完成：成功 %d / 跳过 %d / 失败 %d（合计 %d）'
        % (stat['ok'], stat['skip'], stat['fail'], len(todo)))
    if fails:
        log('失败清单：')
        for f in fails:
            log('   #%d %s | %s' % (f['no'], f['company'], f['err'][:100]))
    try:
        with open(FAILP, 'w', encoding='utf-8') as f:
            json.dump(fails, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log('写失败清单异常: %s' % e)

    # 回填 build
    try:
        man['extract_build'] = build
        man['stats']['extracted_ok'] = stat['ok'] + stat['skip']
        man['stats']['extracted_fail'] = stat['fail']
        with open(MANIFEST, 'w', encoding='utf-8') as f:
            json.dump(man, f, ensure_ascii=False, indent=1)
        log('已回填 manifest.extract_build = %s' % build)
    except Exception as e:
        log('回填 manifest 失败: %s' % e)
    log('BATCH_DONE')


if __name__ == '__main__':
    main()
