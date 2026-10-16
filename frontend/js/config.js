/**
 * config.js — 系统API配置
 * 集中管理所有后端服务地址，方便部署时修改
 *
 * 架构：
 *   前端 → 提取服务(DeepSeek) → 增强JSON →
 *   前端 → /api/suggest(容器代理 ADP) → 根因分析+Top10建议 → 前端渲染
 */

const API_CONFIG = {
    // 提取服务（Docker沙箱，DeepSeek API + 智能体一提示词）
    // ⚠️ 必须用同源相对路径 '/api'，不要写 http://localhost
    //    写死 localhost 时：公网用户浏览器会把 localhost 解析成"自己的电脑"，
    //    且 HTTPS 页面请求 HTTP 属混合内容，会被直接拦截（CORS + ERR_FAILED 400）
    // 相对路径的好处：本机 http://localhost 和公网 https://<你的域名> 都能用
    // 路径含 /api 前缀，fetch 拼接时只加 /extract 即可
    EXTRACT_API: '/api',

    // ADP 建议生成（容器内代理 → 腾讯云 ADP 对话流）
    // ⚠️ 同上：同源相对路径，勿写死域名/主机
    // 后端用 AppKey 鉴权，前端不接触密钥
    ADP_API: '/api/suggest',

    // 当前模式: 'mock' 用模拟数据演示 | 'live' 真实API
    MODE: 'live',

    // Supabase（用户体系 + 分析历史存储，v=70 新增）
    // publishable key 公开安全（RLS 行级安全保护数据），勿放 service_role 密钥
    // v=97：旧项目 dtxwciejvxcpzvglhqvj 已因免费版不活跃被暂停下线（公共 DNS 无解析），
    //       迁移至新项目 ucejlpyxhxpaqmgkjepa（ap-southeast-1 新加坡），schema 与 RLS 已重建
    SUPABASE_URL: 'https://ucejlpyxhxpaqmgkjepa.supabase.co',
    SUPABASE_ANON_KEY: 'sb_publishable_-Jz_B8guFeHMJAVhxCkwoA_AKPtaBUn',

    // 分析模型列表（v=78 多模型，one-api 网关路由）
    // id 与后端 models.py SUPPORTED_MODELS 保持一致
    // 分析模型列表（v=87：替换 glm-5v-turbo 为 deepseek-v4-flash-vision-exp，复用 DeepSeek 余额）
    // id 与后端 models.py SUPPORTED_MODELS 保持一致
    // v=203：Kimi 入口置灰（disabled=true 表示「展示但不可选」）
    //   原因：Moonshot 国内站按源 IP 屏蔽境外服务器（香港实测 443/80 全超时），
    //         国际站需境外网络 + 美元信用卡支付，本次交付版本不开放该入口。
    //   ⚠️ disabled 的模型不会出现在可选列表里；即使 localStorage 里残留旧选择，
    //      getSelectedModel() 也会自动回落到 DEFAULT_MODEL。
    MODELS: [
        { id: 'deepseek-v4-flash', label: 'DeepSeek V4 Flash', desc: '快速 · 默认' },
        { id: 'deepseek-v4-pro',   label: 'DeepSeek V4 Pro',   desc: '深度推理' },
        { id: 'deepseek-v4-flash-vision-exp', label: 'DeepSeek V4 Vision', desc: '多模态视觉' },
        { id: 'glm-5.2',           label: 'GLM 5.2',           desc: '智谱' },
        { id: 'kimi-k3',           label: 'Kimi K3',           desc: '月之暗面 · 暂不可用', disabled: true },
        { id: 'minimax-m3',        label: 'MiniMax M3',        desc: 'MiniMax' }
    ],
    DEFAULT_MODEL: 'deepseek-v4-flash'
};