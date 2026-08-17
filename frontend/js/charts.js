/**
 * charts.js — 图表渲染
 * 使用 Chart.js 渲染雷达图、柱状图、对比图
 */

const Charts = {
    _instances: {},

    // ============ 数据源切换：真实评分优先，MOCK兜底 ============
    _getScoreData() {
        // 优先使用真实评分结果（两段式：前端 Python 已算好的「评分增强数据」）
        // 注意：数据在 App.extractResult.extracted.评分增强数据（不是 App.scoreResult.评分结果！）
        try {
            const ext = (window.App && App.extractResult && App.extractResult.extracted) || (window.App && App.extractResult) || {};
            const sd = ext['评分增强数据'];
            if (sd && sd['维度得分']) {
                const dims = sd['维度得分'];
                return {
                    internal: dims['企业内部碳管理'] ?? 0,
                    supply: dims['供应链碳管理'] ?? 0,
                    disclosure: dims['透明度'] ?? 0,
                    finance: dims['金融化情况'] ?? 0,
                    overall: sd['总分'] ?? 0,
                    // 行业平均（从 L4 计算结果按维度聚合）
                    industry: this._getIndustryAverages(sd),
                    level: sd['评分等级'] || '-'
                };
            }
        } catch (e) {
            console.warn('评分增强数据读取失败，回退 MOCK:', e);
        }
        // MOCK兜底
        const mock = MOCK_DATA.currentCompany.score;
        return {
            internal: mock.internal, supply: mock.supply,
            disclosure: mock.disclosure, finance: mock.finance,
            overall: mock.overall, industry: null, level: '-'
        };
    },

    // 行业平均：从 L4计算结果取每个指标"行业均值"，按维度加权平均（权重为指标权重）
    _getIndustryAverages(sd) {
        try {
            const l4 = sd['L4计算结果'] || {};
            if (!Object.keys(l4).length) return null;
            const dims = { '企业内部碳管理': [0, 0], '供应链碳管理': [0, 0], '透明度': [0, 0], '金融化情况': [0, 0] };
            Object.values(l4).forEach(r => {
                const dim = r['维度'];
                const indMean = r['行业均值'];
                const weight = r['权重'];
                if (dims[dim] && typeof indMean === 'number' && typeof weight === 'number') {
                    dims[dim][0] += indMean * weight;
                    dims[dim][1] += weight;
                }
            });
            const out = {};
            Object.entries(dims).forEach(([dim, [sum, w]]) => {
                out[dim] = w > 0 ? +(sum / w).toFixed(2) : null;
            });
            return out;
        } catch (e) {
            console.warn('行业均值聚合失败:', e);
            return null;
        }
    },

    // ============ Hero 小型雷达图 ============
    initHeroRadar() {
        const ctx = document.getElementById('heroRadar');
        if (!ctx || this._instances.heroRadar) return;
        // 真实维度得分（MOCK 兜底）
        const score = this._getScoreData();

        this._instances.heroRadar = new Chart(ctx, {
            type: 'radar',
            data: {
                labels: ['内部管理', '供应链', '透明度', '碳金融'],
                datasets: [{
                    data: [score.internal, score.supply, score.disclosure, score.finance],
                    backgroundColor: 'rgba(16, 185, 129, 0.15)',
                    borderColor: 'rgba(16, 185, 129, 0.8)',
                    borderWidth: 2,
                    pointBackgroundColor: 'rgba(16, 185, 129, 1)',
                    pointBorderColor: '#fff',
                    pointBorderWidth: 1.5,
                    pointRadius: 4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false }, tooltip: { enabled: false } },
                scales: {
                    r: {
                        min: 0, max: 10,
                        ticks: { display: false },
                        grid: { color: 'rgba(0,0,0,0.06)' },
                        angleLines: { color: 'rgba(0,0,0,0.06)' },
                        pointLabels: { font: { size: 10 }, color: '#94a3b8' }
                    }
                }
            }
        });
    },

    // ============ 评分页 - 雷达图 ============
    initRadarChart() {
        const ctx = document.getElementById('radarChart');
        if (!ctx || this._instances.radar) return;
        // 数据源：优先真实评分，无则用MOCK兜底
        const score = this._getScoreData();

        this._instances.radar = new Chart(ctx, {
            type: 'radar',
            data: {
                labels: ['企业内部碳管理', '供应链管理', '透明度披露', '碳金融转化'],
                datasets: [{
                    label: '当前企业',
                    data: [score.internal, score.supply, score.disclosure, score.finance],
                    backgroundColor: 'rgba(16, 185, 129, 0.2)',
                    borderColor: 'rgba(16, 185, 129, 1)',
                    borderWidth: 2.5,
                    pointBackgroundColor: 'rgba(16, 185, 129, 1)',
                    pointBorderColor: '#fff',
                    pointBorderWidth: 2,
                    pointRadius: 6,
                    pointHoverRadius: 8
                }, {
                    label: '行业平均',
                    data: [
                        score.industry?.['企业内部碳管理'] ?? 5.5,
                        score.industry?.['供应链碳管理'] ?? 5.5,
                        score.industry?.['透明度'] ?? 5.5,
                        score.industry?.['金融化情况'] ?? 5.5
                    ],
                    backgroundColor: 'rgba(59, 130, 246, 0.1)',
                    borderColor: 'rgba(59, 130, 246, 0.6)',
                    borderWidth: 1.5,
                    borderDash: [5, 5],
                    pointBackgroundColor: 'rgba(59, 130, 246, 0.8)',
                    pointBorderColor: '#fff',
                    pointBorderWidth: 1.5,
                    pointRadius: 4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: {
                            font: { size: 13 },
                            color: '#5a6c7d',
                            padding: 16,
                            usePointStyle: true,
                            pointStyle: 'circle'
                        }
                    },
                    tooltip: {
                        backgroundColor: 'rgba(26,35,50,0.95)',
                        padding: 12,
                        cornerRadius: 8,
                        titleFont: { size: 13 },
                        bodyFont: { size: 13 }
                    }
                },
                scales: {
                    r: {
                        min: 0, max: 10,
                        ticks: {
                            stepSize: 2,
                            font: { size: 11 },
                            color: '#94a3b8',
                            backdropColor: 'transparent'
                        },
                        grid: { color: 'rgba(0,0,0,0.06)' },
                        angleLines: { color: 'rgba(0,0,0,0.06)' },
                        pointLabels: {
                            font: { size: 13, weight: '600' },
                            color: '#1a2332'
                        }
                    }
                }
            }
        });
    },

    // ============ 评分页 - 柱状图 ============
    initBarChart() {
        const ctx = document.getElementById('barChart');
        if (!ctx || this._instances.bar) return;
        const data = this._getScoreData();

        this._instances.bar = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: ['内部碳管理', '供应链管理', '透明度披露', '碳金融转化', '综合评分'],
                datasets: [{
                    label: '当前企业',
                    data: [data.internal, data.supply, data.disclosure, data.finance, data.overall],
                    backgroundColor: [
                        'rgba(16, 185, 129, 0.7)',
                        'rgba(245, 158, 11, 0.7)',
                        'rgba(20, 184, 166, 0.7)',
                        'rgba(59, 130, 246, 0.7)',
                        'rgba(16, 185, 129, 0.9)'
                    ],
                    borderColor: [
                        'rgba(16, 185, 129, 1)',
                        'rgba(245, 158, 11, 1)',
                        'rgba(20, 184, 166, 1)',
                        'rgba(59, 130, 246, 1)',
                        'rgba(16, 185, 129, 1)'
                    ],
                    borderWidth: 1.5,
                    borderRadius: 8,
                    barThickness: 'flex',
                    maxBarThickness: 50
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(26,35,50,0.95)',
                        padding: 12,
                        cornerRadius: 8,
                        callbacks: {
                            label: (ctx) => `${ctx.parsed.y} / 10 分`
                        }
                    }
                },
                scales: {
                    y: {
                        min: 0, max: 10,
                        ticks: {
                            stepSize: 2,
                            font: { size: 12 },
                            color: '#94a3b8'
                        },
                        grid: { color: 'rgba(0,0,0,0.05)' }
                    },
                    x: {
                        ticks: {
                            font: { size: 12, weight: '600' },
                            color: '#5a6c7d'
                        },
                        grid: { display: false }
                    }
                }
            }
        });
    },

    // ============ 对比图 ============
    initCompareChart() {
        this.renderCompareChart();
    },

    renderCompareChart() {
        const ctx = document.getElementById('compareChart');
        if (!ctx) return;

        // 销毁旧实例
        if (this._instances.compare) {
            this._instances.compare.destroy();
        }

        const companies = MOCK_DATA.companies;
        const labels = companies.map(c => c.name);

        if (App.compareChartType === 'radar') {
            this._instances.compare = new Chart(ctx, {
                type: 'radar',
                data: {
                    labels: ['内部碳管理', '供应链管理', '透明度披露', '碳金融转化'],
                    datasets: companies.map((c, i) => {
                        const colors = [
                            { bg: 'rgba(16,185,129,0.15)', border: 'rgba(16,185,129,1)' },
                            { bg: 'rgba(59,130,246,0.15)', border: 'rgba(59,130,246,1)' },
                            { bg: 'rgba(20,184,166,0.15)', border: 'rgba(20,184,166,1)' },
                            { bg: 'rgba(245,158,11,0.15)', border: 'rgba(245,158,11,1)' },
                            { bg: 'rgba(139,92,246,0.15)', border: 'rgba(139,92,246,1)' },
                            { bg: 'rgba(236,72,153,0.15)', border: 'rgba(236,72,153,1)' }
                        ];
                        const col = colors[i % colors.length];
                        return {
                            label: c.name,
                            data: [c.scores.internal, c.scores.supply, c.scores.disclosure, c.scores.finance],
                            backgroundColor: col.bg,
                            borderColor: col.border,
                            borderWidth: 2,
                            pointBackgroundColor: col.border,
                            pointBorderColor: '#fff',
                            pointBorderWidth: 1.5,
                            pointRadius: 4
                        };
                    })
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            position: 'bottom',
                            labels: { font: { size: 12 }, padding: 12, usePointStyle: true, pointStyle: 'circle' }
                        },
                        tooltip: {
                            backgroundColor: 'rgba(26,35,50,0.95)',
                            padding: 12, cornerRadius: 8
                        }
                    },
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
        } else {
            // 柱状图 - 分组
            this._instances.compare = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: labels,
                    datasets: [
                        {
                            label: '内部碳管理',
                            data: companies.map(c => c.scores.internal),
                            backgroundColor: 'rgba(16, 185, 129, 0.7)',
                            borderColor: 'rgba(16, 185, 129, 1)',
                            borderWidth: 1,
                            borderRadius: 6
                        },
                        {
                            label: '供应链管理',
                            data: companies.map(c => c.scores.supply),
                            backgroundColor: 'rgba(245, 158, 11, 0.7)',
                            borderColor: 'rgba(245, 158, 11, 1)',
                            borderWidth: 1,
                            borderRadius: 6
                        },
                        {
                            label: '透明度披露',
                            data: companies.map(c => c.scores.disclosure),
                            backgroundColor: 'rgba(20, 184, 166, 0.7)',
                            borderColor: 'rgba(20, 184, 166, 1)',
                            borderWidth: 1,
                            borderRadius: 6
                        },
                        {
                            label: '碳金融转化',
                            data: companies.map(c => c.scores.finance),
                            backgroundColor: 'rgba(59, 130, 246, 0.7)',
                            borderColor: 'rgba(59, 130, 246, 1)',
                            borderWidth: 1,
                            borderRadius: 6
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            position: 'bottom',
                            labels: { font: { size: 12 }, padding: 12, usePointStyle: true, pointStyle: 'circle' }
                        },
                        tooltip: {
                            backgroundColor: 'rgba(26,35,50,0.95)',
                            padding: 12, cornerRadius: 8
                        }
                    },
                    scales: {
                        y: {
                            min: 0, max: 10,
                            ticks: { stepSize: 2, font: { size: 12 }, color: '#94a3b8' },
                            grid: { color: 'rgba(0,0,0,0.05)' }
                        },
                        x: {
                            ticks: { font: { size: 11 }, color: '#5a6c7d' },
                            grid: { display: false }
                        }
                    }
                }
            });
        }
    },

    // ============ 初始化所有评分图表 ============
    initAll() {
        this.initRadarChart();
        this.initBarChart();
        this.initCompareChart();
    },

    // 分数更新后刷新所有图表（销毁重建，用真实评分数据）
    refresh() {
        Object.values(this._instances).forEach(i => {
            try { i.destroy(); } catch (e) { /* 忽略 */ }
        });
        this._instances = {};
        this.initHeroRadar();
        this.initRadarChart();
        this.initBarChart();
        this.initCompareChart();
    }
};
