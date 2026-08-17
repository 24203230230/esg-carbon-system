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

function handleFiles(fileList) {
    const validTypes = ['.pdf', '.doc', '.docx'];
    let added = 0;
    let rejected = 0;

    Array.from(fileList).forEach(file => {
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
        showToast(`已添加${added}个文件，正在上传...`, 'success');
    }
}

/**
 * 真实上传：调提取服务（DeepSeek API + 智能体一提示词）
 * 文件 → 提取服务 → 精简JSON
 */
function uploadToExtractService(fileObj) {
    // 模拟模式下走旧逻辑（演示用）
    if (API_CONFIG.MODE === 'mock') {
        simulateUpload(fileObj);
        return;
    }

    const formData = new FormData();
    formData.append('file', fileObj.file);

    // 进度条模拟（fetch不支持上传进度，用递增模拟）
    const progressTimer = setInterval(() => {
        if (fileObj.progress < 90) {
            fileObj.progress += Math.random() * 10 + 3;
            renderProgressList();
        }
    }, 300);

    fetch(`${API_CONFIG.EXTRACT_API}/extract`, {
        method: 'POST',
        body: formData
    })
    .then(res => {
        if (!res.ok) throw new Error(`提取服务响应异常: ${res.status}`);
        return res.json();
    })
    .then(data => {
        clearInterval(progressTimer);
        fileObj.progress = 100;
        fileObj.status = 'done';
        fileObj.extractResult = data;   // 保存提取JSON
        renderProgressList();
        renderFileList();
        checkUploadAction();
        showToast(`${fileObj.name} 提取完成`, 'success');
    })
    .catch(err => {
        clearInterval(progressTimer);
        fileObj.status = 'error';
        fileObj.error = err.message;
        renderProgressList();
        renderFileList();
        showToast(`${fileObj.name} 提取失败: ${err.message}`, 'error');
    });
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
                <div class="progress-name">${f.name}</div>
                <div class="progress-bar-wrap">
                    <div class="progress-bar" style="width:${f.progress}%"></div>
                </div>
            </div>
            <span class="progress-percent">${Math.floor(f.progress)}%</span>
            <span class="progress-status uploading">上传中</span>
        </div>
    `).join('');
}

function renderFileList() {
    const section = $('#fileListSection');
    const list = $('#fileList');
    const count = $('#fileCount');
    const done = App.uploadedFiles.filter(f => f.status === 'done');

    if (done.length === 0) {
        section.style.display = 'none';
        return;
    }

    section.style.display = 'block';
    count.textContent = `${done.length} 个文件`;

    list.innerHTML = done.map(f => {
        const iconClass = f.type === '.pdf' ? 'pdf' : 'doc';
        const iconSvg = f.type === '.pdf'
            ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>'
            : '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';

        return `
            <div class="file-item" data-id="${f.id}">
                <div class="file-icon-sm ${iconClass}">${iconSvg}</div>
                <div class="file-info">
                    <div class="file-name">${f.name}</div>
                    <div class="file-meta">${formatFileSize(f.size)} · ${f.type.toUpperCase().replace('.', '')} · 已上传</div>
                </div>
                <div class="file-actions">
                    <button class="file-btn" title="重新上传" onclick="retryUpload('${f.id}')">
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
    if (!confirm(`确认重新上传「${file.name}」？`)) return;
    file.status = 'uploading';
    file.progress = 0;
    renderFileList();
    renderProgressList();
    simulateUpload(file);
    showToast(`正在重新上传 ${file.name}`, 'info');
}

function deleteFile(id) {
    const file = App.uploadedFiles.find(f => f.id == id);
    if (!file) return;
    if (!confirm(`确认删除「${file.name}」？`)) return;
    App.uploadedFiles = App.uploadedFiles.filter(f => f.id != id);
    renderFileList();
    renderProgressList();
    checkUploadAction();
    showToast(`已删除 ${file.name}`, 'success');
}

function checkUploadAction() {
    const action = $('#uploadAction');
    const hasDone = App.uploadedFiles.some(f => f.status === 'done');
    action.style.display = hasDone ? 'block' : 'none';
}

// ============ 数据分析 ============
function startAnalysis() {
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

    // 真实模式：取第一个已上传文件
    const doneFile = App.uploadedFiles.find(f => f.status === 'done');
    if (!doneFile) {
        showToast('请先上传并完成提取的报告', 'error');
        btn.disabled = false;
        btn.innerHTML = '开始分析';
        return;
    }

    // 读取用户选择的公司类型（上市/非上市 → 评分分流）
    const companyType = getCompanyType();
    App.companyType = companyType;
    status.textContent = `正在提交提取结果到ADP评分（${companyType === 'listed' ? '上市公司' : '非上市公司'}方案）...`;

    // 步骤1：如果没有提取结果，先调提取服务（带上公司类型用于评分分流）
    const runExtract = doneFile.extractResult
        ? Promise.resolve(doneFile.extractResult)
        : fetch(`${API_CONFIG.EXTRACT_API}/extract`, {
            method: 'POST',
            body: (() => { const fd = new FormData(); fd.append('file', doneFile.file); fd.append('company_type', companyType); return fd; })()
          }).then(res => res.json());

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

    return fetch(endpoint, {
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
            user: {                        // 嵌套（部分 ADP 工作流用 user.* 命名空间）
                company_type: ctype,
                esg_json: enrichedJson
            }
        })
    }).then(res => {
        if (!res.ok) throw new Error(`评分服务响应异常: ${res.status}`);
        return res.json();
    }).then(scoreData => {
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
            scoreSummaryP.innerHTML = `该企业碳治理能力整体处于<span class="text-green font-bold">${level}</span>水平（综合评分 <strong>${total}</strong> 分）。<strong>${strongest[0]}</strong> 表现突出（${strongest[1]} 分），<strong>${weakest[0]}</strong> 仍有提升空间（${weakest[1]} 分）。`;
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
                <strong>${strongest[0]}</strong> 表现最佳（${strongest[1]} 分），
                <strong>${weakest[0]}</strong> 仍有提升空间（${weakest[1]} 分）。
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

        console.log(`✅ [fillScoreCards] 完成：总分=${total} 等级=${level}`);
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
function previewReport() {
    const body = $('#reportModalBody');

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
        // 建议：优先用 ADP 返回的 raw_text（Markdown 诊断报告），没有再降级到 Top10 模板
        // ADP chat 输出是 Markdown 报告，不是结构化 JSON，直接当报告正文展示最自然
        const adpRawText = App.scoreResult && App.scoreResult.raw_text;
        if (adpRawText && adpRawText.trim().length > 0) {
            suggestions = [{ __adp_raw_text: adpRawText }];
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
                    '完整度失分': `建议在ESG报告中补充披露该指标相关数据（基础分为0）`,
                    '质量失分': `建议补充定量数据与多年连续序列，提升披露质量`,
                    '对标失分': `建议对标行业先进企业，提升披露水平`
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

    // 生成改进建议列表
    // 如果第一条是 __adp_raw_text 标记（ADP 真实 Markdown 报告），整体渲染
    let suggestionHtml;
    let adpRawTextForPdf = '';
    if (suggestions.length === 1 && suggestions[0] && suggestions[0].__adp_raw_text) {
        const rawText = suggestions[0].__adp_raw_text;
        adpRawTextForPdf = rawText;
        // 完整 Markdown 渲染：marked 解析 + DOMPurify 防 XSS（ADP 是 LLM 生成内容，必须 sanitize）
        const escapeHtml = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        let mdHtml;
        try {
            if (typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
                mdHtml = DOMPurify.sanitize(marked.parse(rawText, { gfm: true, breaks: true }));
            } else {
                // 库未加载兜底：纯文本转义（不渲染 Markdown）
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
            return `                    <li>• ${s.建议 || JSON.stringify(s)}</li>`;
        }).join('\n');
    } else {
        suggestionHtml = '                    <li>暂无改进建议</li>';
    }

    body.innerHTML = `
        <div style="padding: 8px 0;">
            <div style="text-align:center; margin-bottom: 32px;">
                <h2 style="font-size:24px; margin-bottom:8px;">碳治理能力画像报告</h2>
                <p style="color:var(--color-text-muted);">${data.name} · ${data.reportYear}年度ESG报告</p>
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
 * 构建 PDF 报告（pdfmake 原生绘制）
 * 结构：评分总览表 + 雷达图 + 评估结论 + ADP 优化建议
 */
function buildPdfReport() {
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

    // ADP 建议 → pdfmake 节点
    const adpNodes = App._adpRawText ? mdToPdfMake(App._adpRawText) : [];
    const fallbackNodes = adpNodes.length ? [] : [{
        text: '（ADP 分析尚未生成，可重新分析获取建议）', color: '#9ca3af', margin: [0, 4, 0, 6]
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
                { image: radarImg, width: 340, alignment: 'center', margin: [0, 0, 0, 8] }
            ] : []),
            // 三、评估结论
            { text: '三、评估结论', fontSize: 14, bold: true, color: '#1a3a6e', margin: [0, 20, 0, 8] },
            {
                text: [
                    { text: `该企业碳治理能力整体处于 ${level} 水平。综合评分 ${(typeof total === 'number' ? total.toFixed(2) : total)} 分。` },
                    { text: `${strongest[0]} 表现最佳（${(strongest[1] ?? 0).toFixed(2)} 分），` },
                    { text: `${weakest[0]} 仍有提升空间（${(weakest[1] ?? 0).toFixed(2)} 分）。` }
                ],
                lineHeight: 1.7, margin: [0, 2, 0, 6]
            },
            // 四、优化建议（ADP）
            { text: '四、优化建议（ADP 智能分析）', fontSize: 14, bold: true, color: '#1a3a6e', margin: [0, 20, 0, 8] },
            ...(adpNodes.length ? adpNodes : fallbackNodes)
        ]
    };

    pdfMake.createPdf(docDefinition).download(`碳治理画像报告-${company}.pdf`);
}

function downloadReport() {
    const btn = $('#downloadBtn');
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<div class="loader-ring" style="width:20px;height:20px;border:2px solid rgba(255,255,255,0.3);border-top-color:#fff;border-radius:50%;display:inline-block;animation:spin 1s linear infinite;"></div> 生成中...';

    setTimeout(() => {
        try {
            buildPdfReport();
            showToast('画像报告 PDF 已生成', 'success');
        } catch (e) {
            console.error('PDF 生成失败，回退打印:', e);
            showToast('PDF 生成失败，回退打印', 'error');
            window.print();
        } finally {
            btn.disabled = false;
            btn.innerHTML = original;
        }
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
    $$('.company-type-option').forEach(opt => {
        opt.addEventListener('click', () => {
            $$('.company-type-option').forEach(o => o.classList.remove('active'));
            opt.classList.add('active');
        });
    });

    // Hero 雷达图（小型预览）
    if (typeof Chart !== 'undefined') {
        Charts.initHeroRadar();
    }

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
