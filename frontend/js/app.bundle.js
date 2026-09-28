/**
 * app.js — 主应用逻辑
 * 处理导航、上传、数据提取模拟、Tab切换、报告生成等交互
 */

// ============ 全局状态 ============
const App = {
    uploadedFiles: [],
    charts: {},
    analysisDone: false,
    compareChartType: 'bar' // 'bar' | 'radar'
};

// ============ 工具函数 ============
function $(selector) { return document.querySelector(selector); }
function $$(selector) { return document.querySelectorAll(selector); }

// HTML 转义（v=83：XSS 防护，所有 innerHTML 拼接的用户/LLM 数据必须先转义）
function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// fetch 超时包装（v=84：M1，LLM 卡死时明确报错，不无限转圈）
function fetchWithTimeout(url, options = {}, timeoutMs = 120000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    return fetch(url, { ...options, signal: controller.signal })
        .finally(() => clearTimeout(timer));
}

/**
 * v=93：统一解析后端响应，把后端返回的「中文原因」透出到界面
 *
 * 背景（bug 修复）：
 *   原先只抛 `提取服务响应异常: 400`，用户看到 400 完全不知道发生了什么。
 *   而后端其实回了明确原因，例如扫描件 PDF 会回：
 *     {"error": "提取文本过短，可能为扫描件PDF，建议用OCR"}
 *   该信息被前端丢弃 → 用户只能看到一个无意义的数字。
 *
 * @param {Response} res   fetch 响应
 * @param {string} label   业务名称（用于拼错误前缀，如「提取」「根因分析」）
 * @returns {Promise<any>} 成功则返回解析后的 JSON
 */
async function parseJsonResponse(res, label) {
    if (res.ok) return res.json();

    // 失败：尽最大努力从 body 里挖出人能看懂的原因
    let detail = '';
    try {
        const raw = await res.text();
        try {
            const j = JSON.parse(raw);
            detail = j.error || j.message || j.detail || j.msg || '';
        } catch (_) {
            // 非 JSON（如 nginx 的 HTML 错误页）→ 去掉标签取纯文本
            detail = String(raw || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
        }
    } catch (_) { /* body 读不出来就只报状态码 */ }

    detail = String(detail || '').trim().slice(0, 150);

    // 兜底：后端没给原因时，按状态码给一句人话
    if (!detail) {
        if (res.status === 413) detail = '文件超过服务端上传大小限制';
        // v=104：530 是 Cloudflare 在「隧道到边缘的连接断开」时返回的（不是业务错误）。
        //   实测重连约需 21s，客户端会自动重试；这里的文案要能解释「为什么在重试」，
        //   而不是甩一个裸的 HTTP 530 让用户以为系统坏了。
        else if (res.status === 530) detail = '边缘隧道连接中断（服务端正在自动重连），请稍后重试';
        else if (res.status === 502 || res.status === 503 || res.status === 504) detail = '后端服务无响应或超时，请稍后重试';
        else if (res.status === 524) detail = '边缘网关等待超时，请重试（长任务已改走异步接口）';
        else detail = `HTTP ${res.status}`;
    }

    const err = new Error(`${label}失败：${detail}`);
    // v=104：把状态码挂在 Error 上 —— 重试策略据此判断「值不值得重试」，
    //   不要再靠正则去匹配上面那些会随时改的提示文案（改文案就悄悄失效了）。
    err.status = res.status;
    throw err;
}

// ============ COS 直传（v=114）============
// 背景：存在 99.8MB 量级报告。原链路（浏览器 → CF 边缘 → 香港源站）实测 **3.0 Mbps**
//   ⇒ 100MB 需 **4.4 分钟**，不可接受。且 CF 给本站分配的边缘落点在阿姆斯特丹
//   （RTT 254ms），免费版无法改 IP 段 ⇒ 换 CDN 也救不了。
// 方案：浏览器直传 COS（国内就近接入，实测 22–23 Mbps ⇒ 99.8MB ≈ 37 秒）
//   → 服务器从 COS 取件（腾讯云内网/全球加速，实测 205–346 Mbps）⇒ 全链路约 40 秒。
// ⚠️ **完全可回落**：下面任一步失败（后端未启用 COS / 后端是旧版本没有该路由 /
//   预签名失败 / 直传失败 / 取件失败）都自动退回原 1MB 串行分片链路，**旧链路一字未改**。
// ⚠️ 预签名 URL **不得改写 host**：COS v5 签名**含 host**，换 host 必然 403
//   （网络是通的、0.1s 快失败，极易误判成"网络不通"）。URL 由后端签发，原样使用。
const COS_MIN_SIZE = 6 * 1024 * 1024;   // 起用直传的体积门槛（更小的走原链路，省一次取件往返）
const COS_MAX_RETRY = 3;                // 整包重试次数（分片级续传由原链路负责）
const COS_RETRY_BACKOFF_MS = [1500, 4000, 10000];

/** 用预签名 URL 一次 PUT 整个文件（进度来自 XHR 原生 upload 事件，真实字节级） */
function putFileToCos(file, url, onProgress) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('PUT', url, true);
        // ⚠️ 不设 Content-Type：服务端预签名**未签该 header**，让浏览器按 file.type
        //   自动带上即可。若签名里带了 Content-Type，前端就得逐字节对齐，太脆。
        xhr.timeout = 1800 * 1000;      // 30 分钟（与解析上限同量级的大余量）
        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable && typeof onProgress === 'function') {
                try { onProgress(e.loaded, e.total); } catch (_) { }
            }
        };
        xhr.onload = () => (xhr.status >= 200 && xhr.status < 300)
            ? resolve()
            : reject(new Error(`COS 直传失败（HTTP ${xhr.status}）`));
        xhr.onerror = () => reject(new Error('COS 直传网络错误（可能被跨域策略拦截）'));
        xhr.ontimeout = () => reject(new Error('COS 直传超时'));
        xhr.onabort = () => reject(new Error('COS 直传被取消'));
        xhr.send(file);
    });
}

/**
 * 走 COS 直传提交提取任务。
 * ⚠️ 契约：**任何失败都抛异常**，由 submitExtraction 统一捕获并回落原分片链路。
 *   因此本函数不需要自己兜底，保持"要么成功返回 task_id，要么抛错"的二值语义。
 *
 * @param {File} file
 * @param {Object} meta   {companyType, model}
 * @param {Function} report (cur, extra) —— 与分片链路同一个 2 参数契约
 *        cur/total 按「1MB 折算 1 片」上报：进度语义与分片链路一致，**UI 零改动**
 * @returns {Promise<string>} task_id
 */
async function submitExtractionViaCos(file, meta, report) {
    // 1) 申请预签名直传凭证（后端未启用 COS 时返回 ok:false ⇒ 属"正常回落"，不是故障）
    const prepRes = await fetchWithTimeout(`${API_CONFIG.EXTRACT_API}/cos_upload_prepare`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            filename: file.name, size: file.size,
            company_type: meta.companyType, model: meta.model
        })
    }, 30000);
    const prep = await parseJsonResponse(prepRes, '申请直传凭证');
    if (!prep || !prep.ok || !prep.url) {
        throw new Error((prep && prep.reason) || 'COS 直传未启用');
    }

    // 2) 直传本体
    const totalChunks = Math.ceil(file.size / UPLOAD_CHUNK_SIZE);
    const reportBytes = (loaded) => {
        const cur = Math.max(0, Math.min(totalChunks, Math.round(loaded / UPLOAD_CHUNK_SIZE)));
        report(cur, {});
    };
    reportBytes(0);

    let lastErr = null;
    for (let attempt = 1; attempt <= COS_MAX_RETRY; attempt++) {
        try {
            await putFileToCos(file, prep.url, reportBytes);
            lastErr = null;
            break;
        } catch (e) {
            lastErr = e;
            if (attempt >= COS_MAX_RETRY) break;
            const wait = COS_RETRY_BACKOFF_MS[Math.min(attempt - 1, COS_RETRY_BACKOFF_MS.length - 1)];
            // 复用分片链路的重试提示文案（failedIndex=0 只是占位，文案里不体现）
            report(0, {
                retry: true, attempt: attempt + 1, maxAttempt: COS_MAX_RETRY,
                waitMs: wait, failedIndex: 0
            });
            await new Promise(r => setTimeout(r, wait));
        }
    }
    if (lastErr) throw lastErr;

    // 3) 通知服务端从 COS 取件建任务（取件走腾讯云内网/加速，100MB 实测秒级）
    const doneRes = await fetchWithTimeout(`${API_CONFIG.EXTRACT_API}/cos_complete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            key: prep.key, filename: file.name, size: file.size,
            company_type: meta.companyType, model: meta.model
        })
    }, 900000);
    const done = await parseJsonResponse(doneRes, '完成直传');
    if (!done || !done.task_id) throw new Error('直传已完成但服务端未返回任务ID');
    return done.task_id;
}

// ============ 分片上传（v=96）============
// 背景：单次 POST 提交大文件的耗时 = 体积 / 上行带宽。
//   实测（2026-09-12，公网 60MB 分片实测）：真实上行仅 **139 KB/s**，
//   且抖动极大（单片 4MB 最快 ~15s、最慢 84.9s，相差 5.7 倍）。
//   ⚠️ 此前用「98.4MB 在 126.9s 断连」反推出的 449KB/s 是错的 ——
//      126.9s 是被 Cloudflare 掐断的时刻，不是上传完成的时刻，速度被高估约 3 倍。
//   → 真实 100s 边缘超时边界约 **14MB**（不是 44MB）；4MB 的大分片在抖动时
//     会逼近甚至撞上超时线，故 v=96 定稿为「1MB 小分片」：
//     单片 1MB 在 139KB/s 下约 7.5s，最坏抖动按 3 倍算也仅 ~22s，安全余量 ~4.5 倍。
const UPLOAD_CHUNK_SIZE = 1 * 1024 * 1024;        // 单片 1MB（抗抖动，最坏 ~22s ≪ 100s 边缘超时）
const UPLOAD_CHUNK_THRESHOLD = 6 * 1024 * 1024;   // 超过 6MB 走分片（6MB 一次性提交需 ~44s，已接近危险区）
const UPLOAD_MAX_CHUNKS = 400;                    // 与后端上限一致（400MB 覆盖上限）
// v=104：重试策略按「隧道重连窗口」重定
//   2026-09-16 实测：cloudflared 到 Cloudflare 边缘的连接被掐断后，约 21s 才重新注册
//   （日志 `Lost connection with the edge` → 21s 后 `Registered tunnel connection`）。
//   断线期间的所有请求在边缘就被判 530，**根本不进 nginx**（nginx 侧只留下一条被
//   切断的 `POST /api/upload_chunk 400 0`）。原配置「3 次重试 + 1s/2s 退避」合计
//   仅 3s，远短于 21s 重连窗口 → 必然全军覆没，用户看到的就是「切片上传报错」。
//   现改为 6 次、退避 2/4/8/15/30s（累计 ~59s，覆盖一次完整重连仍有余量）。
const UPLOAD_CHUNK_RETRY = 6;
const UPLOAD_RETRY_BACKOFF_MS = [2000, 4000, 8000, 15000, 30000];
// 边缘/网关瞬断类错误才值得重试：
//   530 = 隧道边缘连接断（本次故障）｜502/503/504/524 = 网关侧无响应/超时
//   408/425/429 = 请求超时/过早/限流（限流等一会儿再来也行）
const UPLOAD_RETRYABLE_RE = /HTTP\s*(408|425|429|5\d\d)|超时|中断|网络|Failed to fetch|NetworkError|Load failed|timeout/i;

/** 该错误是否属于「等一会儿重试就能好」的瞬断类（v=104） */
function isRetryableUploadError(err) {
    if (!err) return false;
    if (err.name === 'AbortError') return true;   // fetchWithTimeout 主动超时掐断
    // 优先看状态码（parseJsonResponse 会挂 err.status）—— 比匹配中文提示文案稳
    const st = Number(err.status || 0);
    if (st) return st === 408 || st === 425 || st === 429 || st >= 500;
    // 无状态码 = 网络层就失败了（断网/DNS/连接被重置）→ 按文案兜底判断
    return UPLOAD_RETRYABLE_RE.test(String(err.message || err));
}

/** 生成 32 位十六进制上传 ID（与后端白名单正则 ^[0-9a-f]{32}$ 一致） */
function genUploadId() {
    const bytes = new Uint8Array(16);
    const c = window.crypto || window.msCrypto;
    if (c && c.getRandomValues) {
        c.getRandomValues(bytes);
    } else {
        for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
    }
    return Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('');
}

/**
 * 大文件分片上传：逐片提交 → 合并 → 返回 task_id
 *
 * 逐片串行上传（并行只会争抢本就有限的上行带宽，反而更容易触发超时）。
 *
 * v=104 变更（对应 2026-09-16「切片上传报错」故障）：
 *   ① 断点续传：带 opts.uploadId 时先查 /api/upload_status，跳过服务端已有的片；
 *   ② 重试策略覆盖隧道重连窗口（6 次 / 最长 59s，见 UPLOAD_CHUNK_RETRY 注释）；
 *   ③ 非瞬断类错误（400 参数错、413 超限）不再无谓重试，直接失败；
 *   ④ 失败时**不再删除服务端分片**（原逻辑删光 → 每次断线都要从第 1 片重来）。
 *
 * @param {File} file   待上传文件
 * @param {Object} meta {companyType, model}
 * @param {Function} onUploadProgress (doneChunks, info)
 *        info: {uploadId, skipped, total, retry, attempt, maxAttempt, waitMs, failedIndex}
 *        ⚠️ info 是**第二个参数**（不是第三个）；历史上一处三参数调用导致字段被静默丢弃。
 * @param {Object} [opts] {uploadId} 传入则断点续传，不传则新建会话
 * @returns {Promise<string>} task_id
 */
async function uploadFileInChunks(file, meta, onUploadProgress, opts) {
    const options = opts || {};
    const uploadId = options.uploadId || genUploadId();
    const totalChunks = Math.ceil(file.size / UPLOAD_CHUNK_SIZE);
    if (totalChunks > UPLOAD_MAX_CHUNKS) {
        throw new Error(`文件过大（${formatFileSize(file.size)}），超过分片上传上限（${UPLOAD_MAX_CHUNKS * 4}MB）`);
    }

    // v=104：先问服务端「哪些片已经落盘了」→ 只补缺片
    let already = new Set();
    if (options.uploadId) {
        try {
            const stRes = await fetchWithTimeout(
                `${API_CONFIG.EXTRACT_API}/upload_status?upload_id=${uploadId}`, {}, 30000
            );
            const st = await parseJsonResponse(stRes, '查询续传位置');
            (st && Array.isArray(st.chunks) ? st.chunks : []).forEach(idx => already.add(idx));
        } catch (e) {
            // 查不到续传点（如服务端会话已被 TTL 回收）→ 当作全新上传，不影响主流程
            console.warn('续传位置查询失败，按整包重传处理:', e && e.message);
            already = new Set();
        }
    }

    const report = (cur, extra) => {
        if (typeof onUploadProgress === 'function') {
            try {
                // ⚠️ 契约：第二个参数就是 info 对象，**不要再单独传 totalChunks**。
                //   2026-09-16 修复（探针实测发现的静默 bug）：原先这里写成
                //   `onUploadProgress(cur, totalChunks, {...})`，而上一层 submitExtraction
                //   的 report 签名是 `(cur, extra)` → extra 实际收到的是数字 totalChunks，
                //   `Object.assign(target, 7)` 不产生任何键 → uploadId / skipped 被**静默吞掉**
                //   （不报错、不抛异常，只是永远读不到），导致「已续传 N 片」提示不显示、
                //   续传凭据也记不住。参数个数不匹配是 JS 里最典型的静默失败。
                onUploadProgress(cur, Object.assign(
                    { uploadId, skipped: already.size, total: totalChunks }, extra || {}));
            } catch (_) { /* 渲染异常绝不影响上传本身 */ }
        }
    };
    report(already.size);

    // 待传片 = 全部 - 已落盘（已落盘的片不再传，避免重复浪费本就很窄的上行）
    const pending = [];
    for (let i = 0; i < totalChunks; i++) {
        if (!already.has(i)) pending.push(i);
    }

    try {
        let done = already.size;
        for (const i of pending) {
            const start = i * UPLOAD_CHUNK_SIZE;
            const blob = file.slice(start, Math.min(file.size, start + UPLOAD_CHUNK_SIZE));

            // 单片失败重试（后端同 chunk_index 为覆盖写 → 重试天然幂等，不会重复拼接）
            let lastErr = null;
            for (let attempt = 1; attempt <= UPLOAD_CHUNK_RETRY; attempt++) {
                try {
                    const fd = new FormData();
                    fd.append('upload_id', uploadId);
                    fd.append('chunk_index', String(i));
                    fd.append('file', blob, file.name);
                    const res = await fetchWithTimeout(
                        `${API_CONFIG.EXTRACT_API}/upload_chunk`, { method: 'POST', body: fd }, 120000
                    );
                    await parseJsonResponse(res, `上传第 ${i + 1}/${totalChunks} 片`);
                    lastErr = null;
                    break;
                } catch (e) {
                    lastErr = e;
                    if (attempt >= UPLOAD_CHUNK_RETRY) break;

                    // 参数/体积类错误重试无意义，立即上抛，别让用户白等 59s
                    if (!isRetryableUploadError(e)) break;

                    const wait = UPLOAD_RETRY_BACKOFF_MS[
                        Math.min(attempt - 1, UPLOAD_RETRY_BACKOFF_MS.length - 1)
                    ];
                    // 让界面显示「重试中」，用户才知道程序在等隧道重连而不是卡死
                    report(done, {
                        retry: true, attempt: attempt + 1, maxAttempt: UPLOAD_CHUNK_RETRY,
                        waitMs: wait, failedIndex: i
                    });
                    await new Promise(r => setTimeout(r, wait));
                }
            }
            if (lastErr) throw lastErr;

            done++;
            report(done);
        }

        const res = await fetchWithTimeout(`${API_CONFIG.EXTRACT_API}/upload_complete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                upload_id: uploadId,
                total_chunks: totalChunks,
                filename: file.name,
                company_type: meta && meta.companyType ? meta.companyType : 'listed',
                model: meta && meta.model ? meta.model : ''
            })
        }, 180000);
        const data = await parseJsonResponse(res, '合并分片');
        if (!data || !data.task_id) throw new Error('合并分片失败：服务端未返回任务ID');
        return data.task_id;
    } catch (err) {
        // v=104：⚠️ 这里**故意不调用 /api/upload_abort**
        //   原逻辑：任一片重试耗尽 → 删掉服务端全部已落盘分片 → 用户只能从第 1 片重来。
        //   隧道每约 2 小时断一次（实测 9h 内 2 次），100MB 报告 = 100 片，
        //   于是「永远传不完」。
        //   现在保留分片（服务端有 1 小时 TTL 自动回收），重试时凭同一 upload_id 续传；
        //   只有用户显式删除文件时才清理（见 deleteFile）。
        const wrapped = (err && (err.name === 'AbortError' || /aborted|failed to fetch|network/i.test(err.message || '')))
            ? new Error('分片上传中断或超时（网络较慢），请检查网络后重试')
            : err;
        // 挂上续传上下文 → 上层存入 fileObj，重试时直接续传，无需重新分片
        wrapped.uploadId = uploadId;
        wrapped.resumeFrom = already.size;
        throw wrapped;
    }
}

/**
 * 提交提取任务：按文件大小自动选择「分片上传」或「一次性提交」，统一返回 task_id
 * @param {FormData} formData 含 file / company_type / model
 * @param {Function} onProgress 进度回调
 * @returns {Promise<string>} task_id
 */
async function submitExtraction(formData, onProgress) {
    const fileObj = formData.get('file');
    const size = (fileObj && typeof fileObj.size === 'number') ? fileObj.size : 0;
    const companyType = formData.get('company_type') || 'listed';
    const model = formData.get('model') || '';

    // v=114：大文件优先走 COS 直传（后端签发预签名 URL → 浏览器直传对象存储）
    //   ⚠️ 失败**不报错**，只打日志后继续走下面的原分片链路 —— 用户侧无感，站点不会挂。
    //   这条 try/catch 是"新旧链路"的唯一交界点，保持原链路一字未改。
    if (size >= COS_MIN_SIZE) {
        const cosTotal = Math.ceil(size / UPLOAD_CHUNK_SIZE);
        const reportCos = (cur, extra) => {
            if (typeof onProgress === 'function') {
                try {
                    onProgress(Object.assign(
                        { status: 'uploading', stage: 'upload', current: cur, total: cosTotal },
                        extra || {}
                    ));
                } catch (_) { }
            }
        };
        try {
            const cosTaskId = await submitExtractionViaCos(
                fileObj, { companyType, model }, reportCos
            );
            if (cosTaskId) return cosTaskId;
        } catch (e) {
            console.warn('COS 直传不可用，自动回落原分片链路:', (e && e.message) || e);
        }
    }

    // 大文件 → 分片上传（每片请求都很小，不会被边缘超时掐断）
    if (size > UPLOAD_CHUNK_THRESHOLD) {
        const totalChunks = Math.ceil(size / UPLOAD_CHUNK_SIZE);
        const report = (cur, extra) => {
            if (typeof onProgress === 'function') {
                try {
                    onProgress(Object.assign(
                        { status: 'uploading', stage: 'upload', current: cur, total: totalChunks },
                        extra || {}
                    ));
                } catch (_) { }
            }
        };
        report(0);
        return await uploadFileInChunks(fileObj, {
            companyType: companyType,
            model: model
        }, report, {
            // v=104：fileObj.uploadId 存在 → 断点续传（失败重试/手动重传都走这里）
            uploadId: (fileObj && fileObj.uploadId) || ''
        });
    }

    // 小文件 → 保持原有一次性提交逻辑不变
    const submitRes = await fetchWithTimeout(
        `${API_CONFIG.EXTRACT_API}/extract_async`, { method: 'POST', body: formData }, 600000
    );
    const submitData = await parseJsonResponse(submitRes, '提交提取任务');
    const taskId = submitData && submitData.task_id;
    if (!taskId) throw new Error('提交提取任务失败：服务端未返回任务ID');
    return taskId;
}

/**
 * 异步提取：提交任务 → 轮询进度 → 取回结果（v=94，v=96 扩展分片上传）
 *
 * 背景：扫描件触发 OCR 兜底时约需 355s，而 Cloudflare 免费版边缘等待约 100s
 *       就返回 524（实测 127.9s 触发），同步长请求必然失败。
 *       改为「提交即返回 + 轮询」后，每个 HTTP 请求都远短于边缘超时。
 *       v=96：大文件（>10MB）的「提交」一步本身也会超时，故改为分片上传后再合并。
 *
 * @param {FormData} formData   含 file / model / company_type
 * @param {Function} onProgress 可选回调 ({status, stage, current, total})
 * @returns {Promise<Object>}   提取结果（与旧的同步接口返回体完全一致）
 */
async function requestExtraction(formData, onProgress) {
    // 步骤1：提交任务（大文件走分片上传，小文件一次提交；都只做接收+落盘，不等解析）
    const taskId = await submitExtraction(formData, onProgress);

    // 步骤2：轮询（每 2s 一次，单次响应极小；总上限 30 分钟防死循环）
    const deadline = Date.now() + 1800000;
    while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 2000));
        const stRes = await fetchWithTimeout(
            `${API_CONFIG.EXTRACT_API}/extract_status/${taskId}`, {}, 60000
        );
        const st = await parseJsonResponse(stRes, '查询提取进度');
        if (typeof onProgress === 'function') {
            try { onProgress(st); } catch (_) { /* 渲染异常绝不影响主流程 */ }
        }
        if (st.status === 'done') return st.result;
        if (st.status === 'error') throw new Error(st.error || '提取失败');
    }
    throw new Error('提取超时（超过30分钟），请改用文字版报告或减少页数');
}

function scrollToSection(id) {
    const el = document.getElementById(id);
    if (el) {
        const navHeight = 64;
        const top = el.getBoundingClientRect().top + window.pageYOffset - navHeight;
        window.scrollTo({ top, behavior: 'smooth' });
    }
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
}

function showToast(message, type = 'info') {
    const container = $('#toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;

    const icons = {
        success: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>',
        error: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
        info: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12" y2="16"/></svg>'
    };

    toast.innerHTML = `
        <span class="toast-icon">${icons[type] || icons.info}</span>
        <span>${message}</span>
    `;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.animation = 'slideInRight 0.3s ease reverse';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

function openModal(id) {
    $(`#${id}`).classList.add('active');
    document.body.style.overflow = 'hidden';
}
function closeModal(id) {
    $(`#${id}`).classList.remove('active');
    document.body.style.overflow = '';
}

// ============ 使用须知公告（v=115） ============
// 频率：每会话一次（sessionStorage）+ 勾选「不再提示」后永久静默（localStorage）。
// storage 在隐私模式下可能抛异常 —— 一律吃掉异常，宁可多弹一次也不能让首屏 JS 崩掉。
const NOTICE_SESSION_KEY = 'esg.noticeSeen';
const NOTICE_MUTE_KEY = 'esg.noticeMuted';

function maybeShowNotice() {
    try {
        if (localStorage.getItem(NOTICE_MUTE_KEY) === '1') return;
        if (sessionStorage.getItem(NOTICE_SESSION_KEY) === '1') return;
    } catch (_) { /* 忽略 */ }
    // 延迟一拍弹，避免和首屏图表初始化抢帧造成抖动
    setTimeout(() => {
        const box = $('#noticeMuteCheck');
        if (box) box.checked = false;
        openModal('noticeModal');
    }, 400);
}

function closeNotice() {
    try {
        const box = $('#noticeMuteCheck');
        if (box && box.checked) localStorage.setItem(NOTICE_MUTE_KEY, '1');
        sessionStorage.setItem(NOTICE_SESSION_KEY, '1');
    } catch (_) { /* 忽略 */ }
    closeModal('noticeModal');
}

// ============ Supabase 认证 + 历史记录（v=70） ============
let supaClient = null;
let authMode = 'login'; // 'login' | 'register'

function initSupabase() {
    if (typeof window.supabase === 'undefined' || !API_CONFIG.SUPABASE_URL) {
        console.warn('Supabase 未加载或未配置，登录功能不可用');
        return;
    }
    supaClient = window.supabase.createClient(API_CONFIG.SUPABASE_URL, API_CONFIG.SUPABASE_ANON_KEY);
    supaClient.auth.onAuthStateChange((event) => {
        updateAuthUI();
        if (event === 'SIGNED_IN') {
            // v=115：若这次登录是被「提取需登录」门禁弹出来的，登录后明确告诉用户可以继续了，
            //   否则用户只看到"登录成功"，不知道刚才被拦下的动作现在能做了。
            if (App._loginPromptedFor) {
                showToast(`已登录，可以继续${App._loginPromptedFor}了`, 'success');
                App._loginPromptedFor = null;
            } else {
                showToast('登录成功', 'success');
            }
        }
        if (event === 'SIGNED_OUT') {
            App._loginPromptedFor = null;
            showToast('已退出登录', 'info');
        }
    });
    updateAuthUI();
}

async function getCurrentUser() {
    if (!supaClient) return null;
    try {
        const { data } = await supaClient.auth.getUser();
        return data.user || null;
    } catch (e) { return null; }
}

/**
 * v=115：登录门禁 —— 数据提取功能必须登录后才能使用
 *
 * 为什么拦在「选文件」而不是「开始数据提取」：
 *   本系统是选中文件即调提取接口（uploadToExtractService），
 *   拦在 startAnalysis() 上等于让用户白传一次几十上百 MB 的文件。
 *
 * 兜底原则（不破原链路）：
 *   supaClient 未就绪（Supabase 组件没加载 / 被网络阻断）时**放行**并打警告。
 *   鉴权组件抽风绝不能把整个提取功能一起拖死。
 *
 * @param {string} where 触发场景，仅用于记录与文案
 * @returns {Promise<boolean>} true=可继续；false=已拦下并弹出登录
 */
async function requireLogin(where) {
    if (!supaClient) {
        console.warn('[v115] Supabase 未就绪，登录门禁兜底放行：', where);
        return true;
    }
    const user = await getCurrentUser();
    if (user) return true;
    App._loginPromptedFor = where || '提取';
    showToast('数据提取需先登录，请登录或注册后再上传报告', 'error');
    openAuthModal();
    return false;
}

/**
 * v=115：上传区未登录态视觉
 * 刻意不做成「禁用」——禁用会让用户找不到路，这里给一个明确的登录入口。
 * @param {object} [userArg] 已知的当前用户（updateAuthUI 已经查过，避免重复请求）
 */
async function refreshUploadLockUI(userArg) {
    const zone = $('#uploadZone');
    const hint = $('#uploadLoginHint');
    if (!zone && !hint) return;
    let logged = true;   // 兜底：鉴权不可用时不算锁定态
    if (supaClient) {
        const u = (typeof userArg !== 'undefined') ? userArg : await getCurrentUser();
        logged = !!u;
    }
    const locked = !logged;
    if (zone) zone.classList.toggle('is-locked', locked);
    if (hint) hint.style.display = locked ? 'block' : 'none';
}

function openAuthModal() {
    authMode = 'login';
    $('#authModalTitle').textContent = '登录';
    $('#authSubmitBtn').textContent = '登录';
    $('#authSwitchText').textContent = '还没有账号？';
    $('#authSwitchLink').textContent = '注册';
    $('#authNicknameGroup').style.display = 'none';
    $('#authError').style.display = 'none';
    openModal('authModal');
}

function toggleAuthMode() {
    authMode = authMode === 'login' ? 'register' : 'login';
    const isRegister = authMode === 'register';
    $('#authModalTitle').textContent = isRegister ? '注册' : '登录';
    $('#authSubmitBtn').textContent = isRegister ? '注册并登录' : '登录';
    $('#authSwitchText').textContent = isRegister ? '已有账号？' : '还没有账号？';
    $('#authSwitchLink').textContent = isRegister ? '登录' : '注册';
    $('#authNicknameGroup').style.display = isRegister ? 'block' : 'none';
    $('#authError').style.display = 'none';
}

async function handleAuthSubmit() {
    const email = $('#authEmail').value.trim();
    const password = $('#authPassword').value;
    const nickname = $('#authNickname').value.trim();
    const errEl = $('#authError');
    errEl.style.display = 'none';
    if (!email || !password) {
        errEl.textContent = '请填写邮箱和密码';
        errEl.style.display = 'block';
        return;
    }
    try {
        if (authMode === 'register') {
            if (!nickname) { errEl.textContent = '请填写昵称'; errEl.style.display = 'block'; return; }
            const { data, error } = await supaClient.auth.signUp({
                email, password,
                options: { data: { username: nickname } }
            });
            if (error) throw error;
            // 创建 profiles 行（RLS 允许本人 insert；失败不阻塞主流程）
            if (data.user) {
                try {
                    const r = await supaClient.from('profiles').insert({ id: data.user.id, username: nickname });
                    if (r.error && !String(r.error.code).startsWith('23')) console.warn('profile 创建:', r.error.message);
                } catch (pe) { console.warn('profile 创建异常:', pe); }
            }
            showToast('注册成功！请用邮箱密码登录（如果需要邮箱确认，请跑 SQL 强制确认）', 'success');
        } else {
            const { error } = await supaClient.auth.signInWithPassword({ email, password });
            if (error) throw error;
        }
        closeModal('authModal');
        $('#authPassword').value = '';
    } catch (e) {
        errEl.textContent = e.message || '操作失败';
        errEl.style.display = 'block';
    }
}

async function handleLogout() {
    await supaClient.auth.signOut();
    $('#history').style.display = 'none';
}

async function updateAuthUI() {
    const user = await getCurrentUser();
    const btn = $('#navAuthBtn');
    const info = $('#navUserInfo');
    const histLink = $('#navHistoryLink');
    const menuWrap = $('#userMenuWrap');
    if (user) {
        btn.textContent = '退出';
        btn.onclick = handleLogout;
        let nickname = (user.user_metadata && user.user_metadata.username) || user.email || '用户';
        try {
            const { data } = await supaClient.from('profiles').select('username').eq('id', user.id).maybeSingle();
            if (data && data.username) nickname = data.username;
        } catch (e) { /* profiles 未建时忽略 */ }
        info.style.display = 'inline';
        info.textContent = `👤 ${nickname} ▾`;
        histLink.style.display = '';
        menuWrap.style.display = 'block';
        // v=115：登录态同步上传区（解锁 + 隐藏"请先登录"提示）
        refreshUploadLockUI(user);
        // 刷新下拉卡片内容（含历史条数）
        loadUserMenu(user, nickname);
    } else {
        btn.textContent = '登录';
        btn.onclick = openAuthModal;
        info.style.display = 'none';
        info.textContent = '';
        histLink.style.display = 'none';
        menuWrap.style.display = 'none';
        closeUserMenu();
        // v=115：未登录态 → 上传区上锁
        refreshUploadLockUI(null);
    }
}

// ---------- 用户下拉卡片（v=75） ----------
let _userMenuLoaded = false;

async function toggleUserMenu() {
    const menu = $('#userMenu');
    const willShow = menu.style.display === 'none';
    if (willShow) {
        await loadUserMenu();
        menu.style.display = 'block';
        _userMenuLoaded = true;
    } else {
        menu.style.display = 'none';
    }
}

function closeUserMenu() {
    const menu = $('#userMenu');
    if (menu) menu.style.display = 'none';
    hideNicknameEditor();
    hidePasswordEditor();
}

async function loadUserMenu(user, nickname) {
    if (!user) user = await getCurrentUser();
    if (!user) return;
    if (!nickname) {
        nickname = (user.user_metadata && user.user_metadata.username) || user.email || '用户';
        try {
            const { data } = await supaClient.from('profiles').select('username').eq('id', user.id).maybeSingle();
            if (data && data.username) nickname = data.username;
        } catch (e) { /* ignore */ }
    }
    // 头像圈：昵称首字符
    const avatar = $('#userAvatar');
    if (avatar) avatar.textContent = (nickname || '?').charAt(0);
    // 昵称 / 邮箱 / 注册时间（到年月）
    const nameEl = $('#userMenuName');
    if (nameEl) nameEl.textContent = nickname;
    const emailEl = $('#userMenuEmail');
    if (emailEl) emailEl.textContent = user.email || '—';
    const joinedEl = $('#userMenuJoined');
    if (joinedEl && user.created_at) {
        const d = new Date(user.created_at);
        joinedEl.textContent = `注册于 ${d.getFullYear()}年${d.getMonth() + 1}月`;
    }
    // 历史条数
    try {
        const { count, error } = await supaClient
            .from('history')
            .select('id', { count: 'exact', head: true });
        const cntEl = $('#userMenuHistoryCount');
        if (cntEl && !error) cntEl.textContent = count || 0;
    } catch (e) { /* ignore */ }
}

// ---------- 修改昵称 ----------
function showNicknameEditor() {
    hidePasswordEditor();
    const ed = $('#nicknameEditor');
    const input = $('#nicknameInput');
    const name = $('#userMenuName');
    if (input && name) input.value = name.textContent === '—' ? '' : name.textContent;
    if (ed) ed.style.display = 'block';
}

function hideNicknameEditor() {
    const ed = $('#nicknameEditor');
    if (ed) ed.style.display = 'none';
}

async function saveNickname() {
    const input = $('#nicknameInput');
    const newName = input ? input.value.trim() : '';
    if (!newName) { showToast('昵称不能为空', 'error'); return; }
    try {
        const user = await getCurrentUser();
        if (!user) return;
        const { error } = await supaClient.from('profiles')
            .update({ username: newName })
            .eq('id', user.id);
        if (error) throw error;
        showToast('昵称已更新', 'success');
        hideNicknameEditor();
        // 刷新导航栏 + 卡片
        await updateAuthUI();
        closeUserMenu();
        // 重新打开时显示新昵称
        const menu = $('#userMenu');
        if (menu) menu.style.display = 'block';
    } catch (e) {
        showToast('昵称更新失败: ' + (e.message || '未知错误'), 'error');
    }
}

// ---------- 修改密码 ----------
function showPasswordEditor() {
    hideNicknameEditor();
    const ed = $('#passwordEditor');
    if (ed) ed.style.display = 'block';
}

function hidePasswordEditor() {
    const ed = $('#passwordEditor');
    if (ed) ed.style.display = 'none';
}

async function savePassword() {
    const p1 = $('#newPasswordInput');
    const p2 = $('#confirmPasswordInput');
    console.log('[savePassword] p1=', p1, 'p2=', p2, 'p1.value=', p1 && p1.value, 'p2.value=', p2 && p2.value);
    if (!p1 || !p2) {
        showToast('密码输入框未找到，请刷新页面', 'error');
        return;
    }
    const v1 = (p1.value || '').trim();
    const v2 = (p2.value || '').trim();
    if (v1.length === 0) { showToast('请输入新密码', 'error'); p1.focus(); return; }
    if (v1.length < 6) { showToast('密码至少 6 位（当前 ' + v1.length + ' 位）', 'error'); p1.focus(); return; }
    if (v2.length === 0) { showToast('请确认新密码', 'error'); p2.focus(); return; }
    if (v1 !== v2) { showToast('两次输入不一致', 'error'); p2.focus(); return; }
    try {
        const { error } = await supaClient.auth.updateUser({ password: v1 });
        if (error) throw error;
        showToast('密码已修改，下次请用新密码登录', 'success');
        hidePasswordEditor();
        p1.value = '';
        p2.value = '';
    } catch (e) {
        showToast('密码修改失败: ' + (e.message || '未知错误'), 'error');
    }
}

// 点击外部区域 / Esc 收起下拉
document.addEventListener('click', (e) => {
    const wrap = $('#userMenuWrap');
    const menu = $('#userMenu');
    if (!wrap || !menu || menu.style.display === 'none') return;
    if (!wrap.contains(e.target)) closeUserMenu();
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeUserMenu();
});

// ---------- 历史记录 ----------
async function saveHistoryToSupabase() {
    if (!supaClient) return;
    try {
        const user = await getCurrentUser();
        if (!user) return; // 游客不保存
        const ext = (App.extractResult && App.extractResult.extracted) || App.extractResult || {};
        const meta = ext['报告元数据'] || {};
        const sd = ext['评分增强数据'] || {};
        // v=80：同时保存当时的 ADP/DeepSeek 建议（让历史查看能完整恢复）
        const suggestionObj = (App.scoreResult && App.scoreResult.评分结果) || App.scoreResult || null;
        const scoreDataForRestore = App._reportScoreData || null;
        const rawTextForRestore = App._adpRawText || (App.scoreResult && App.scoreResult.raw_text) || '';
        const { error } = await supaClient.from('history').insert({
            user_id: user.id,                // v=79：RLS policy 要求 user_id = auth.uid()，必须显式传
            company_name: meta['企业名称'] || '未命名企业',
            industry: meta['所属行业'] || '',
            report_year: meta['报告年份'] || '',
            total_score: sd['总分'] || 0,
            grade: sd['评分等级'] || '',
            result_json: {
                extracted: ext,
                scoreData: scoreDataForRestore,
                suggestion: suggestionObj,
                rawText: rawTextForRestore
            }
        });
        if (error) console.warn('历史保存失败:', error.message);
        else showToast('分析记录已保存', 'success');
    } catch (e) {
        console.warn('历史保存异常:', e);
    }
}

function openHistoryPage() {
    $('#history').style.display = 'block';
    scrollToSection('history');
    refreshHistory();
}

async function refreshHistory() {
    if (!supaClient) return;
    const listEl = $('#historyList');
    listEl.innerHTML = '<div class="history-loading">加载中...</div>';
    const { data, error } = await supaClient
        .from('history')
        .select('*')
        .order('created_at', { ascending: false });
    if (error) {
        listEl.innerHTML = `<div class="history-empty" style="display:block;">加载失败：${error.message}</div>`;
        return;
    }
    if (!data || data.length === 0) {
        listEl.innerHTML = '<div class="history-empty" style="display:block;">暂无分析记录 —— 上传报告并完成分析后，结果会自动保存到这里</div>';
        return;
    }
    listEl.innerHTML = data.map(h => `
        <div class="history-card">
            <div class="history-card-main" style="cursor:pointer;" onclick="openHistoryDetail('${h.id}')">
                <div class="history-card-name">${escapeHtml(h.company_name) || '未命名企业'}</div>
                <div class="history-card-meta">${escapeHtml(h.report_year) || '—'} 年度 · ${escapeHtml(h.industry) || '—'} · ${formatTime(h.created_at)}</div>
            </div>
            <div class="history-card-score">
                <div class="history-card-grade">${escapeHtml(h.grade) || '—'}</div>
                <div class="history-card-score-num">${h.total_score != null ? Number(h.total_score).toFixed(2) : '—'}</div>
                <div class="history-card-score-label">综合评分</div>
            </div>
            <div class="history-card-actions">
                <button class="btn btn-outline" onclick="openHistoryDetail('${h.id}')">查看</button>
                <button class="btn btn-outline" onclick="deleteHistory('${h.id}')" style="color:#dc2626;">删除</button>
            </div>
        </div>
    `).join('');
}

function formatTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function openHistoryDetail(id) {
    if (!supaClient) return;
    const { data, error } = await supaClient.from('history').select('*').eq('id', id).single();
    if (error || !data) { showToast('记录加载失败', 'error'); return; }
    // 设置全局数据复用 previewReport（不触发重复保存）
    // v=80：result_json 现在是 {extracted, scoreData, suggestion, rawText} 包裹结构
    const rj = data.result_json || {};
    const ext = rj.extracted || (data.result_json && data.result_json['报告元数据'] ? data.result_json : {});
    App.extractResult = { extracted: ext };
    // 恢复当时的评分/建议数据 → previewReport 能展示当时的完整建议（而不是模板 fallback）
    if (rj.scoreData) App._reportScoreData = rj.scoreData;
    if (rj.suggestion) {
        App.scoreResult = { 评分结果: rj.suggestion };
        if (rj.rawText) App.scoreResult.raw_text = rj.rawText;
    } else {
        App.scoreResult = null;
    }
    App._adpRawText = rj.rawText || '';  // 给 previewReport/PDF 用
    App.analysisDone = true;
    App._historySaveToken = null; // 历史详情不重复保存
    previewReport();
}

async function deleteHistory(id) {
    if (!confirm('确定删除这条分析记录吗？')) return;
    const { error } = await supaClient.from('history').delete().eq('id', id);
    if (error) showToast('删除失败: ' + error.message, 'error');
    else { showToast('已删除', 'success'); refreshHistory(); }
}

// ============ 导航栏 ============
function initNavbar() {
    const navbar = $('#navbar');
    const navLinks = $$('.nav-link');

    window.addEventListener('scroll', () => {
        if (window.scrollY > 10) {
            navbar.classList.add('scrolled');
        } else {
            navbar.classList.remove('scrolled');
        }

        // 高亮当前section
        const sections = ['hero', 'upload', 'analysis', 'scoring', 'finance'];
        let current = 'hero';
        for (const id of sections) {
            const el = document.getElementById(id);
            if (el && el.style.display !== 'none') {
                const rect = el.getBoundingClientRect();
                if (rect.top <= 100) current = id;
            }
        }
        navLinks.forEach(link => {
            link.classList.toggle('active', link.dataset.section === current);
        });
    });

    // 平滑滚动
    navLinks.forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            const target = link.dataset.section;
            const el = document.getElementById(target);
            if (el && el.style.display !== 'none') {
                scrollToSection(target);
            } else if (el) {
                el.style.display = '';
                setTimeout(() => scrollToSection(target), 50);
            }
        });
    });
}

// ============ 上传模块 ============
function initUpload() {
    const zone = $('#uploadZone');
    const fileInput = $('#fileInput');

    // 点击上传
    zone.addEventListener('click', (e) => {
        // v=115：点「请先登录」里的链接就别再往下走（那个链接自己会开登录弹窗）
        if (e.target.closest && e.target.closest('#uploadLoginHint')) return;

        // v=115 登录门禁（最前一道）：未登录时连文件选择器都不打开。
        //   让用户白挑一次文件、等选完才告诉他"要登录"，体验很差。
        //   这里必须同步判断 —— requireLogin() 是异步的，来不及拦 click 的默认行为，
        //   故读 refreshUploadLockUI() 维护的 .is-locked 类。
        if (zone.classList.contains('is-locked')) {
            e.preventDefault();
            e.stopPropagation();
            requireLogin('上传提取');
            return;
        }

        if (e.target.tagName !== 'LABEL') fileInput.click();
    });

    // 拖拽
    zone.addEventListener('dragover', (e) => {
        e.preventDefault();
        zone.classList.add('dragover');
    });
    zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
    zone.addEventListener('drop', (e) => {
        e.preventDefault();
        zone.classList.remove('dragover');
        handleFiles(e.dataTransfer.files);
    });

    // 文件选择
    fileInput.addEventListener('change', (e) => {
        handleFiles(e.target.files);
        fileInput.value = '';
    });
}

async function handleFiles(fileList) {
    // ---- v=115 门禁①：数据提取必须登录后才能使用 ----
    // 本系统「选中文件即调提取接口」，所以拦截点必须在这里；
    // 若拦在 startAnalysis()，用户已经白传了整份几十上百 MB 的文件。
    if (!await requireLogin('上传提取')) return;

    // ---- v=115 门禁②：一次一份 ----
    // 已存在任何一条记录（上传中 / 上传失败 / 已完成）都拒收：
    // 失败文件在服务端同样留下了 uploadId 分片目录，不算占位就等于留了个绕过口子。
    if (App.uploadedFiles.length > 0) {
        showToast('当前每次仅支持 1 份报告，请先删除已上传的报告再上传新的', 'error');
        return;
    }

    const validTypes = ['.pdf', '.doc', '.docx'];
    let added = 0;
    let rejected = 0;

    // 拖拽多份时只取第一份，并明确告知其余被忽略 —— 静默丢弃比报错更让人困惑
    let files = Array.from(fileList);
    if (files.length > 1) {
        showToast(`一次只能上传 1 份报告，已忽略其余 ${files.length - 1} 个文件`, 'info');
        files = files.slice(0, 1);
    }

    files.forEach(file => {
        const ext = '.' + file.name.split('.').pop().toLowerCase();
        if (!validTypes.includes(ext)) {
            rejected++;
            return;
        }
        if (file.size > 200 * 1024 * 1024) {
            showToast(`${file.name} 超过200MB限制`, 'error');
            return;
        }
        // 避免重复
        if (App.uploadedFiles.some(f => f.name === file.name && f.size === file.size)) {
            showToast(`${file.name} 已存在`, 'info');
            return;
        }

        const fileObj = {
            id: Date.now() + Math.random(),
            name: file.name,
            size: file.size,
            type: ext,
            status: 'uploading',
            progress: 0,
            file: file          // 保留原始文件对象，用于真实上传
        };
        App.uploadedFiles.push(fileObj);
        added++;
        // 真实上传到提取服务
        uploadToExtractService(fileObj);
    });

    if (rejected > 0) {
        showToast(`${rejected}个文件格式不支持`, 'error');
    }
    if (added > 0) {
        showToast('正在上传并提取，请稍候...', 'success');
    }
}

/**
 * 真实上传：调提取服务（DeepSeek API + 智能体一提示词）
 * 文件 → 提取服务 → 精简JSON
 */
async function uploadToExtractService(fileObj) {
    // 模拟模式下走旧逻辑（演示用）
    if (API_CONFIG.MODE === 'mock') {
        simulateUpload(fileObj);
        return;
    }

    const formData = new FormData();
    // v=104：把续传凭据带到提交层 —— 提交层（submitExtraction）是从 formData 里取 File
    //   对象读 uploadId 的，而上传过程把它记在 App.uploadedFiles 条目上。两者不是同一个
    //   对象，不在这一步"搬运"的话，续传判定永远读不到 ID，断点续传静默失效。
    if (fileObj.uploadId) fileObj.file.uploadId = fileObj.uploadId;
    formData.append('file', fileObj.file);
    formData.append('model', getSelectedModel());   // v=78 多模型

    // v=94：改走异步任务 + 轮询（同步长请求会被 Cloudflare 边缘 ~100s 掐成 524）
    // v=96：>10MB 的大文件改走分片上传，上传阶段回报真实片数进度
    const uploadStartAt = Date.now();
    fileObj.ocrHint = false;
    fileObj.ocrProgress = '';
    fileObj.uploadHint = '';

    // 进度条模拟（fetch不支持上传进度，用递增模拟）
    const progressTimer = setInterval(() => {
        // v=96：分片上传阶段由真实片数驱动进度条，此处不再叠加模拟值
        if (!fileObj.uploadHint && fileObj.progress < 90) {
            fileObj.progress = Math.min(100, fileObj.progress + Math.random() * 10 + 3);   // v=82：上限 100，避免 102% 这种 bug
            renderProgressList();
        }
        // 兜底提示：若服务端进度未回报（如旧后端），超过 60s 也切换 OCR 文案安抚用户
        if (!fileObj.uploadHint && !fileObj.ocrHint && (Date.now() - uploadStartAt) > 60000) {
            fileObj.ocrHint = true;
            renderProgressList();
        }
    }, 300);

    try {
        const data = await requestExtraction(formData, (st) => {
            // 服务端真实阶段回报（优先级高于时间兜底）
            if (st.stage === 'upload') {
                // v=96：分片上传阶段 → 真实片数进度（进度条走前 30%，其余留给解析/OCR）
                // v=104：记住 uploadId（断点续传凭据）+ 显示续传/重试状态
                if (st.uploadId) fileObj.uploadId = st.uploadId;
                if (st.retry) {
                    // 隧道瞬断等瞬断类错误正在自动重试：必须显示出来，否则用户以为卡死了
                    fileObj.uploadHint = `第 ${st.failedIndex + 1}/${st.total} 片重试中（第 ${st.attempt}/${st.maxAttempt} 次，等 ${Math.round((st.waitMs || 0) / 1000)}s）…`;
                } else {
                    fileObj.uploadHint = st.total > 0
                        ? `上传中 ${st.current}/${st.total} 片${st.skipped ? `（已续传 ${st.skipped} 片）` : ''}`
                        : '上传中';
                }
                if (st.total > 0) {
                    fileObj.progress = Math.min(30, Math.round(st.current / st.total * 30));
                }
                fileObj.ocrHint = false;
                fileObj.ocrProgress = '';
            } else if (st.stage === 'ocr') {
                fileObj.uploadHint = '';
                fileObj.ocrHint = true;
                fileObj.ocrProgress = st.total > 0 ? `${st.current}/${st.total} 页` : '';
            } else if (st.stage === 'parse') {
                fileObj.uploadHint = '';
                fileObj.ocrHint = false;
                fileObj.ocrProgress = '';
            }
            renderProgressList();
        });
        clearInterval(progressTimer);
        fileObj.progress = 100;
        fileObj.status = 'done';
        fileObj.extractResult = data;   // 保存提取JSON
        renderProgressList();
        renderFileList();
        checkUploadAction();
        showToast(`${fileObj.name} 提取完成`, 'success');
    } catch (err) {
        clearInterval(progressTimer);
        fileObj.status = 'error';
        fileObj.error = err.message;
        // v=104：保住续传凭据 —— 重试时凭它跳过已落盘分片，不必从第 1 片重来
        if (err && err.uploadId) fileObj.uploadId = err.uploadId;
        fileObj.uploadHint = '';
        renderProgressList();
        renderFileList();
        showToast(`${fileObj.name} 提取失败: ${err.message}`, 'error');
    }
}

function simulateUpload(fileObj) {
    const interval = setInterval(() => {
        fileObj.progress += Math.random() * 15 + 5;
        if (fileObj.progress >= 100) {
            fileObj.progress = 100;
            fileObj.status = 'done';
            clearInterval(interval);
            setTimeout(() => {
                renderProgressList();
                renderFileList();
                checkUploadAction();
            }, 300);
        }
        renderProgressList();
    }, 200);
}

function renderProgressList() {
    const container = $('#progressList');
    const uploading = App.uploadedFiles.filter(f => f.status === 'uploading');

    if (uploading.length === 0) {
        container.innerHTML = '';
        return;
    }

    container.innerHTML = uploading.map(f => `
        <div class="progress-item" data-id="${f.id}">
            <div class="progress-file-icon">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                    <polyline points="14 2 14 8 20 8"/>
                </svg>
            </div>
            <div class="progress-info">
                <div class="progress-name">${escapeHtml(f.name)}</div>
                <div class="progress-bar-wrap">
                    <div class="progress-bar" style="width:${f.progress}%"></div>
                </div>
            </div>
            <span class="progress-percent">${Math.floor(f.progress)}%</span>
            <span class="progress-status uploading" title="${f.uploadHint ? '正在分片上传大文件，请勿关闭页面' : (f.ocrHint ? '检测到扫描件，正在逐页 OCR 识别（约3-6分钟），请勿关闭页面' : '正在上传并解析文档')}">${f.uploadHint ? escapeHtml(f.uploadHint) : (f.ocrHint ? escapeHtml(`OCR识别中 ${f.ocrProgress || ''}`.trim()) : '上传中')}</span>
        </div>
    `).join('');
}

function renderFileList() {
    const section = $('#fileListSection');
    const list = $('#fileList');
    const count = $('#fileCount');
    // v=104：把「上传失败」的文件也列出来 —— 原逻辑只渲染 status==='done'，
    //   失败文件（status='error'）在界面上彻底消失，用户除了 3 秒的 toast 之外
    //   没有任何重试入口，只能重新拖一次文件（还得从第 1 片重来）。
    const shown = App.uploadedFiles.filter(f => f.status === 'done' || f.status === 'error');

    if (shown.length === 0) {
        section.style.display = 'none';
        return;
    }

    section.style.display = 'block';
    count.textContent = `${shown.length} 个文件`;

    list.innerHTML = shown.map(f => {
        const ok = f.status === 'done';
        const iconClass = f.type === '.pdf' ? 'pdf' : 'doc';
        const iconSvg = f.type === '.pdf'
            ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>'
            : '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';

        return `
            <div class="file-item${ok ? '' : ' has-error'}" data-id="${f.id}">
                <div class="file-icon-sm ${iconClass}">${iconSvg}</div>
                <div class="file-info">
                    <div class="file-name">${escapeHtml(f.name)}</div>
                    <div class="file-meta">${ok
                        ? `${formatFileSize(f.size)} · ${f.type.toUpperCase().replace('.', '')} · 已上传`
                        : `${formatFileSize(f.size)} · <span class="file-error">上传失败：${escapeHtml(String(f.error || '未知原因').slice(0, 40))}</span>`}</div>
                </div>
                <div class="file-actions">
                    <button class="file-btn" title="${ok ? '重新上传' : '重试（断点续传）'}" onclick="retryUpload('${f.id}')">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-9-9c2.5 0 4.8 1 6.4 2.6L21 8"/><path d="M21 3v5h-5"/></svg>
                    </button>
                    <button class="file-btn delete" title="删除" onclick="deleteFile('${f.id}')">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                    </button>
                </div>
            </div>
        `;
    }).join('');
}

function retryUpload(id) {
    const file = App.uploadedFiles.find(f => f.id == id);
    if (!file) return;

    // 演示模式沿用模拟上传
    if (API_CONFIG.MODE === 'mock') {
        if (!confirm(`确认重新上传「${file.name}」？`)) return;
        file.status = 'uploading';
        file.progress = 0;
        renderFileList();
        renderProgressList();
        simulateUpload(file);
        showToast(`正在重新上传 ${file.name}`, 'info');
        return;
    }

    // v=104：修复「假上传」bug —— 原实现无条件调 simulateUpload()，界面 3 秒后显示
    //   「已上传」，但服务端根本没有这个文件，后续分析必然失败。
    //   现在走真实链路；若 file.uploadId 仍在，会凭它在服务端已落盘的分片处续传。
    const resume = !!file.uploadId;
    if (!confirm(`确认重新上传「${file.name}」？${resume ? '\n将从上次中断处续传（不必重传已上传的分片）' : ''}`)) return;

    file.status = 'uploading';
    file.progress = 0;
    file.error = '';
    file.uploadHint = '';
    renderFileList();
    renderProgressList();
    uploadToExtractService(file);
    showToast(`正在重新上传 ${file.name}${resume ? '（续传）' : ''}`, 'info');
}

function deleteFile(id) {
    const file = App.uploadedFiles.find(f => f.id == id);
    if (!file) return;
    if (!confirm(`确认删除「${file.name}」？`)) return;

    // v=104：用户「显式删除」才清理服务端残留分片。
    //   上传失败时**故意不清理**（要留着续传），所以清理动作收敛到这里；
    //   若已 upload_complete 成功，服务端目录早已自行删除，这里是空操作。
    if (file.uploadId) {
        try {
            fetch(`${API_CONFIG.EXTRACT_API}/upload_abort`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ upload_id: file.uploadId })
            }).catch(() => { /* best-effort，失败不影响删除 */ });
        } catch (_) { /* 忽略 */ }
    }

    App.uploadedFiles = App.uploadedFiles.filter(f => f.id != id);
    renderFileList();
    renderProgressList();
    checkUploadAction();
    // v=115：删文件必须同步清空页面上的提取结果与评分画像。
    //   否则「删掉旧报告 → 还没传新报告」这个窗口期里，页面仍挂着上一份的评分，
    //   用户很容易把它当成新报告的结果 —— 这正是讨论中要根治的误导点。
    resetAnalysisView();
    showToast(`已删除 ${file.name}，评分预览已同步清空`, 'success');
}

function checkUploadAction() {
    const action = $('#uploadAction');
    const hasDone = App.uploadedFiles.some(f => f.status === 'done');
    action.style.display = hasDone ? 'block' : 'none';
}

/**
 * v=115：清空「上一份报告」留在页面上的提取结果与评分画像。
 * 触发时机：deleteFile() —— 用户删除当前报告时（配合「一次一份」的约束）。
 *
 * ⚠️ 为什么只 destroy 图表、不 Charts.refresh()：
 *   charts.js 的 _getScoreData() 取不到真实评分时会**回退 MOCK 假数据**，
 *   清空后刷新图表等于把 Mock 的 7.8 分画出来，比"显示上一份"更误导。
 *   所以用新增的 Charts.reset()：只销毁实例、不重建。
 *
 * ⚠️ 为什么 #scoring 是整段隐藏而不是逐字段置空：
 *   该区块内带着硬编码默认值（#overallScore=7.8、"优秀 (A)"、整段评估结论），
 *   逐字段清空既清不干净、又可能与下次 fillScoreCards() 打架；整段隐藏最干净。
 *
 * ---------------------------------------------------------------------------
 * v=116 修复：「删除报告后提取结果区变成永久空壳」缺陷
 *
 * 现象：删掉第 1 份 → 再传第 2 份 → Step2「数据提取结果」只剩标题，42 张指标
 *       卡片全没了；控制台刷 42 条 `l4-XX 元素不存在` +
 *       `fillExtractCards 完成：填充 0 个，跳过 0 个`
 *       （跳过 0 ⇒ 说明提取数据本身是齐的，纯粹是 DOM 骨架没了）。
 *
 * 根因：#extractResult 里的 42 张 l4-* 卡片骨架是 index.html 的**静态 HTML**；
 *       fillExtractCards() 只负责「填充已存在的元素」，从不创建元素；
 *       而 resetAnalysisView() 原用 innerHTML = '' 把骨架整段删除 ⇒ 无法再生。
 *       （顺带坏处：fillScoreCards 会往 l4-XX-src 里 append 评分徽章，
 *         不还原骨架则会跨报告累积徽章。）
 *
 * 修法：① 页面加载时抓一份「出厂骨架」快照；
 *       ② reset 时用快照还原（= 清空上一份数据，但保留 DOM 骨架）；
 *       ③ fillExtractCards 前再自愈一次 ⇒ 已处于坏状态的页面无需刷新即可恢复。
 * ---------------------------------------------------------------------------
 */
let _extractResultTemplate = null;
(function captureExtractResultTemplate() {
    const grab = () => {
        try {
            const box = document.getElementById('extractResult');
            // 只在骨架齐全时留底（若加载时已被清空，宁可空着交给用户刷新）
            if (box && box.innerHTML.indexOf('id="l4-01"') !== -1) {
                _extractResultTemplate = box.innerHTML;
            }
        } catch (e) {
            console.warn('提取结果骨架快照失败:', e);
        }
    };
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', grab, { once: true });
    } else {
        grab();
    }
})();

/** 确保 #extractResult 的卡片骨架在位；若已被清空则用出厂快照重建。 */
function ensureExtractResultDom() {
    const box = document.getElementById('extractResult');
    if (!box) return null;
    if (document.getElementById('l4-01')) {
        // 骨架完好；若加载期留底失败（极端情形），趁骨架仍在且尚未被评分徽章污染时补留一份
        if (_extractResultTemplate === null &&
            box.innerHTML.indexOf('id="l4-01"') !== -1 &&
            box.innerHTML.indexOf('score-badge') === -1) {
            _extractResultTemplate = box.innerHTML;
        }
        return box;   // 骨架完好
    }
    if (_extractResultTemplate) {                        // 用快照自愈
        box.innerHTML = _extractResultTemplate;
        console.info('[v=116] 提取结果卡片骨架已自愈重建（修复"删除报告后空壳"）');
    } else {
        console.warn('[v=116] 提取结果骨架快照缺失，无法自愈；请刷新页面');
    }
    return box;
}

function resetAnalysisView() {
    // 1) 隐藏并清空结果区（Step2 提取结果 / Step3 评分画像）
    const analysis = $('#analysis');
    const scoring = $('#scoring');
    if (analysis) analysis.style.display = 'none';
    if (scoring) scoring.style.display = 'none';

    const resultBox = $('#extractResult');
    if (resultBox) {
        // v=116 修复：原为 `resultBox.innerHTML = ''` —— 会把 #extractResult 内的静态
        //   卡片骨架（42 张 l4-*）永久删除，而页面上没有任何重建代码 ⇒ 改为还原出厂快照：
        //   视觉上等于「清空上一份数据」，但 DOM 骨架保留，下次提取才能正常填充。
        resultBox.style.display = 'none';
        try { ensureExtractResultDom(); } catch (e) { console.warn('提取结果骨架还原失败:', e); }
    }
    const progressBox = $('#extractProgress');
    if (progressBox) progressBox.style.display = 'none';
    const modalBody = $('#reportModalBody');
    if (modalBody) modalBody.innerHTML = '';

    // 2) 销毁图表实例（不重建，避免 MOCK 兜底把假数据画出来）
    if (typeof Charts !== 'undefined' && Charts.reset) {
        try { Charts.reset(); } catch (e) { console.warn('图表重置失败:', e); }
    }

    // 3) 复位应用状态 —— 不留下任何「还属于上一份报告」的引用
    App.extractResult = null;
    App.scoreResult = null;
    App.companyType = null;
    App.analysisDone = false;
    App._historySaveToken = null;
    App._historySavedForToken = false;
    App._reportScoreData = null;
    App._adpRawText = '';

    // 4) 按钮复位（下次上传完成后由 checkUploadAction() 重新显示）
    const btn = $('#analyzeBtn');
    if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-9-9c2.5 0 4.8 1 6.4 2.6L21 8"/><path d="M21 3v5h-5"/></svg> 开始数据提取';
    }

    // 5) Supabase 的 history 记录**不删** —— 历史是用户资产，删本地文件不该删掉「我的分析」。
}

// ============ 数据分析 ============
async function startAnalysis() {
    // ---- v=115 门禁：数据提取（首次会真调提取接口）必须登录 ----
    // handleFiles() 已拦过一次，这里再拦一次属防御：页面刷新后虽取不到文件，
    // 但仍有人可能从控制台直接调 startAnalysis()。
    if (!await requireLogin('数据提取')) return;

    const analysisSection = $('#analysis');
    analysisSection.style.display = 'block';

    const progress = $('#extractProgress');
    const result = $('#extractResult');
    const status = $('#extractStatus');

    progress.style.display = 'block';
    result.style.display = 'none';

    const btn = $('#analyzeBtn');
    btn.disabled = true;
    btn.innerHTML = '<div class="loader-ring" style="width:20px;height:20px;border:2px solid rgba(255,255,255,0.3);border-top-color:#fff;border-radius:50%;display:inline-block;animation:spin 1s linear infinite;"></div> 分析中...';

    // 模拟模式下走旧逻辑（演示用）
    if (API_CONFIG.MODE === 'mock') {
        simulateAnalysis(btn, status, progress, result);
        return;
    }

    // 真实模式：v=115 起一次只允许 1 份，直接取数组首项。
    //   原来用 find(status==='done') 取「第一个完成的」，多份并存时与用户以为的
    //   「最后上传的那份」不一致，是造成「传了新报告、结果还是上一份」误导的根因之一。
    const doneFile = App.uploadedFiles[0];
    if (!doneFile || doneFile.status !== 'done') {
        showToast('请先上传并完成提取的报告', 'error');
        btn.disabled = false;
        btn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-9-9c2.5 0 4.8 1 6.4 2.6L21 8"/><path d="M21 3v5h-5"/></svg> 开始数据提取';
        return;
    }

    // 读取用户选择的公司类型（上市/非上市 → 评分分流）
    const companyType = getCompanyType();
    App.companyType = companyType;
    status.textContent = `正在提交提取结果到ADP评分（${companyType === 'listed' ? '上市公司' : '非上市公司'}方案）...`;

    // 步骤1：如果没有提取结果，先调提取服务（带上公司类型用于评分分流）
    // v=94：改走异步任务 + 轮询，避免公网长请求被 Cloudflare 边缘 524 掐断
    const runExtract = doneFile.extractResult
        ? Promise.resolve(doneFile.extractResult)
        : requestExtraction(
            (() => { const fd = new FormData(); fd.append('file', doneFile.file); fd.append('company_type', companyType); fd.append('model', getSelectedModel()); return fd; })(),
            (st) => {
                if (st.stage === 'upload') {
                    // v=96：分片上传阶段
                    status.textContent = st.total > 0
                        ? `正在分片上传大文件（${st.current}/${st.total} 片）...`
                        : '正在上传文件...';
                } else if (st.stage === 'ocr') {
                    status.textContent = st.total > 0
                        ? `检测到扫描件，正在 OCR 识别（${st.current}/${st.total} 页，约3-6分钟）...`
                        : '检测到扫描件，正在 OCR 识别（约3-6分钟）...';
                } else if (st.stage === 'parse') {
                    status.textContent = '正在解析文档文本...';
                }
            }
          );

    runExtract
        .then(extractData => {
            // 两段式渲染第一段：提取响应已含「评分增强数据」（分数已由前端Python算好）
            App.extractResult = extractData;
            status.textContent = `提取完成，正在计算评分...`;
            try {
                renderExtractResult(extractData);
                fillScoreCards(extractData);   // 立即填充分数卡片（完整度/质量/对标/总分）
            } catch (e) {
                console.error('评分增强数据填充失败:', e);
            }

            // 步骤2：回传ADP评分API（智能体二），获取 Top10 改进建议（第二段渲染）
            status.textContent = '正在生成改进建议...';
            return callScoringAPI(extractData, companyType);
        })
        .then(scoreData => {
            App.scoreResult = scoreData;
            progress.style.display = 'none';
            result.style.display = 'block';
            App.analysisDone = true;
            // 分析完成标记：previewReport 首次打开时保存历史（防重复）
            App._historySaveToken = Date.now();
            App._historySavedForToken = false;

            btn.disabled = false;
            btn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-9-9c2.5 0 4.8 1 6.4 2.6L21 8"/><path d="M21 3v5h-5"/></svg> 重新分析';

            showToast('评分完成！', 'success');

            // 渲染提取结果到前端（用 App.extractResult，因为 extractData 变量在第二个 then 作用域外）
            renderExtractResult(App.extractResult);

            // 显示评分区域
            $('#scoring').style.display = 'block';

            // 初始化图表（用真实评分数据）
            setTimeout(() => {
                Charts.refresh();
                scrollToSection('analysis');
            }, 300);
        })
        .catch(err => {
            progress.style.display = 'none';
            btn.disabled = false;
            btn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-9-9c2.5 0 4.8 1 6.4 2.6L21 8"/><path d="M21 3v5h-5"/></svg> 重新分析';
            showToast(`分析失败: ${err.message}`, 'error');
        });
}

/**
 * 读取用户选择的公司类型（评分分流依据）
 * @returns {string} 'listed'=上市公司 | 'unlisted'=非上市公司
 */
function getCompanyType() {
    const checked = document.querySelector('input[name="companyType"]:checked');
    return checked ? checked.value : 'listed';
}

// ============ 分析模型选择（v=78 多模型，one-api 网关路由） ============
function getSelectedModel() {
    // localStorage 记忆，默认 deepseek-v4-flash
    const saved = localStorage.getItem('esg.selectedModel');
    const models = (API_CONFIG.MODELS || []).map(m => m.id);
    if (saved && models.includes(saved)) return saved;
    return API_CONFIG.DEFAULT_MODEL || 'deepseek-v4-flash';
}

function setSelectedModel(modelId) {
    localStorage.setItem('esg.selectedModel', modelId);
    // 更新 UI 高亮
    document.querySelectorAll('#modelOptions .company-type-option').forEach(el => {
        el.classList.toggle('active', el.dataset.model === modelId);
    });
    const model = (API_CONFIG.MODELS || []).find(m => m.id === modelId);
    showToast(`已切换分析模型：${model ? model.label : modelId}`, 'info');
}

function initModelSelector() {
    const container = $('#modelOptions');
    if (!container) return;
    const models = API_CONFIG.MODELS || [];
    if (!models.length) return;
    const current = getSelectedModel();
    container.innerHTML = models.map(m => `
        <label class="company-type-option ${m.id === current ? 'active' : ''}" data-model="${m.id}"
               onclick="setSelectedModel('${m.id}')">
            <span class="option-card">
                <span class="option-name">${m.label}</span>
                <span class="option-desc">${m.desc}</span>
            </span>
        </label>
    `).join('');
}

/**
 * 调用ADP评分API（智能体二）
 * 输入: 提取JSON → 输出: 评分JSON（总分+障碍度+建议）
 *
 * 回退逻辑：
 *   - 若 ADP_API 已配置 → 真实调用
 *   - 若 ADP_API 未配置 → 返回占位数据，避免 404 报错，并提示"评分待对接"
 */
function callScoringAPI(extractData, companyType) {
    const endpoint = API_CONFIG.ADP_API;
    const ctype = companyType || 'listed';  // listed=上市公司, unlisted=非上市公司

    if (!endpoint) {
        // ADP 评分未对接，返回占位数据（前端能正常显示）
        console.warn(`⚠️ ADP_API 未配置，评分服务待对接（公司类型: ${ctype}）`);
        return Promise.resolve({
            success: true,
            pending: true,
            message: 'ADP 评分服务待对接',
            company_type: ctype,
            评分结果: {
                总分: 0,
                维度得分: {
                    '企业内部碳管理': 0,
                    '供应链碳管理': 0,
                    '透明度': 0,
                    '金融化情况': 0
                },
                评分等级: '待对接',
                评分依据: '等待ADP评分接口对接完成（提取已成功）',
                障碍度分析: {
                    障碍度清单: [],
                    失分原因汇总: '评分服务尚未对接'
                },
                改进建议: []
            }
        });
    }

    // 提取增强 JSON：取 extracted 内容（报告元数据+指标提取结果+评分增强数据）
    const enrichedJson = (extractData && extractData.extracted)
        ? extractData.extracted
        : extractData || {};

    return fetchWithTimeout(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // 请求体对齐 ADP 工作流「开始」节点定义的两个变量：
        //   - user.esg_json   : 增强JSON（含42指标 + 评分增强数据，分数已算好）
        //   - user.company_type: 'listed'(上市公司) | 'unlisted'(非上市公司)
        // 双发兼容：平铺 + 嵌套 user.* 两种结构，ADP 哪个能用就用哪个
        body: JSON.stringify({
            company_type: ctype,           // 平铺（条件判断1 直接读）
            esg_json: enrichedJson,        // 平铺（ADP 开始节点期望的字段名）
            extract_data: enrichedJson,    // 兜底老字段名
            model: getSelectedModel(),     // v=78 多模型（one-api 网关路由）
            user: {                        // 嵌套（部分 ADP 工作流用 user.* 命名空间）
                company_type: ctype,
                esg_json: enrichedJson
            }
        })
    }, 180000).then(res => parseJsonResponse(res, '根因分析'))   // v=84：根因分析最长 180s ｜ v=93：透出后端中文原因
    .then(scoreData => {
        // ADP 结束节点输出为 Output.finalreport，兼容两种返回格式：
        //   - 直接 finalreport 字段
        //   - 嵌套 Output.finalreport 字段
        if (scoreData && !scoreData.评分结果 && scoreData.finalreport) {
            scoreData.评分结果 = scoreData.finalreport;
        }
        if (scoreData && scoreData.Output && scoreData.Output.finalreport) {
            scoreData.评分结果 = scoreData.Output.finalreport;
        }
        return scoreData;
    });
}

/** 渲染提取结果到前端展示区 */
function renderExtractResult(extractData) {
    // 提取结果摘要容器（可能不存在，不影响卡片填充）
    const container = $('#extractResultContent');
    if (container) {
        // 简化展示：显示企业信息和关键指标
        const meta = extractData.报告元数据 || {};
        const indicators = extractData.指标提取结果 || {};
        container.innerHTML = `
            <div class="extract-meta" style="margin-bottom:16px;">
                <strong>企业名称:</strong> ${meta['企业名称'] || '-'} |
                <strong>报告年份:</strong> ${meta['报告年份'] || '-'} |
                <strong>所属行业:</strong> ${meta['所属行业'] || '待定'}
            </div>
            <pre style="max-height:300px; overflow:auto; font-size:12px; background:var(--color-card-bg); padding:12px; border-radius:8px;">${JSON.stringify(indicators, null, 2).slice(0, 3000)}</pre>
        `;
    }
    // 用真实数据填充页面卡片占位元素（核心逻辑：无论 container 是否存在都要跑）
    fillExtractCards(extractData);
}

/**
 * 提取成功后，用真实JSON数据填充页面上的卡片（index.html 中的 l4-XX 占位）
 * 提取前显示 mock 硬编码，提取后替换为真实数据（值 + 原文引用）
 */

// 42 指标路径映射（模块级，fillExtractCards 与 fillScoreCards 共用）
// 编号顺序必须与 index.html 的 l4-XX 一致
const L4_MAP = [
    // 维度一：企业内部碳管理（01-13）
    ['企业内部碳管理', '碳排放', '碳总量'],
    ['企业内部碳管理', '碳排放', '碳排放强度'],
    ['企业内部碳管理', '碳排放', '范围一排放'],
    ['企业内部碳管理', '碳排放', '范围二排放'],
    ['企业内部碳管理', '碳排放', '范围三排放'],
    ['企业内部碳管理', '碳排放', '碳排放抵消与同比变化'],
    ['企业内部碳管理', '环境治理', '环保投资'],
    ['企业内部碳管理', '环境治理', '碳管理体系建设'],
    ['企业内部碳管理', '环境治理', '环境管理体系'],
    ['企业内部碳管理', '能源消耗与节能方案', '能源类型和强度'],
    ['企业内部碳管理', '能源消耗与节能方案', '能耗同比变化'],
    ['企业内部碳管理', '能源消耗与节能方案', '节能目标与改造情况'],
    ['企业内部碳管理', '能源消耗与节能方案', '能效提升对比度'],
    // 维度二：供应链碳管理（14-25）
    ['供应链碳管理', '供应商碳管理', '供应商碳目标'],
    ['供应链碳管理', '供应商碳管理', '供应商碳披露及评价要求'],
    ['供应链碳管理', '供应商碳管理', '供应商碳培训及考核情况'],
    ['供应链碳管理', '供应商碳管理', '供应商绿色采购标准'],
    ['供应链碳管理', '供应链协同减排', '供应商激励'],
    ['供应链碳管理', '供应链协同减排', '绿色物流'],
    ['供应链碳管理', '供应链协同减排', '包装减量及回收情况'],
    ['供应链碳管理', '供应链协同减排', '循环经济链塑造'],
    ['供应链碳管理', '供应链风险管控', '高碳供应商占比'],
    ['供应链碳管理', '供应链风险管控', '气候风险暴露供应商数量'],
    ['供应链碳管理', '供应链风险管控', '碳合规风险事件发生率'],
    ['供应链碳管理', '供应链风险管控', '供应链碳风险传导影响评估'],
    // 维度三：透明度（26-35）
    ['透明度', '披露范围与完整性', '碳排放披露范围'],
    ['透明度', '披露范围与完整性', '供应链碳披露层级'],
    ['透明度', '披露范围与完整性', '碳资产与交易信息披露比例'],
    ['透明度', '披露质量与可信度', '第三方核查覆盖率'],
    ['透明度', '披露质量与可信度', 'ESG评级结果及行业排名'],
    ['透明度', '披露质量与可信度', 'ISO14064/ISO50001认证'],
    ['透明度', '披露质量与可信度', '信息披露频次'],
    ['透明度', '披露合规性', '监管强制披露要求符合性'],
    ['透明度', '披露合规性', '交易所ESG披露指引符合性'],
    ['透明度', '披露合规性', '历史信息披露更正次数'],
    // 维度四：金融化情况（36-42）
    ['金融化情况', '碳金融', '绿色信贷'],
    ['金融化情况', '碳金融', '绿色债券'],
    ['金融化情况', '碳金融', '碳金融衍生品参与情况'],
    ['金融化情况', '碳资产管理', '碳配额管理'],
    ['金融化情况', '碳资产管理', 'CCER开发与储备'],
    ['金融化情况', '碳资产管理', '碳资产交易收益'],
    ['金融化情况', '碳资产管理', '碳资产保值增值策略'],
];

/** 填充评分数据（两段式渲染第一段）：从「评分增强数据」读取分数，给所有评分卡片填真实值 */
function fillScoreCards(extractData) {
    try {
        // 解包 {extracted: {...}} 结构
        if (extractData && extractData.extracted) extractData = extractData.extracted;
        const scoreData = extractData['评分增强数据'];
        if (!scoreData) {
            console.warn('⚠️ 无评分增强数据（提取服务未计算评分）');
            return;
        }
        console.log('🎯 [fillScoreCards] 评分增强数据:', scoreData);

        const dimScores = scoreData['维度得分'] || {};
        const total = scoreData['总分'];
        const level = scoreData['评分等级'];

        // === 1. Hero 区域卡片 ===
        const heroTotal = document.getElementById('hero-total');
        if (heroTotal && total !== undefined) heroTotal.textContent = total;
        const heroLevel = document.getElementById('hero-level');
        if (heroLevel && level) heroLevel.textContent = level;
        const heroScore = document.getElementById('hero-score');
        if (heroScore && total !== undefined) heroScore.textContent = total;

        // === 2. 评分画像区（综合分圆环 + 等级）===
        const overallScore = document.getElementById('overallScore');
        if (overallScore && total !== undefined) overallScore.textContent = total;
        const scoreGrade = document.querySelector('.score-grade');
        if (scoreGrade && level) scoreGrade.textContent = `${level} (${getGradeLabel(total)})`;

        // === 2.1 SVG 评分圆环动态化（总分驱动 stroke-dashoffset + 颜色）===
        const scoreCircle = document.getElementById('scoreCircle');
        if (scoreCircle && typeof total === 'number') {
            // 周长 534（r=85, 2πr≈534.07），按总分 0-10 映射
            const circumference = 534;
            const ratio = Math.max(0, Math.min(10, total)) / 10;
            const offset = circumference * (1 - ratio);
            scoreCircle.setAttribute('stroke-dashoffset', offset.toFixed(1));
            // 低分红 / 中黄 / 高绿
            const strokeColor = ratio < 0.4 ? '#ef4444' : ratio < 0.7 ? '#f59e0b' : '#10b981';
            scoreCircle.style.stroke = strokeColor;
            // 渐变 defs 同步（简单覆盖为实色，渐变由 CSS 渐变替代视觉效果）
            const grad = document.getElementById('scoreGradient');
            if (grad) {
                const stops = grad.querySelectorAll('stop');
                if (stops.length >= 2) {
                    stops[0].setAttribute('stop-color', strokeColor);
                    stops[1].setAttribute('stop-color', strokeColor);
                }
            }
        }

        // === 2.2 评分页评估结论文字（.score-summary p）动态化 ===
        const scoreSummaryP = document.querySelector('.score-summary p');
        if (scoreSummaryP) {
            const sortedDims = Object.entries(dimScores).sort((a, b) => b[1] - a[1]);
            const strongest = sortedDims[0] || ['-', 0];
            const weakest = sortedDims[sortedDims.length - 1] || ['-', 0];
            scoreSummaryP.innerHTML = `该企业碳治理能力整体处于<span class="text-green font-bold">${level}</span>水平（综合评分 <strong>${total}</strong> 分）。其中 <strong>${strongest[0]}</strong> 的碳治理能力相对扎实（${strongest[1]} 分），<strong>${weakest[0]}</strong> 的碳治理能力存在明显缺口（${weakest[1]} 分），是能力建设的优先方向。`;
        }

        // === 3. 维度得分 ===
        const dimScoreEls = {
            '企业内部碳管理': document.getElementById('score-dim-internal'),
            '供应链碳管理': document.getElementById('score-dim-supply'),
            '透明度': document.getElementById('score-dim-transparency'),
            '金融化情况': document.getElementById('score-dim-finance'),
        };
        Object.entries(dimScores).forEach(([dim, score]) => {
            const el = dimScoreEls[dim];
            if (el) el.textContent = score;
        });

        // === 4. 评估结论文字（按维度得分自动生成）===
        const conclusionEl = document.querySelector('.section-analysis .conclusion-text, #analysisConclusion');
        if (conclusionEl) {
            const sortedDims = Object.entries(dimScores).sort((a, b) => b[1] - a[1]);
            const strongest = sortedDims[0];
            const weakest = sortedDims[sortedDims.length - 1];
            conclusionEl.innerHTML = `
                该企业碳治理能力整体处于 <span class="text-green font-bold">${level}</span> 水平（综合评分 <strong>${total}</strong>）。
                其中 <strong>${strongest[0]}</strong> 的碳治理能力相对扎实（${strongest[1]} 分），
                <strong>${weakest[0]}</strong> 的碳治理能力存在明显缺口（${weakest[1]} 分），是能力建设的优先方向。
            `;
        }

        // === 5. 维度标签（按 data-dim 匹配，按分排序高/中/低贴不同样式）===
        const dimKeyMap = {
            '企业内部碳管理': 'internal',
            '供应链碳管理': 'supply',
            '透明度': 'disclosure',
            '金融化情况': 'finance',
        };
        const dimNameMap = {
            'internal': '内部管理',
            'supply': '供应链',
            'disclosure': '透明度',
            'finance': '金融化',
        };
        const sortedEntries = Object.entries(dimScores).sort((a, b) => b[1] - a[1]);
        const maxScore = sortedEntries[0][1];
        const minScore = sortedEntries[sortedEntries.length - 1][1];
        Object.entries(dimScores).forEach(([dim, score]) => {
            const key = dimKeyMap[dim];
            const el = document.querySelector(`[data-dim="${key}"]`);
            if (!el) return;
            const isHighest = score === maxScore;
            const isLowest = score === minScore;
            // 重置 className 再贴新样式
            el.classList.remove('green', 'yellow', 'blue', 'dim-tag-leading', 'dim-tag-needs-improvement');
            if (isHighest) {
                el.classList.add('dim-tag-leading');
                el.textContent = `${dimNameMap[key]}领先 ✓`;
            } else if (isLowest) {
                el.classList.add('dim-tag-needs-improvement');
                el.textContent = `${dimNameMap[key]}待提升`;
            } else {
                el.classList.add('dim-tag-normal');
                el.textContent = `${dimNameMap[key]}正常`;
            }
        });

        // === 6. 每个 L4 指标：挂评分徽章（完整度/质量/对标/L4得分/障碍度）===
        const l4Results = scoreData['L4计算结果'] || {};
        L4_MAP.forEach((path, idx) => {
            const [dim, sub, name] = path;
            const result = l4Results[name];
            if (!result) return;
            const idxStr = (idx + 1).toString().padStart(2, '0');
            const srcEl = document.getElementById(`l4-${idxStr}-src`);
            if (srcEl) {
                const badge = document.createElement('span');
                badge.className = 'score-badge';
                badge.innerHTML = ` 评分 ${result['L4得分']} | 完整度${result['完整度分']} 质量${result['质量分']} 对标${result['对标分']}`;
                srcEl.appendChild(badge);
            }
        });

        // === 7. 四维度详情卡片动态化（v=79）===
        // 维度 → 卡片 id 映射（与 index.html 占位符对应）
        const dimCardMap = {
            '企业内部碳管理': { nameId: 'detail-name-internal', scoreId: 'detail-score-internal', listId: 'detail-list-internal', cls: 'green' },
            '供应链碳管理':   { nameId: 'detail-name-supply',   scoreId: 'detail-score-supply',   listId: 'detail-list-supply',   cls: 'yellow' },
            '透明度':         { nameId: 'detail-name-disclosure', scoreId: 'detail-score-disclosure', listId: 'detail-list-disclosure', cls: 'green' },
            '金融化情况':     { nameId: 'detail-name-finance',  scoreId: 'detail-score-finance',  listId: 'detail-list-finance',  cls: 'blue' },
        };
        // 从 ADP 改进建议（App.scoreResult）取按维度的建议列表
        let suggestionsByDim = {};
        try {
            const sRes = (App.scoreResult && App.scoreResult.评分结果) || App.scoreResult || {};
            const advList = sRes.改进建议 || [];
            advList.forEach((s, i) => {
                // 建议条目无"维度"字段时，尝试从指标名反查 L4 计算结果
                let dim = s['维度'];
                if (!dim && s['指标']) {
                    const l4r = l4Results[s['指标']];
                    if (l4r && l4r['维度']) dim = l4r['维度'];
                }
                if (!dim) {
                    // 兜底：按关键词归类
                    const kw = s['指标'] || s['建议'] || '';
                    if (kw.includes('供应链') || kw.includes('供应商') || kw.includes('采购') || kw.includes('范围3')) dim = '供应链碳管理';
                    else if (kw.includes('披露') || kw.includes('报告') || kw.includes('审计') || kw.includes('GRI') || kw.includes('SASB') || kw.includes('TCFD')) dim = '透明度';
                    else if (kw.includes('金融') || kw.includes('信贷') || kw.includes('债券') || kw.includes('碳交易') || kw.includes('碳资产')) dim = '金融化情况';
                    else dim = '企业内部碳管理';
                }
                if (!suggestionsByDim[dim]) suggestionsByDim[dim] = [];
                suggestionsByDim[dim].push(s);
            });
        } catch (e) { console.warn('四维度建议归类失败:', e); }

        Object.entries(dimCardMap).forEach(([dim, cfg]) => {
            // 分数
            const score = dimScores[dim];
            const scoreEl = document.getElementById(cfg.scoreId);
            if (scoreEl && score !== undefined) {
                scoreEl.textContent = `${Number(score).toFixed(1)} / 10`;
                scoreEl.classList.add(cfg.cls);
            }
            // 列表：优先用改进建议（每条取 建议 字段，最多 3 条）
            const listEl = document.getElementById(cfg.listId);
            if (listEl) {
                const dimAdvs = (suggestionsByDim[dim] || []).slice(0, 3);
                if (dimAdvs.length > 0) {
                    listEl.innerHTML = dimAdvs.map((s, i) => {
                        const cls = i === 0 ? cfg.cls : 'yellow';
                        return `<li><span class="dot ${cls}"></span>${s['指标'] || '指标'}：${(s['建议'] || s['现状'] || '待完善').slice(0, 60)}</li>`;
                    }).join('');
                } else {
                    // 无建议时：显示该维度障碍度 top3 指标（若有 L4 结果）
                    const dimL4 = Object.entries(l4Results)
                        .filter(([k, v]) => v['维度'] === dim)
                        .sort((a, b) => (b[1]['障碍度'] || 0) - (a[1]['障碍度'] || 0))
                        .slice(0, 3);
                    if (dimL4.length > 0) {
                        listEl.innerHTML = dimL4.map(([name, r], i) => {
                            const cls = i === 0 ? cfg.cls : 'yellow';
                            const status = r['失分类型'] === '完整度失分' ? '能力尚未建立' : (r['值'] || '待完善');
                            return `<li><span class="dot ${cls}"></span>${name}：${status}</li>`;
                        }).join('');
                    } else {
                        listEl.innerHTML = '<li><span class="dot gray"></span>该维度暂无重点指标</li>';
                    }
                }
            }
        });

        console.log(`✅ [fillScoreCards] 完成：总分=${total} 等级=${level}`);
        // v=80：保存 scoreData 全局，供 buildPdfReport 使用（无需先调 previewReport）
        App._reportScoreData = scoreData;
    } catch (e) {
        console.error('❌ fillScoreCards 失败:', e);
    }
}

/** 总分 → 等级标签（A/B/C/D/E/F）*/
function getGradeLabel(total) {
    if (total >= 9.0) return 'A';
    if (total >= 7.0) return 'B';
    if (total >= 5.0) return 'C';
    if (total >= 3.0) return 'D';
    if (total >= 1.0) return 'E';
    return 'F';
}

function fillExtractCards(extractData) {
    // v=116 修复：先确保卡片骨架在位（删除报告时曾被 innerHTML='' 抹掉）
    try { ensureExtractResultDom(); } catch (e) { console.warn('提取结果骨架自愈失败:', e); }

    // ↑↑↑ 关键：dims 必须在 try 块外声明（块作用域陷阱）↑↑↑
    let dims = {};
    try {
        console.log('🎯 [fillExtractCards] 启动');
        console.log('   raw extractData:', JSON.stringify(extractData).slice(0, 500));

        // 兼容两种后端响应结构：{extracted: {...}} 或 {...}
        if (extractData && extractData.extracted && !extractData.报告元数据 && !extractData.指标提取结果) {
            console.log('   检测到 extracted 嵌套结构，自动解包');
            extractData = extractData.extracted;
        }

        if (!extractData || !extractData.指标提取结果) {
            console.warn('⚠️ extractData 为空，回退到 App.extractResult');
            extractData = window.App?.extractResult || extractData;
            if (extractData && extractData.extracted) {
                extractData = extractData.extracted;
            }
        }
        console.log('   报告元数据:', extractData?.报告元数据);
        dims = extractData.指标提取结果 || {};
        console.log('   维度列表:', Object.keys(dims));
        console.log('   企业内部碳管理维度内容:', dims['企业内部碳管理']);
    } catch (e) {
        console.error('❌ fillExtractCards 启动失败:', e);
        return;
    }

    // 获取指标节点
    const getNode = (dim, sub, name) => {
        const dimObj = dims[dim] || {};
        const subObj = (dimObj.指标 || {})[sub] || {};
        return subObj[name] || null;
    };

    // 填充值 + 原文
    let filledCount = 0;
    let missingCount = 0;
    try {
        L4_MAP.forEach((path, idx) => {
            const [dim, sub, name] = path;
            const node = getNode(dim, sub, name);
            const idxStr = (idx + 1).toString().padStart(2, '0');  // '01' '02' ... '42'
            const valEl = document.getElementById(`l4-${idxStr}`);
            const srcEl = document.getElementById(`l4-${idxStr}-src`);
            if (!node) {
                missingCount++;
                return;
            }
            const val = (node['值'] || '').toString().trim();
            const src = (node['原文'] || '').toString().trim();
            if (valEl) {
                valEl.textContent = (val && val !== '未披露' && val !== '—') ? val : '0';
                filledCount++;
            } else {
                console.warn(`l4-${idxStr} 元素不存在`);
            }
            if (srcEl) {
                srcEl.textContent = src ? `📌 ${src.slice(0, 80)}${src.length > 80 ? '…' : ''}` : '未披露';
            }
        });
    } catch (e) {
        console.error('❌ L4_MAP 遍历失败:', e);
    }
    console.log(`✅ fillExtractCards 完成：填充 ${filledCount} 个，跳过 ${missingCount} 个`);

    // Hero 区域卡片同步（若存在）
    const heroScope1 = document.getElementById('hero-scope1');
    const scope1Node = getNode('企业内部碳管理', '碳排放', '范围一排放');
    if (heroScope1 && scope1Node && scope1Node['值'] && scope1Node['值'] !== '未披露') {
        heroScope1.textContent = scope1Node['值'];
    }

    // 企业名称展示（若有标题元素则更新）
    const nameEl = document.getElementById('companyName') || document.querySelector('.hero-title strong');
    const meta = extractData.报告元数据 || {};
    if (nameEl && meta['企业名称']) {
        nameEl.textContent = meta['企业名称'];
    }
}

// 暴露到 window 方便调试
window.fillExtractCards = fillExtractCards;
window.fillScoreCards = fillScoreCards;
window.App = App;

/** 模拟分析（演示用） */
function simulateAnalysis(btn, status, progress, result) {
    const steps = [
        '正在解析报告文件...',
        '识别碳排放数据 (Scope 1/2/3)...',
        '提取供应链碳管理信息...',
        '分析透明度披露指标...',
        '识别碳金融相关信息...',
        '数据结构化处理完成！'
    ];

    let step = 0;
    const interval = setInterval(() => {
        if (step < steps.length) {
            status.textContent = steps[step];
            step++;
        }
    }, 600);

    setTimeout(() => {
        clearInterval(interval);
        progress.style.display = 'none';
        result.style.display = 'block';
        App.analysisDone = true;

        btn.disabled = false;
        btn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-9-9c2.5 0 4.8 1 6.4 2.6L21 8"/><path d="M21 3v5h-5"/></svg> 重新提取';

        showToast('数据提取完成！', 'success');

        // 显示评分区域
        $('#scoring').style.display = 'block';

        // 初始化图表
        setTimeout(() => {
            Charts.refresh();
            scrollToSection('analysis');
        }, 300);
    }, 3800);
}

// ============ Tab切换 ============
function initTabs() {
    $$('.extract-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            const target = tab.dataset.tab;
            $$('.extract-tab').forEach(t => t.classList.remove('active'));
            $$('.tab-panel').forEach(p => p.classList.remove('active'));
            tab.classList.add('active');
            $(`#tab-${target}`).classList.add('active');
        });
    });
}

// ============ 报告预览与下载 ============

/**
 * v=103：把文本强转为 JSON 对象（容错三连）
 *   1) 直接 JSON.parse
 *   2) 剥离 ```json 代码围栏后再 parse
 *   3) 截取首个 { 到最后一个 } 的主体；仍失败则「截断修复」——按括号平衡截到最后一个完整值并补齐
 *
 * 背景：服务端 raw_text 曾按 8000 字符硬截断，直接 JSON.parse 必然抛错，
 *       前端随后把整坨 JSON 当 Markdown 正文渲染 → 报告正文里出现一屏 JSON 源码。
 */
function coerceJsonObject(text) {
    if (!text || typeof text !== 'string') return null;
    const t = text.trim();
    if (!t) return null;
    const tryParse = s => { try { return JSON.parse(s); } catch (e) { return null; } };

    let obj = tryParse(t);
    if (obj) return obj;

    // 代码围栏 ```json ... ```
    if (t.indexOf('```') > -1) {
        const fenced = t.replace(/^[\s\S]*?```[a-zA-Z]*\s*/, '').replace(/```[\s\S]*$/, '').trim();
        obj = tryParse(fenced);
        if (obj) return obj;
        const repaired = repairTruncatedJson(fenced);
        if (repaired) { obj = tryParse(repaired); if (obj) return obj; }
    }

    // 取首个 { 到最后一个 } 的主体
    const a = t.indexOf('{');
    const b = t.lastIndexOf('}');
    if (a >= 0 && b > a) {
        obj = tryParse(t.slice(a, b + 1));
        if (obj) return obj;
    }
    // 截断修复（针对被切断的 JSON）
    const repaired = repairTruncatedJson(a >= 0 ? t.slice(a) : t);
    if (repaired) { obj = tryParse(repaired); if (obj) return obj; }
    return null;
}

/**
 * 修复被截断的 JSON：扫描到最后一个「括号平衡点」，截断后按**那一刻的**未闭合栈补齐 ] } ，
 * 让前面已完整的条目仍能被解析出来（例如 10 条建议被切在第 7 条中间 → 恢复前 6 条）。
 *
 * 注意：未闭合栈必须在 lastClose 那一刻做快照。若直接复用扫描结束后的栈，
 *       lastClose 之后新出现的 [ { 会被多补一个 ] }，拼出来仍是非法 JSON（v=103 已修）。
 */
function repairTruncatedJson(src) {
    if (!src || typeof src !== 'string') return null;
    let inStr = false, esc = false, lastClose = -1, snapshot = null;
    const stack = [];
    for (let i = 0; i < src.length; i++) {
        const c = src[i];
        if (inStr) {
            if (esc) esc = false;
            else if (c === '\\') esc = true;
            else if (c === '"') inStr = false;
            continue;
        }
        if (c === '"') { inStr = true; }
        else if (c === '{') { stack.push('}'); }
        else if (c === '[') { stack.push(']'); }
        else if (c === '}' || c === ']') {
            if (stack.length && stack[stack.length - 1] === c) {
                stack.pop();
                lastClose = i;
                snapshot = stack.slice();   // ← 关键：记录此刻的未闭合状态
            }
        }
    }
    if (lastClose < 0 || !snapshot || !snapshot.length) return null;   // 本就平衡 → 无需修复
    let cut = src.slice(0, lastClose + 1);
    while (snapshot.length) cut += snapshot.pop();
    return cut;
}

/** 从解析出来的对象里取出「改进建议 / 诊断摘要 / 企业画像」（兼容 Output.* 嵌套） */
function adviceFromObject(obj) {
    if (!obj || typeof obj !== 'object') return null;
    const pools = [obj, (obj['Output'] && typeof obj['Output'] === 'object') ? obj['Output'] : null];
    for (const p of pools) {
        if (!p) continue;
        if (Array.isArray(p['改进建议']) && p['改进建议'].length) {
            return {
                advs: p['改进建议'],
                summary: p['诊断摘要'] || '',
                profile: p['企业画像'] || ''
            };
        }
    }
    return null;
}

/** 判断一段文本是不是「JSON 源码」（用于兜底：残缺 JSON 绝不能当 Markdown 印进报告） */
function looksLikeJson(text) {
    if (!text || typeof text !== 'string') return false;
    const t = text.trim();
    if (!/^[\{\[]/.test(t)) return false;
    return /"\s*[^"]{1,30}"\s*:/.test(t.slice(0, 500));
}

/**
 * v=103：统一「建议载荷」解析 —— 预览、PDF 下载、pdfmake 兼容模式共用同一份结果
 * 优先级：scoreResult.suggestion（服务端已解析，未截断）→ 评分结果/finalreport → raw_text 容错
 * @returns {{advs: Array|null, summary: string, profile: string, md: string, source: string}}
 */
function parseAdvicePayload(scoreResult) {
    const out = { advs: null, summary: '', profile: '', md: '', source: 'none' };
    const sr = scoreResult || {};
    const candidates = [
        sr.suggestion,
        sr['评分结果'],
        sr.finalreport,
        (sr['Output'] && sr['Output'].finalreport) || null
    ];
    for (const c of candidates) {
        const r = adviceFromObject(c);
        if (r) { out.advs = r.advs; out.summary = r.summary; out.profile = r.profile; out.source = 'suggestion'; return out; }
    }
    const raw = sr.raw_text || '';
    if (raw.trim()) {
        const r = adviceFromObject(coerceJsonObject(raw));
        if (r) { out.advs = r.advs; out.summary = r.summary; out.profile = r.profile; out.source = 'raw_text(json)'; return out; }
        // 兜底：修复后仍是 JSON 形态（残缺/损坏）时，绝不把它当 Markdown 渲染，
        //       否则报告正文会整屏印出 JSON 源码 —— 这正是 v=103 之前线上报告的现象。
        if (looksLikeJson(raw)) {
            out.source = 'raw_text(broken-json)';
            return out;
        }
        // 真·自由文本才当 Markdown
        out.md = raw;
        out.source = 'raw_text(text)';
    }
    return out;
}

function previewReport() {
    const body = $('#reportModalBody');
    // v=103：统一解析建议载荷（data 构造、结构化渲染、PDF 复用同一份结果）
    const advPayload = parseAdvicePayload(App.scoreResult);

    // 数据源优先级（两段式渲染）：
    //   1. 评分增强数据（前端 Python 已算好，立即可用）
    //   2. App.scoreResult.评分结果（ADP 返回的建议，叠加）
    //   3. MOCK_DATA 兜底
    let data;
    let scoreData;
    let suggestions = [];
    const ext = (App.extractResult && App.extractResult.extracted) || App.extractResult || {};
    const preComputed = ext['评分增强数据'];   // 前端 Python 算好的分数
    const adpResult = (App.scoreResult && App.scoreResult.评分结果) || {};

    if (API_CONFIG.MODE === 'live' && (preComputed || App.scoreResult)) {
        scoreData = preComputed || {};
        // 企业名称/年份：从提取结果元数据取
        const meta = ext['报告元数据'] || {};
        data = {
            name: meta['企业名称'] || '待评估企业',
            reportYear: meta['报告年份'] || '-',
            score: {
                overall: scoreData['总分'] || 0,
                internal: (scoreData['维度得分'] || {})['企业内部碳管理'] || 0,
                supply: (scoreData['维度得分'] || {})['供应链碳管理'] || 0,
                disclosure: (scoreData['维度得分'] || {})['透明度'] || 0,
                finance: (scoreData['维度得分'] || {})['金融化情况'] || 0
            },
            grade: scoreData['评分等级'] || '-',
            gradeLabel: scoreData['评分等级'] || '-',
            carbonData: {}
        };
        // 建议：v=103 统一解析（结构化 suggestion 优先 → raw_text 容错 → Top10 模板兜底）
        if (advPayload.advs && advPayload.advs.length > 0) {
            suggestions = advPayload.advs;
        } else if (advPayload.md) {
            suggestions = [{ __adp_raw_text: advPayload.md }];
        } else if (adpResult.改进建议 && adpResult.改进建议.length > 0) {
            suggestions = adpResult.改进建议;
        } else if (preComputed && preComputed['障碍度Top10']) {
            // ADP 完全没接通：用 Python 算的 Top10 障碍度生成简短建议
            suggestions = preComputed['障碍度Top10'].map((item, i) => {
                const l4 = (preComputed['L4计算结果'] || {})[item['指标']] || {};
                const rank = i + 1;
                const ind = item['指标'];
                const loss = item['失分类型'] || '需关注';
                const actionMap = {
                    '完整度失分': `建立该能力项的管理机制与数据采集口径（当前能力尚未建立）`,
                    '质量失分': `建立年度台账与连续追踪机制，把定性动作固化为可度量管理动作`,
                    '对标失分': `对标行业标杆企业的能力水平，补齐差距项`
                };
                return {
                    优先级: rank <= 3 ? '高' : (rank <= 7 ? '中' : '低'),
                    指标: ind,
                    失分原因: loss,
                    建议: actionMap[loss] || `关注${ind}指标改善`
                };
            });
        }
    } else {
        data = MOCK_DATA.currentCompany;
        suggestions = [
            '提高供应链碳排放核查覆盖率至90%以上',
            '推动更多供应商签署碳减排目标',
            '将Scope 3审计范围扩大至全部类别',
            '增加CCER项目开发数量',
            '提高碳减排目标至2030年40%'
        ];
    }

    // 生成改进建议列表（v=103：结构化优先 / 真 Markdown 次之 / 模板兜底）
    let suggestionHtml;
    let adpRawTextForPdf = '';
    let structuredAdvs = advPayload.advs || null;        // 结构化建议数组
    let structuredSummary = advPayload.summary || '';    // 诊断摘要
    let structuredProfile = advPayload.profile || '';    // v=101：企业画像（"这是一家什么企业"）
    const escapeHtml = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');  // v=80：提到顶部，三个分支共用

    if (structuredAdvs && structuredAdvs.length) {
        // 结构化渲染（v=103：补齐企业特征/现状/同行对照/改进收益/适用性/证据来源，与 PDF 模板对齐）
        const summaryHtml = structuredSummary
            ? `<div class="adp-summary">${escapeHtml(structuredSummary)}</div>`
            : '';
        const profileHtml = structuredProfile
            ? `<div class="adp-summary" style="border-left-color:#34d399;"><b style="color:#047857;">企业画像：</b>${escapeHtml(structuredProfile)}</div>`
            : '';
        const itemsHtml = structuredAdvs.map((s, i) => `
            <div class="adp-item">
                <div class="adp-item-head">
                    <span class="adp-item-idx">${i + 1}</span>
                    <span class="adp-item-name">${escapeHtml(s['指标'] || '指标')}</span>
                    ${s['适用性'] ? `<span class="adp-item-pri" style="background:#ecfdf5;border:1px solid #a7f3d0;color:#047857;">${escapeHtml(s['适用性'])}</span>` : ''}
                    <span class="adp-item-pri adp-pri-${escapeHtml((s['优先级'] || '中').toLowerCase())}">${escapeHtml(s['优先级'] || '中')}</span>
                </div>
                ${(s['失分类型'] || s['能力判据']) ? `<div class="adp-item-meta">${s['失分类型'] ? `能力缺口类型：<b>${escapeHtml(s['失分类型'])}</b>` : ''}${(s['失分类型'] && s['能力判据']) ? '　·　' : ''}${s['能力判据'] ? `判据级别：<b>${escapeHtml(s['能力判据'])}</b>` : ''}</div>` : ''}
                ${s['企业特征'] ? `<div class="adp-item-row"><span class="adp-row-label">企业特征：</span>${escapeHtml(s['企业特征'])}</div>` : ''}
                ${s['现状']     ? `<div class="adp-item-row"><span class="adp-row-label">能力表现：</span>${escapeHtml(s['现状'])}</div>` : ''}
                ${s['同行对照'] ? `<div class="adp-item-row"><span class="adp-row-label">标杆对照：</span>${escapeHtml(s['同行对照'])}</div>` : ''}
                ${s['根源分析'] ? `<div class="adp-item-row"><span class="adp-row-label">能力缺口成因：</span>${escapeHtml(s['根源分析'])}</div>` : ''}
                ${s['建议']     ? `<div class="adp-item-row"><span class="adp-row-label">能力建设路径：</span>${escapeHtml(s['建议'])}</div>` : ''}
                ${s['改进收益'] ? `<div class="adp-item-row" style="background:#f0fdf4;border-left:3px solid #34d399;padding:2px 8px;border-radius:4px;"><span class="adp-row-label" style="color:#047857;">能力补齐后水平：</span>${escapeHtml(s['改进收益'])}</div>` : ''}
                ${s['验证理由'] ? `<div class="adp-item-row"><span class="adp-row-label adp-row-why">为何有效：</span>${escapeHtml(s['验证理由'])}</div>` : ''}
                ${(s['证据来源'] || s['事实基础']) ? `<div class="adp-item-meta" style="color:#94a3b8;">${s['事实基础'] ? `事实基础：${escapeHtml(s['事实基础'] === '报告未披露' ? '报告未体现' : s['事实基础'])}` : ''}${(s['事实基础'] && s['证据来源']) ? '　' : ''}${s['证据来源'] ? `证据来源：${escapeHtml(Array.isArray(s['证据来源']) ? s['证据来源'].join('；') : s['证据来源'])}` : ''}</div>` : ''}
            </div>
        `).join('');
        suggestionHtml = `<div class="adp-report adp-report-structured">${profileHtml}${summaryHtml}<div class="adp-list">${itemsHtml}</div></div>`;
    } else if (advPayload.md || (suggestions.length === 1 && suggestions[0] && suggestions[0].__adp_raw_text)) {
        // Markdown 渲染：仅当确认是自由文本（非 JSON）时才走这里
        const rawText = advPayload.md || suggestions[0].__adp_raw_text;
        adpRawTextForPdf = rawText;
        let mdHtml;
        try {
            if (typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
                mdHtml = DOMPurify.sanitize(marked.parse(rawText, { gfm: true, breaks: true }));
            } else {
                mdHtml = rawText.split('\n').map(l => `<p>${escapeHtml(l)}</p>`).join('');
            }
        } catch (e) {
            console.error('Markdown 渲染失败，回退纯文本:', e);
            mdHtml = rawText.split('\n').map(l => `<p>${escapeHtml(l)}</p>`).join('');
        }
        suggestionHtml = `<div class="adp-report">${mdHtml}</div>`;
    } else if (suggestions.length > 0) {
        suggestionHtml = suggestions.map(s => {
            if (typeof s === 'string') return `                    <li>• ${s}</li>`;
            return `                    <li>• ${s.建议 || s.建议内容 || ''}</li>`;
        }).join('\n');
    } else {
        suggestionHtml = '                    <li>暂无改进建议</li>';
    }

    body.innerHTML = `
        <div style="padding: 8px 0;">
            <div style="text-align:center; margin-bottom: 32px;">
                <h2 style="font-size:24px; margin-bottom:8px;">碳治理能力画像报告</h2>
                <p style="color:var(--color-text-muted);">${escapeHtml(data.name)} · ${escapeHtml(data.reportYear)}年度ESG报告</p>
            </div>

            <div style="display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin-bottom:32px;">
                <div style="text-align:center; padding:16px; background:var(--color-green-bg); border-radius:10px;">
                    <div style="font-size:32px; font-weight:900; color:var(--color-green);">${typeof data.score.overall === 'number' ? data.score.overall.toFixed(2) : data.score.overall}</div>
                    <div style="font-size:13px; color:var(--color-text-muted);">综合评分</div>
                </div>
                <div style="text-align:center; padding:16px; background:var(--color-blue-bg); border-radius:10px;">
                    <div style="font-size:32px; font-weight:900; color:var(--color-blue);">${typeof data.score.internal === 'number' ? data.score.internal.toFixed(2) : data.score.internal}</div>
                    <div style="font-size:13px; color:var(--color-text-muted);">内部碳管理</div>
                </div>
                <div style="text-align:center; padding:16px; background:var(--color-teal-bg); border-radius:10px;">
                    <div style="font-size:32px; font-weight:900; color:var(--color-teal);">${typeof data.score.supply === 'number' ? data.score.supply.toFixed(2) : data.score.supply}</div>
                    <div style="font-size:13px; color:var(--color-text-muted);">供应链管理</div>
                </div>
                <div style="text-align:center; padding:16px; background:var(--color-yellow-bg); border-radius:10px;">
                    <div style="font-size:32px; font-weight:900; color:var(--color-yellow);">${typeof data.score.disclosure === 'number' ? data.score.disclosure.toFixed(2) : data.score.disclosure}</div>
                    <div style="font-size:13px; color:var(--color-text-muted);">透明度披露</div>
                </div>
            </div>

            <div style="margin-bottom:24px;">
                <h3 style="font-size:16px; margin-bottom:12px;">四维度能力雷达图</h3>
                <div style="height:320px; position:relative;">
                    <canvas id="reportRadar"></canvas>
                </div>
            </div>

            <div style="margin-bottom:24px;">
                <h3 style="font-size:16px; margin-bottom:12px;">评估结论</h3>
                <p style="font-size:14px; color:var(--color-text-secondary); line-height:1.8;">
                    该企业碳治理能力整体处于<span style="color:var(--color-green); font-weight:700;">${data.grade || '待评估'}级</span>水平。
                    综合评分 <span style="font-weight:700;">${typeof data.score.overall === 'number' ? data.score.overall.toFixed(2) : data.score.overall}</span> 分，
                    内部碳管理 ${typeof data.score.internal === 'number' ? data.score.internal.toFixed(2) : data.score.internal} 分，
                    供应链碳管理 ${typeof data.score.supply === 'number' ? data.score.supply.toFixed(2) : data.score.supply} 分，
                    透明度披露 ${typeof data.score.disclosure === 'number' ? data.score.disclosure.toFixed(2) : data.score.disclosure} 分，
                    碳金融转化 ${typeof data.score.finance === 'number' ? data.score.finance.toFixed(2) : data.score.finance} 分。
                </p>
            </div>

            <div style="margin-bottom:24px;">
                <h3 style="font-size:16px; margin-bottom:12px;">改进建议</h3>
                <ul style="list-style:none; font-size:14px; color:var(--color-text-secondary); line-height:2;">
${suggestionHtml}
                </ul>
            </div>

            <div style="text-align:center; padding-top:24px; border-top:1px solid var(--color-border); font-size:12px; color:var(--color-text-muted);">
                报告生成时间：${new Date().toLocaleString('zh-CN')} | 本报告由系统自动生成，数据仅供参考
            </div>
        </div>
    `;

    // 存全局供 downloadReport(pdfmake) 使用
    App._reportScoreData = scoreData;
    App._adpRawText = adpRawTextForPdf;

    openModal('reportModal');

    // 渲染报告内雷达图（真实维度得分；PDF 导出时 toDataURL 复用）
    renderReportRadar(scoreData);

    // 分析完成首次打开报告 → 保存历史到 Supabase（已登录用户）
    if (App._historySaveToken && !App._historySavedForToken) {
        App._historySavedForToken = true;
        saveHistoryToSupabase();
    }
}

/**
 * 渲染报告模态框内的雷达图（reportRadar canvas）
 * 数据：评分增强数据.维度得分（Python 已算好）
 */
function renderReportRadar(scoreData) {
    const canvas = document.getElementById('reportRadar');
    if (!canvas || typeof Chart === 'undefined') return;
    const dims = (scoreData && scoreData['维度得分']) || {};
    const data = [
        dims['企业内部碳管理'] ?? 0,
        dims['供应链碳管理'] ?? 0,
        dims['透明度'] ?? 0,
        dims['金融化情况'] ?? 0
    ];
    // 销毁旧实例（重复打开时）
    if (window.__reportRadarInst) window.__reportRadarInst.destroy();
    window.__reportRadarInst = new Chart(canvas, {
        type: 'radar',
        data: {
            labels: ['企业内部碳管理', '供应链碳管理', '透明度披露', '碳金融转化'],
            datasets: [{
                label: '当前企业',
                data: data,
                backgroundColor: 'rgba(16, 185, 129, 0.2)',
                borderColor: 'rgba(16, 185, 129, 1)',
                borderWidth: 2.5,
                pointBackgroundColor: 'rgba(16, 185, 129, 1)',
                pointBorderColor: '#fff',
                pointBorderWidth: 2,
                pointRadius: 5
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false }, tooltip: { enabled: true } },
            scales: {
                r: {
                    min: 0, max: 10,
                    ticks: { stepSize: 2, font: { size: 11 }, color: '#94a3b8', backdropColor: 'transparent' },
                    grid: { color: 'rgba(0,0,0,0.06)' },
                    angleLines: { color: 'rgba(0,0,0,0.06)' },
                    pointLabels: { font: { size: 13, weight: '600' }, color: '#1a2332' }
                }
            }
        }
    });
}

/**
 * Markdown → pdfmake 节点转换器（支持：标题/段落/列表/表格/粗体/行内代码/引用/分隔线）
 * ADP 报告是 Markdown，PDF 用 pdfmake 原生绘制，保持专业排版
 */
function mdToPdfMake(text) {
    if (!text) return [];
    const nodes = [];
    const lines = text.split('\n');
    const escapePdf = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    // 行内解析：**bold** *italic* `code`
    function inlineParse(str) {
        const out = [];
        const regex = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g;
        let last = 0; let m;
        while ((m = regex.exec(str)) !== null) {
            if (m.index > last) out.push({ text: escapePdf(str.slice(last, m.index)) });
            const tok = m[0];
            if (tok.startsWith('**')) out.push({ text: escapePdf(tok.slice(2, -2)), bold: true });
            else if (tok.startsWith('`')) out.push({ text: escapePdf(tok.slice(1, -1)), font: 'Courier', fontSize: 8.5, background: '#f3f4f6' });
            else out.push({ text: escapePdf(tok.slice(1, -1)), italics: true });
            last = m.index + tok.length;
        }
        if (last < str.length) out.push({ text: escapePdf(str.slice(last)) });
        return out.length === 0 ? escapePdf(str) : out;
    }

    let i = 0;
    let pendingList = null;   // {type: 'ul'|'ol', items: []}
    let tableRows = null;     // 正在收集的表格行
    let tableHeader = null;

    function flushList() {
        if (pendingList && pendingList.items.length) {
            nodes.push({
                [pendingList.type === 'ol' ? 'ol' : 'ul']: pendingList.items.map(item =>
                    typeof item === 'string' ? inlineParse(item) : item
                ),
                margin: [0, 3, 0, 6]
            });
        }
        pendingList = null;
    }
    function flushTable() {
        if (tableRows && tableRows.length > 1) {
            const body = [tableHeader].concat(tableRows).map(row =>
                row.map(cell => ({ text: typeof cell === 'string' ? inlineParse(cell) : cell, margin: [2, 2] }))
            );
            nodes.push({
                table: { headerRows: 1, widths: Array(tableHeader.length).fill('*'), body },
                layout: {
                    fillColor: (rowIdx) => rowIdx === 0 ? '#eff6ff' : (rowIdx % 2 === 0 ? '#fafafa' : null),
                    hLineColor: () => '#d1d5db',
                    vLineColor: () => '#e5e7eb',
                    hLineWidth: () => 0.5,
                    vLineWidth: () => 0.5
                },
                margin: [0, 5, 0, 10]
            });
        }
        tableRows = null; tableHeader = null;
    }

    for (; i < lines.length; i++) {
        const line = lines[i];
        const trimmed = line.trim();

        // 表格：| a | b |
        if (/^\|.*\|$/.test(trimmed)) {
            const cells = trimmed.split('|').slice(1, -1).map(c => c.trim());
            // 表头分隔行 |---|---| 跳过
            if (/^[-: ]+$/.test(cells.join(''))) continue;
            if (!tableHeader) { tableHeader = cells; tableRows = []; }
            else tableRows.push(cells);
            continue;
        } else {
            flushTable();
        }

        // 标题
        const hMatch = /^(#{1,4})\s+(.*)$/.exec(trimmed);
        if (hMatch) {
            flushList();
            const level = hMatch[1].length;
            nodes.push({
                text: inlineParse(hMatch[2]),
                fontSize: [17, 15, 13, 12][level - 1],
                bold: true,
                color: level <= 2 ? '#1a3a6e' : '#111827',
                margin: [0, level <= 2 ? 14 : 10, 0, 5]
            });
            continue;
        }

        // 列表项
        const ulMatch = /^[-*]\s+(.*)$/.exec(trimmed);
        const olMatch = /^\d+[.)]\s+(.*)$/.exec(trimmed);
        if (ulMatch || olMatch) {
            flushTable();
            if (!pendingList) pendingList = { type: ulMatch ? 'ul' : 'ol', items: [] };
            // 子列表（缩进）
            const indent = line.search(/\S/);
            pendingList.items.push({
                text: inlineParse((ulMatch || olMatch)[1]),
                margin: [indent > 0 ? 12 : 0, 1, 0, 1]
            });
            continue;
        } else {
            flushList();
        }

        // 引用
        if (/^>\s?/.test(trimmed)) {
            nodes.push({
                text: inlineParse(trimmed.replace(/^>\s?/, '')),
                italics: true,
                color: '#6b7280',
                margin: [8, 3, 0, 6],
                background: '#f9fafb'
            });
            continue;
        }

        // 分隔线
        if (/^---+\s*$/.test(trimmed) || /^\*\*\*+\s*$/.test(trimmed)) {
            flushList(); flushTable();
            nodes.push({
                canvas: [{ type: 'line', x1: 0, y1: 0, x2: 500, y2: 0, lineWidth: 0.6, lineColor: '#d1d5db' }],
                margin: [0, 6, 0, 10]
            });
            continue;
        }

        // 空行：跳过
        if (trimmed === '') { flushList(); continue; }

        // 段落
        nodes.push({ text: inlineParse(trimmed), margin: [0, 2, 0, 6], lineHeight: 1.5 });
    }
    flushList();
    flushTable();
    return nodes;
}

/**
 * v=92：pdfmake + vfs_cn（中文字体）懒加载
 *
 * 背景：这两个库合计约 9.8MB（pdfmake.min.js 1.3MB + vfs_cn.js 8.5MB），
 *      仅在本函数所在的「客户端兜底导出」路径使用（主路径走 /api/report 的
 *      WeasyPrint 后端渲染）。原先在 <head> 同步阻塞加载，导致首屏白白
 *      多传 9.8MB —— 在隧道吞吐仅 ~115KB/s 的情况下就是 80 秒起步。
 * 现改为：首次真正需要时才注入 script，加载一次后长期驻留。
 */
let _pdfLibsPromise = null;
function ensurePdfLibs() {
    // 已就绪（含 vfs 字体虚拟文件系统）直接返回
    if (typeof pdfMake !== 'undefined' && pdfMake.vfs) return Promise.resolve();
    if (_pdfLibsPromise) return _pdfLibsPromise;

    const V = '?v=92';
    const inject = (src) => new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = src;
        s.async = false;   // 保证 pdfmake 先于 vfs_cn 执行（vfs_cn 依赖 pdfMake）
        s.onload = () => resolve();
        s.onerror = () => reject(new Error('资源加载失败: ' + src));
        document.head.appendChild(s);
    });

    _pdfLibsPromise = inject('vendor/pdfmake.min.js' + V)
        .then(() => inject('vendor/vfs_cn.js' + V))
        .catch(err => {
            _pdfLibsPromise = null;   // 失败后允许重试
            throw err;
        });

    return _pdfLibsPromise;
}

/**
 * 构建 PDF 报告（pdfmake 原生绘制）
 * 结构：评分总览表 + 雷达图 + 评估结论 + ADP 优化建议
 * v=92：改为 async —— 入口先 await 懒加载 pdfmake 与中文字体
 */
async function buildPdfReport() {
    // 首次导出需要现下 9.8MB 组件，给个提示避免用户以为卡死
    if (typeof pdfMake === 'undefined' && !_pdfLibsPromise) {
        showToast('首次导出需加载 PDF 组件（约 9.8MB），请稍候…', 'info');
    }
    try {
        await ensurePdfLibs();
    } catch (e) {
        console.error('PDF 组件加载失败，回退打印:', e);
        showToast('PDF 组件加载失败，回退打印', 'error');
        window.print();
        return;
    }
    if (typeof pdfMake === 'undefined') {
        showToast('PDF 库未加载，回退打印', 'error');
        window.print();
        return;
    }
    // 配置中文字体（vfs_cn.js 提供思源黑体子集）
    pdfMake.fonts = {
        NotoSansSC: {
            normal: 'NotoSansSC-Regular.ttf',
            bold: 'NotoSansSC-Bold.ttf',
            italics: 'NotoSansSC-Regular.ttf',
            bolditalics: 'NotoSansSC-Bold.ttf'
        }
    };

    // 数据
    const scoreData = App._reportScoreData || {};
    const ext = (App.extractResult && App.extractResult.extracted) || App.extractResult || {};
    const meta = ext['报告元数据'] || {};
    const dims = scoreData['维度得分'] || {};
    const total = scoreData['总分'] ?? 0;
    const level = scoreData['评分等级'] || '-';
    const company = meta['企业名称'] || '待评估企业';
    const year = meta['报告年份'] || '-';

    // 雷达图 → 图片（复用 reportRadar canvas，Chart.js 已渲染）
    let radarImg = null;
    const radarCanvas = document.getElementById('reportRadar');
    if (radarCanvas) {
        try { radarImg = radarCanvas.toDataURL('image/png'); } catch (e) { radarImg = null; }
    }

    // 评估结论
    const sortedDims = Object.entries(dims).sort((a, b) => b[1] - a[1]);
    const strongest = sortedDims[0] || ['-', 0];
    const weakest = sortedDims[sortedDims.length - 1] || ['-', 0];

    // 评分总览表
    const scoreTable = {
        table: {
            headerRows: 1,
            widths: ['*', '*', '*'],
            body: [
                [{ text: '维度', bold: true }, { text: '得分', bold: true }, { text: '说明', bold: true }],
                [{ text: '综合评分', bold: true }, { text: (typeof total === 'number' ? total.toFixed(2) : total), bold: true }, { text: level, color: '#059669', bold: true }],
                ['企业内部碳管理', (dims['企业内部碳管理'] ?? 0).toFixed(2), ''],
                ['供应链碳管理', (dims['供应链碳管理'] ?? 0).toFixed(2), ''],
                ['透明度', (dims['透明度'] ?? 0).toFixed(2), ''],
                ['金融化情况', (dims['金融化情况'] ?? 0).toFixed(2), '']
            ]
        },
        layout: {
            fillColor: (rowIdx) => rowIdx === 0 ? '#eff6ff' : (rowIdx % 2 === 0 ? '#fafafa' : null),
            hLineColor: () => '#d1d5db', vLineColor: () => '#e5e7eb',
            hLineWidth: () => 0.5, vLineWidth: () => 0.5
        }
    };

    // 优化建议 → pdfmake 节点（v=103：统一解析；结构化字段补齐，且永不再把 JSON 源码当正文）
    let adpNodes = [];
    const advPayloadPdf = parseAdvicePayload(App.scoreResult);
    const structuredAdv = (advPayloadPdf.advs && advPayloadPdf.advs.length) ? advPayloadPdf.advs : null;
    if (structuredAdv) {
        adpNodes = structuredAdv.map((s, i) => ({
            stack: [
                { text: `${i + 1}. ${s['指标'] || '指标'}（${s['适用性'] ? s['适用性'] + ' · ' : ''}${s['优先级'] || '-'}优先级）`, bold: true, margin: [0, 6, 0, 2] },
                { text: `${s['能力判据'] ? `判据级别：${s['能力判据']}　·　` : ''}能力缺口类型：${s['失分类型'] || '—'}`, fontSize: 9, color: '#6b7280', margin: [0, 0, 0, 1] },
                ...(s['企业特征'] ? [{ text: `企业特征：${s['企业特征']}`, margin: [0, 0, 0, 2] }] : []),
                ...(s['现状'] ? [{ text: `能力表现：${s['现状']}`, margin: [0, 0, 0, 2] }] : []),
                ...(s['同行对照'] ? [{ text: `标杆对照：${s['同行对照']}`, margin: [0, 0, 0, 2] }] : []),
                { text: `能力缺口成因：${s['根源分析'] || '—'}`, margin: [0, 0, 0, 2] },
                { text: `能力建设路径：${s['建议'] || '—'}`, margin: [0, 0, 0, 2] },
                ...(s['改进收益'] ? [{ text: `能力补齐后水平：${s['改进收益']}`, color: '#047857', margin: [0, 0, 0, 2] }] : []),
                ...(s['验证理由'] ? [{ text: `为何有效：${s['验证理由']}`, fontSize: 9, color: '#6b7280', margin: [0, 0, 0, 4] }] : [])
            ]
        }));
    } else if (advPayloadPdf.md) {
        adpNodes = mdToPdfMake(advPayloadPdf.md);
    }
    const fallbackNodes = adpNodes.length ? [] : [{
        text: '（分析建议尚未生成，可重新分析获取建议）', color: '#9ca3af', margin: [0, 4, 0, 6]
    }];

    const docDefinition = {
        pageSize: 'A4',
        pageMargins: [45, 50, 45, 45],
        defaultStyle: { font: 'NotoSansSC', fontSize: 10, lineHeight: 1.5 },
        info: { title: `碳治理画像报告-${company}`, author: 'ESG碳治理能力画像系统' },
        header: (currentPage) => ({
            text: '碳治理能力画像报告',
            alignment: 'right', margin: [45, 15, 45, 0], fontSize: 8, color: '#9ca3af'
        }),
        footer: (currentPage, pageCount) => ({
            text: `第 ${currentPage} 页 / 共 ${pageCount} 页   生成时间：${new Date().toLocaleString('zh-CN')}`,
            alignment: 'center', margin: [0, 8, 0, 0], fontSize: 8, color: '#9ca3af'
        }),
        content: [
            // 标题
            { text: '碳治理能力画像报告', fontSize: 22, bold: true, alignment: 'center', margin: [0, 5, 0, 4] },
            { text: `${company} · ${year}年度ESG报告`, alignment: 'center', color: '#6b7280', margin: [0, 0, 0, 22] },
            // 一、评分总览
            { text: '一、评分总览', fontSize: 14, bold: true, color: '#1a3a6e', margin: [0, 0, 0, 8] },
            scoreTable,
            // 二、雷达图
            ...(radarImg ? [
                { text: '二、四维度能力雷达图', fontSize: 14, bold: true, color: '#1a3a6e', margin: [0, 20, 0, 8] },
                { image: radarImg, width: 480, alignment: 'center', margin: [0, 0, 0, 8] }
            ] : []),
            // 三、评估结论
            { text: '三、评估结论', fontSize: 14, bold: true, color: '#1a3a6e', margin: [0, 20, 0, 8] },
            {
                text: [
                    { text: `该企业碳治理能力整体处于 ${level} 水平。综合评分 ${(typeof total === 'number' ? total.toFixed(2) : total)} 分。` },
                    { text: `其中 ${strongest[0]} 的碳治理能力相对扎实（${(strongest[1] ?? 0).toFixed(2)} 分），` },
                    { text: `${weakest[0]} 的碳治理能力存在明显缺口（${(weakest[1] ?? 0).toFixed(2)} 分），是能力建设的优先方向。` }
                ],
                lineHeight: 1.7, margin: [0, 2, 0, 6]
            },
            // 四、优化建议（智能分析）
            { text: '四、优化建议（智能分析）', fontSize: 14, bold: true, color: '#1a3a6e', margin: [0, 20, 0, 8] },
            ...(adpNodes.length ? adpNodes : fallbackNodes)
        ]
    };

    pdfMake.createPdf(docDefinition).download(`碳治理画像报告-${company}.pdf`);
}

/**
 * v=103：离屏渲染高清雷达图（供 WeasyPrint 报告使用）
 *
 * 为什么不再直接用页面上的 #reportRadar canvas：
 *   页面 canvas 受 CSS 高度（320px）限制，toDataURL 出来的位图偏小，
 *   在 A4 上按满宽缩放后线条与刻度发虚。这里按 1000×860 离屏出图，
 *   缩到 A4 正文宽（约 17.8cm）时仍有约 2 倍像素密度。
 *
 * @param {Object} sd 评分增强数据（含 维度得分）
 * @returns {string} base64 dataURL；失败返回 ''
 */
function _renderRadarChart(sd) {
    try {
        if (typeof Chart === 'undefined') return '';
        const dims = (sd && sd['维度得分']) || {};
        const cv = document.createElement('canvas');
        cv.width = 1000;
        cv.height = 860;
        const chart = new Chart(cv, {
            type: 'radar',
            data: {
                labels: ['企业内部碳管理', '供应链碳管理', '透明度披露', '碳金融转化'],
                datasets: [{
                    label: '当前企业',
                    data: [
                        dims['企业内部碳管理'] ?? 0,
                        dims['供应链碳管理'] ?? 0,
                        dims['透明度'] ?? 0,
                        dims['金融化情况'] ?? 0
                    ],
                    backgroundColor: 'rgba(16, 185, 129, 0.22)',
                    borderColor: 'rgba(4, 120, 87, 1)',
                    borderWidth: 4,
                    pointBackgroundColor: 'rgba(4, 120, 87, 1)',
                    pointBorderColor: '#fff',
                    pointBorderWidth: 3,
                    pointRadius: 8
                }]
            },
            options: {
                responsive: false,
                animation: false,
                plugins: {
                    legend: { display: false },
                    tooltip: { enabled: false },
                    title: {
                        display: true, text: '四维度碳治理能力（满分 10 分）',
                        font: { size: 26, weight: '700' }, color: '#111827', padding: { bottom: 8 }
                    }
                },
                scales: {
                    r: {
                        min: 0, max: 10,
                        ticks: { stepSize: 2, font: { size: 18 }, color: '#94a3b8', showLabelBackdrop: false },
                        grid: { color: 'rgba(0,0,0,0.10)', lineWidth: 1.5 },
                        angleLines: { color: 'rgba(0,0,0,0.10)', lineWidth: 1.5 },
                        pointLabels: { font: { size: 22, weight: '600' }, color: '#1a2332', padding: 14 }
                    }
                }
            }
        });
        chart.update('none');
        const url = cv.toDataURL('image/png');
        chart.destroy();
        return url;
    } catch (e) {
        console.warn('v=103 雷达图离屏渲染失败:', e);
        return '';
    }
}

/**
 * v=100：生成报告内图表（离屏 canvas → base64 PNG）
 *
 * 为什么用离屏 + animation:false：
 *   Chart.js 未完成渲染时 toDataURL() 会拿到白图/半成品。页面上的图表可能没被滚动到
 *   （懒渲染）或已被销毁，所以这里临时建 canvas、关动画、强制 update('none') 后再取图。
 *   尺寸按 A4 正文宽的 2 倍输出，在报告里 CSS 缩到 100% 宽度 = 2x 清晰度。
 *
 * @param {Object} sd 评分增强数据（含 L4计算结果 / 障碍度Top10）
 * @returns {{obstacle_img: string, benchmark_img: string}}
 */
function _renderReportCharts(sd) {
    const out = { obstacle_img: '', benchmark_img: '' };
    if (typeof Chart === 'undefined') return out;
    const l4 = (sd && sd['L4计算结果']) || {};
    const top10 = Array.isArray(sd && sd['障碍度Top10']) ? sd['障碍度Top10'] : [];
    if (!top10.length && !Object.keys(l4).length) return out;

    const short = (s, n) => (s && s.length > n) ? s.slice(0, n) + '…' : (s || '');
    // 失分类型配色（与页面状态色一致）
    const LOSE_COLOR = {
        '完整度失分': 'rgba(239, 68, 68, 0.85)',
        '质量失分': 'rgba(245, 158, 11, 0.85)',
        '对标失分': 'rgba(59, 130, 246, 0.85)'
    };

    // ---- 图1：障碍度 Top10（横向条形，越长越拖后腿） ----
    // v=107 说明：此处刻意不加 minBarLength —— 障碍度 0 意味着「该指标不是缺口」，
    // 若也画成短条会让人误读为「存在缺口」，与图 3 的零值语义正好相反（图 3 的 0 是「有数据且为 0」）。
    try {
        const rows = top10.slice(0, 10);
        if (rows.length) {
            const cv = document.createElement('canvas');
            cv.width = 1200;
            cv.height = 130 + rows.length * 46;
            const chart = new Chart(cv, {
                type: 'bar',
                data: {
                    labels: rows.map(r => short(r['指标'], 16)),
                    datasets: [{
                        label: '障碍度',
                        data: rows.map(r => Number(r['障碍度']) || 0),
                        backgroundColor: rows.map(r => LOSE_COLOR[r['失分类型']] || 'rgba(16,185,129,0.85)'),
                        borderWidth: 0
                    }]
                },
                options: {
                    indexAxis: 'y',
                    responsive: false,
                    animation: false,
                    plugins: {
                        legend: { display: false },
                        title: { display: true, text: '障碍度 Top10（条形越长 = 越拖累总分）', font: { size: 22, weight: '700' }, color: '#111827' },
                        tooltip: { enabled: false }
                    },
                    scales: {
                        x: { beginAtZero: true, ticks: { font: { size: 15 }, color: '#6b7280' }, grid: { color: 'rgba(0,0,0,0.06)' } },
                        y: { ticks: { font: { size: 17 }, color: '#1f2937' }, grid: { display: false } }
                    }
                }
            });
            chart.update('none');
            out.obstacle_img = cv.toDataURL('image/png');
            chart.destroy();
        }
    } catch (e) { console.warn('v=100 障碍度图生成失败:', e); }

    // ---- 图3：本企业 vs 行业水平（v=103 改横向分组条形；v=112 修口径错配） ----
    // 为什么改横向：原纵向柱状图 8 个中文指标名只能 45° 斜排，A4 上缩印后字小到看不清；
    // 横向布局把指标名放左侧水平排列，条目高度可撑开，缩印后依然清楚。
    //
    // v=112 口径修复（核心）：灰条原先取 l4[k]['行业均值']，那是【基础分】口径（0-6），
    //   而绿条是【L4得分】（0-10）—— 两个量纲放在同一根轴上，比出来的高低没有意义。
    //   症状：零披露、按保底规则拿 1 分的指标（如绿色信贷/绿色债券），绿条反而长于灰条，
    //   看起来"领先行业"，与事实相反。
    //   现改用后端派生字段「行业L4得分」（= 行业基础分均值按同一套对标规则换算到 0-10），
    //   与报告正文「达行业均值→X」那一列同源 ⇒ 图文口径一致。
    //   预期结果：行业全员未披露的指标，两条等长（= 同处保底线 1.0），
    //   语义是"本行业普遍尚未建立"，而非"企业领先"。
    try {
        const keys = top10.slice(0, 8).map(r => r['指标']).filter(k => k && l4[k]);
        if (keys.length) {
            // v=113：「未披露」分两种，标签必须区分开 ——
            //   ① 行业普遍未披露（行业均值 < 0.5）→ 对标分走兜底档记 1 → L4 = 1.0，与保底线齐平；
            //   ② 行业已披露而本企业缺位（行业均值 ≥ 0.5）→ 基础分 0 < 行业均值×0.6 → 对标分 0
            //      → L4 = 0.0，**低于保底线是正常的**（显著落后档，不是没吃到保底）。
            //   v112 两种都只标「（未披露）」，会让读者误以为「凡未披露都该落在保底线上」，
            //   进而把 ② 的 0 分条看成画错了。此处把两种语义在标签上写开。
            const labels = keys.map(k => {
                const r = l4[k] || {};
                if (Number(r['基础分']) !== 0) return k;
                return r['保底分'] != null ? (k + '（未披露·行业普遍未披露）')
                                           : (k + '（未披露·行业已披露）');
            });
            // 保底线：只对本指标【真的吃到保底】的行画一小段橙色虚线（x = 1.0）。
            // v=113 不再画通高竖线 —— 通高线会让「行业已披露→0 分」的行看起来"没够到保底线"，
            // 而保底规则本身只适用于行业普遍未披露的情形（见 weights_logic.calc_benchmark_score）。
            const floorRows = keys.map((k, i) => (l4[k] && l4[k]['保底分'] != null) ? i : -1)
                                   .filter(i => i >= 0);
            const hasFloor = floorRows.length > 0;
            const floorLine = {
                id: 'v113FloorLine',
                afterDatasetsDraw(c2chart) {
                    const opt = c2chart.options.plugins && c2chart.options.plugins.v113Floor;
                    if (!opt || !opt.enabled || !(opt.rows || []).length) return;
                    const xs = c2chart.scales.x;
                    const meta = c2chart.getDatasetMeta(0);
                    if (!xs || !meta || !meta.data) return;
                    const px = xs.getPixelForValue(opt.value);
                    const area = c2chart.chartArea;
                    const c2 = c2chart.ctx;
                    c2.save();
                    c2.setLineDash([7, 6]);
                    c2.lineWidth = 2;
                    c2.strokeStyle = 'rgba(217, 119, 6, 0.95)';
                    // v=113：逐行绘制。只标"本指标真的吃了保底"的行，
                    //   其余行（含未披露但行业已披露→0 分）不出现任何橙色标记，避免误导。
                    opt.rows.forEach(function (i) {
                        const el = meta.data[i];
                        if (!el) return;
                        const half = ((el.height != null ? el.height : 22) / 2) + 5;
                        c2.beginPath();
                        c2.moveTo(px, el.y - half);
                        c2.lineTo(px, el.y + half);
                        c2.stroke();
                    });
                    // 文字放在 x 轴下方的刻度带：x=1.0 处没有刻度数字（stepSize=2，只有 0/2/4…），
                    // 横向居中书写，既不压住横条，也不会被裁切。
                    c2.setLineDash([]);
                    c2.fillStyle = 'rgba(180, 83, 9, 0.95)';
                    c2.font = '600 15px sans-serif';
                    c2.textAlign = 'center';
                    c2.textBaseline = 'top';
                    c2.fillText(opt.label || '保底线 1.0', px, area.bottom + 6);
                    c2.restore();
                }
            };
            const cv = document.createElement('canvas');
            cv.width = 1200;
            cv.height = 150 + keys.length * 76;      // 每条 76px：两条横柱 + 间隙，缩印后仍清晰
            const chart = new Chart(cv, {
                type: 'bar',
                data: {
                    labels: labels,                       // 不再截断：横向有充足空间放全名
                    datasets: [
                        // v=107 修复「得 0 分就整条不画」：minBarLength 让 0 值也画出 8px 的可见条。
                        // 依据（Chart.js v4.4.1 源码 controller.bar 的 _calculateBarValuePixels）：
                        //   size = head - base;  if (Math.abs(size) < minBarLength) {
                        //     size = (0 !== size ? Math.sign(size)
                        //            : (vScale.isHorizontal() ? 1 : -1) * (vScale.min >= base ? 1 : -1)) * minBarLength; }
                        // 即源码专门为 size === 0 留了分支（不取符号，按轴向取 ±1），故 0 值同样被绘制；
                        // 画布 1200px 宽，缩印到 A4 正文后 8px ≈ 4px，仍清晰可辨。
                        // 目的：把「该项有数据且为 0」（本企业尚未起步 / 行业均值确为 0）与「数据缺失」在图上区分开。
                        // 注意：图 1（障碍度 Top10）刻意不加此参数——障碍度 0 表示「非缺口」，画条反而误导。
                        { label: '本企业', data: keys.map(k => Number(l4[k]['L4得分']) || 0), minBarLength: 8, backgroundColor: 'rgba(4, 120, 87, 0.92)', barPercentage: 0.88, categoryPercentage: 0.62 },
                        // v=112：灰条取「行业L4得分」（同口径 0-10）。字段缺失时按 0 画（不伪造数据）。
                        { label: '行业水平（同口径）', data: keys.map(k => { const v = l4[k]['行业L4得分']; return (v == null || isNaN(Number(v))) ? 0 : Number(v); }), minBarLength: 8, backgroundColor: 'rgba(156, 163, 175, 0.85)', barPercentage: 0.88, categoryPercentage: 0.62 }
                    ]
                },
                options: {
                    indexAxis: 'y',
                    responsive: false,
                    animation: false,
                    layout: { padding: { right: 26, top: 4 } },
                    plugins: {
                        legend: { position: 'top', labels: { font: { size: 20 }, color: '#374151', boxWidth: 26, padding: 14 } },
                        title: { display: true, text: '关键能力项：本企业 vs 行业水平（同口径 0-10 分）', font: { size: 24, weight: '700' }, color: '#111827', padding: { bottom: 10 } },
                        tooltip: { enabled: false },
                        // v=113 保底线：仅对「真的吃了保底」的行逐行标注（rows 为空则不画）
                        v113Floor: { enabled: hasFloor, value: 1, rows: floorRows, label: '保底线 1.0' }
                    },
                    scales: {
                        x: { beginAtZero: true, max: 10, ticks: { font: { size: 17 }, color: '#6b7280', stepSize: 2 }, grid: { color: 'rgba(0,0,0,0.06)' } },
                        y: { ticks: { font: { size: 19 }, color: '#1f2937', autoSkip: false }, grid: { display: false } }
                    }
                },
                plugins: [floorLine]
            });
            chart.update('none');
            out.benchmark_img = cv.toDataURL('image/png');
            chart.destroy();
        }
    } catch (e) { console.warn('v=103 对标图生成失败:', e); }

    return out;
}

function downloadReport() {
    const btn = $('#downloadBtn');
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<div class="loader-ring" style="width:20px;height:20px;border:2px solid rgba(255,255,255,0.3);border-top-color:#fff;border-radius:50%;display:inline-block;animation:spin 1s linear infinite;"></div> 生成中...';

    // v=85：优先 WeasyPrint 后端渲染（精美版），失败回退 pdfmake
    const genWithWeasyPrint = async () => {
        const scoreData = App._reportScoreData || {};
        const ext = (App.extractResult && App.extractResult.extracted) || App.extractResult || {};
        const meta = ext['报告元数据'] || {};
        const sd = ext['评分增强数据'] || {};
        // v=103：建议数据统一走 parseAdvicePayload（服务端 suggestion 优先 + raw_text 容错修复）
        // 旧实现只认 raw_text 的 JSON.parse，一旦服务端截断，就把整坨 JSON 塞进 __raw_md 渲染进 PDF
        const advPayload = parseAdvicePayload(App.scoreResult);
        let advList = (advPayload.advs && advPayload.advs.length) ? advPayload.advs : [];
        // 兜底：仅当确认是自由 Markdown 文本时才走 __raw_md（JSON 源码永不进正文）
        if (!advList.length && advPayload.md) {
            advList = [{ '__raw_md': advPayload.md }];
        }

        // 雷达图（v=103：优先离屏高清渲染，取不到再退页面 canvas）
        let radarImg = _renderRadarChart(sd);
        if (!radarImg) {
            const radarCanvas = document.getElementById('reportRadar');
            if (radarCanvas) {
                try { radarImg = radarCanvas.toDataURL('image/png'); } catch (e) { radarImg = ''; }
            }
        }
        // v=100：障碍度 Top10 + 行业均值对比图（离屏渲染，规避「未渲染→白图」）
        const extraCharts = _renderReportCharts(sd);

        const reportData = {
            company: meta['企业名称'] || '待评估企业',
            report_year: meta['报告年份'] || '—',
            total_score: sd['总分'] || 0,
            grade: sd['评分等级'] || '—',
            dim_scores: sd['维度得分'] || {},
            suggestions: advList,
            radar_img: radarImg,
            obstacle_img: extraCharts.obstacle_img,
            benchmark_img: extraCharts.benchmark_img,
            // v=100：诊断摘要（Agent2 输出），报告正文用
            conclusion: advPayload.summary || '',
            // v=103：企业画像（模板里早就有 {% if profile %}，但前端从未传过值）
            profile: advPayload.profile || ''
        };

        const r = await fetchWithTimeout('/api/report', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(reportData)
        }, 120000);
        if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            throw new Error(err.error || ('报告服务异常: ' + r.status));
        }
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `碳治理画像报告-${reportData.company}.pdf`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        return 'weasyprint';
    };

    setTimeout(() => {
        genWithWeasyPrint()
            .then(() => {
                showToast('画像报告 PDF 已生成（v=85 精美版）', 'success');
            })
            .catch(e => {
                console.warn('WeasyPrint 报告生成失败，回退 pdfmake:', e);
                // v=92：buildPdfReport 已改为 async，必须用 catch 承接（try/catch 抓不到 Promise 拒绝）
                try {
                    Promise.resolve(buildPdfReport())
                        .then(() => showToast('已用兼容模式生成 PDF', 'info'))
                        .catch(e2 => {
                            console.error('pdfmake 也失败，回退打印:', e2);
                            showToast('PDF 生成失败，回退打印', 'error');
                            window.print();
                        });
                } catch (e2) {
                    console.error('pdfmake 也失败，回退打印:', e2);
                    showToast('PDF 生成失败，回退打印', 'error');
                    window.print();
                }
            })
            .finally(() => {
                btn.disabled = false;
                btn.innerHTML = original;
            });
    }, 100);
}

// ============ 金融机构对比 ============
function initComparison() {
    renderComparisonTable();
    $('#sortSelect').addEventListener('change', (e) => {
        renderComparisonTable(e.target.value);
    });
}

function renderComparisonTable(sortBy = 'overall') {
    const tbody = $('#comparisonBody');
    let companies = [...MOCK_DATA.companies];

    companies.sort((a, b) => {
        if (sortBy === 'overall') return b.overall - a.overall;
        return b.scores[sortBy] - a.scores[sortBy];
    });

    tbody.innerHTML = companies.map(c => {
        const gradeClass = c.grade === 'S' ? 'green' : c.grade === 'A' ? 'blue' : c.grade === 'B' ? 'teal' : 'gray';
        return `
            <tr>
                <td>${c.name}<br><span style="font-size:12px;color:var(--color-text-muted);font-weight:400;">${c.industry}</span></td>
                <td>${c.scores.internal}</td>
                <td>${c.scores.supply}</td>
                <td>${c.scores.disclosure}</td>
                <td>${c.scores.finance}</td>
                <td><strong style="font-size:16px;">${c.overall}</strong></td>
                <td><span class="score-badge ${gradeClass}">${c.grade} · ${c.gradeLabel}</span></td>
                <td><button class="btn btn-ghost btn-sm" onclick="showCompanyDetail('${c.name}')">详情</button></td>
            </tr>
        `;
    }).join('');
}

function showCompanyDetail(name) {
    const company = MOCK_DATA.companies.find(c => c.name === name);
    if (!company) return;
    showToast(`${company.name}：综合评分 ${company.overall} (${company.grade}级)`, 'info');
}

function toggleCompareChart() {
    App.compareChartType = App.compareChartType === 'bar' ? 'radar' : 'bar';
    Charts.renderCompareChart();
    showToast(`已切换为${App.compareChartType === 'bar' ? '柱状图' : '雷达图'}视图`, 'info');
}

// ============ 初始化 ============
document.addEventListener('DOMContentLoaded', () => {
    initNavbar();
    initUpload();
    initTabs();
    initComparison();

    // 公司类型选择器交互（高亮当前选中的方案卡片）
    // 排除模型选择器（#modelOptions 由 initModelSelector 独立管理）
    $$('.company-type-option').forEach(opt => {
        if (opt.closest('#modelOptions')) return;
        opt.addEventListener('click', () => {
            $$('.company-type-option').forEach(o => o.classList.remove('active'));
            opt.classList.add('active');
        });
    });

    // 分析模型选择器（v=78 多模型）
    initModelSelector();

    // Hero 雷达图（小型预览）
    if (typeof Chart !== 'undefined') {
        Charts.initHeroRadar();
    }

    // Supabase 认证 + 历史（v=70）
    initSupabase();

    // v=115：上传区登录态视觉（initSupabase → updateAuthUI 内也会刷一次，这里是首屏兜底）
    refreshUploadLockUI();

    // v=115：进入页面弹「使用须知」公告（每会话一次；勾选「不再提示」后永久静默）
    maybeShowNotice();

    // 点击模态框外部关闭
    $$('.modal-overlay').forEach(overlay => {
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) overlay.classList.remove('active');
            document.body.style.overflow = '';
        });
    });

    // ESC关闭模态框
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            $$('.modal-overlay').forEach(m => m.classList.remove('active'));
            document.body.style.overflow = '';
        }
    });
});

// ============ 移动端导航抽屉（v=99） ============
/**
 * 背景（bug 修复）：
 *   style.css 的 @media (max-width:768px) 把 .nav-menu / .nav-actions 设成了 display:none，
 *   而 #navToggle 这个汉堡按钮从来没有绑定过任何事件 —— 于是手机上导航项和「登录」按钮
 *   全部消失，☰ 点了也没反应，用户根本找不到登录弹窗的入口（桌面端不受影响，所以一直没暴露）。
 *
 * 做法：
 *   点汉堡按钮给 .navbar 加/去 .nav-open，由 CSS 把「原生」导航展开为纵向抽屉。
 *   刻意不新增第二套导航 DOM —— 登录态仍由 updateAuthUI() 单点驱动（它管的是
 *   #navAuthBtn / #userMenuWrap / #navHistoryLink 的显隐），登录/退出后抽屉内自动同步。
 *
 * 注：脚本在 </body> 前引入，DOM 已解析完毕，故无需等 DOMContentLoaded。
 */
(function initMobileNavDrawer() {
    const navbar = document.getElementById('navbar');
    const toggle = document.getElementById('navToggle');
    if (!navbar || !toggle) return;

    const closeDrawer = () => navbar.classList.remove('nav-open');

    toggle.addEventListener('click', (e) => {
        e.stopPropagation(); // 别让下面的「点外部收起」立刻把它关掉
        navbar.classList.toggle('nav-open');
    });

    // 点抽屉里的导航项 / 顶部操作按钮后自动收起。
    // '.nav-actions > .btn' 用直接子选择器，避免误伤用户下拉卡片内部的「保存/取消」按钮
    // （那些按钮也在 .nav-actions 内，但父级不是 .nav-actions）。
    navbar.addEventListener('click', (e) => {
        if (!e.target || !e.target.closest) return;
        if (e.target.closest('.nav-toggle')) return;
        if (e.target.closest('.nav-link, .nav-actions > .btn')) closeDrawer();
    });

    // 点抽屉外部收起
    document.addEventListener('click', (e) => {
        if (!navbar.classList.contains('nav-open')) return;
        if (navbar.contains(e.target)) return;
        closeDrawer();
    });

    // Esc 收起
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeDrawer();
    });

    // 切回桌面尺寸时收起，避免宽屏残留 .nav-open
    window.addEventListener('resize', () => {
        if (window.innerWidth > 768) closeDrawer();
    });
})();
