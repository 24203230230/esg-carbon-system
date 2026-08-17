/**
 * mock-data.js — 模拟数据
 * 供前端演示使用，后续可替换为后端API返回数据
 */

const MOCK_DATA = {
    // 当前评估企业
    currentCompany: {
        name: "示例科技集团",
        reportYear: 2024,
        reportType: "ESG年度报告",
        score: {
            overall: 7.8,
            internal: 8.2,
            supply: 6.5,
            disclosure: 8.5,
            finance: 7.9
        },
        grade: "A",
        gradeLabel: "优秀",
        carbonData: {
            scope1: 12450,
            scope2: 8320,
            scope3: 45600,
            total: 66370,
            energyTotal: 186500,
            renewableRatio: 32.5,
            reductionTarget: "2030年减排30%",
            reductionBaseYear: 2023,
            yoyChange: -8.5
        }
    },

    // 对比企业数据
    companies: [
        {
            id: 1,
            name: "示例科技集团",
            industry: "信息技术",
            scores: { internal: 8.2, supply: 6.5, disclosure: 8.5, finance: 7.9 },
            overall: 7.8,
            grade: "A",
            gradeLabel: "优秀"
        },
        {
            id: 2,
            name: "绿色能源股份",
            industry: "清洁能源",
            scores: { internal: 9.1, supply: 8.3, disclosure: 9.2, finance: 8.8 },
            overall: 8.9,
            grade: "S",
            gradeLabel: "卓越"
        },
        {
            id: 3,
            name: "华信制造集团",
            industry: "高端制造",
            scores: { internal: 7.0, supply: 5.8, disclosure: 7.2, finance: 6.3 },
            overall: 6.6,
            grade: "B",
            gradeLabel: "良好"
        },
        {
            id: 4,
            name: "中环新材料",
            industry: "新材料",
            scores: { internal: 6.5, supply: 4.2, disclosure: 6.0, finance: 5.5 },
            overall: 5.6,
            grade: "C",
            gradeLabel: "待改进"
        },
        {
            id: 5,
            name: "远东物流股份",
            industry: "物流运输",
            scores: { internal: 7.5, supply: 7.0, disclosure: 7.8, finance: 6.8 },
            overall: 7.3,
            grade: "B",
            gradeLabel: "良好"
        },
        {
            id: 6,
            name: "蓝海生物科技",
            industry: "生物医药",
            scores: { internal: 8.0, supply: 6.8, disclosure: 8.2, finance: 7.5 },
            overall: 7.6,
            grade: "A",
            gradeLabel: "优秀"
        }
    ],

    // 评级标准
    gradeStandards: [
        { grade: "S", label: "卓越", range: [9.0, 10.0], color: "green" },
        { grade: "A", label: "优秀", range: [7.5, 8.9], color: "blue" },
        { grade: "B", label: "良好", range: [6.0, 7.4], color: "teal" },
        { grade: "C", label: "待改进", range: [0, 5.9], color: "gray" }
    ]
};

// 图表颜色配置
const CHART_COLORS = {
    green: "rgba(16, 185, 129, 0.7)",
    greenBorder: "rgba(16, 185, 129, 1)",
    blue: "rgba(59, 130, 246, 0.7)",
    blueBorder: "rgba(59, 130, 246, 1)",
    teal: "rgba(20, 184, 166, 0.7)",
    tealBorder: "rgba(20, 184, 166, 1)",
    yellow: "rgba(245, 158, 11, 0.7)",
    yellowBorder: "rgba(245, 158, 11, 1)",
    gray: "rgba(100, 116, 139, 0.5)",
    grayBorder: "rgba(100, 116, 139, 1)"
};
