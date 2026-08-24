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
    // 走 nginx 反代（80 端口），同源避免 CORS
    // 路径含 /api 前缀，fetch 拼接时只加 /extract 即可
    EXTRACT_API: 'http://localhost/api',

    // ADP 建议生成（容器内代理 → 腾讯云 ADP 对话流）
    // 走 nginx 反代（80 端口），同源避免 CORS
    // 后端用 AppKey 鉴权，前端不接触密钥
    ADP_API: 'http://localhost/api/suggest',

    // 当前模式: 'mock' 用模拟数据演示 | 'live' 真实API
    MODE: 'live',

    // Supabase（用户体系 + 分析历史存储，v=70 新增）
    // publishable key 公开安全（RLS 行级安全保护数据），勿放 service_role 密钥
    SUPABASE_URL: 'https://dtxwciejvxcpzvglhqvj.supabase.co',
    SUPABASE_ANON_KEY: 'sb_publishable_JSz0wfsr2A3h_X_66dDeyA_qUkqDubn',

    // 分析模型列表（v=78 多模型，one-api 网关路由）
    // id 与后端 models.py SUPPORTED_MODELS 保持一致
    // 分析模型列表（v=87：替换 glm-5v-turbo 为 deepseek-v4-flash-vision-exp，复用 DeepSeek 余额）
    // id 与后端 models.py SUPPORTED_MODELS 保持一致
    MODELS: [
        { id: 'deepseek-v4-flash', label: 'DeepSeek V4 Flash', desc: '快速 · 默认' },
        { id: 'deepseek-v4-pro',   label: 'DeepSeek V4 Pro',   desc: '深度推理' },
        { id: 'deepseek-v4-flash-vision-exp', label: 'DeepSeek V4 Vision', desc: '多模态视觉' },
        { id: 'glm-5.2',           label: 'GLM 5.2',           desc: '智谱' },
        { id: 'kimi-k3',           label: 'Kimi K3',           desc: '月之暗面' },
        { id: 'minimax-m3',        label: 'MiniMax M3',        desc: 'MiniMax' }
    ],
    DEFAULT_MODEL: 'deepseek-v4-flash'
};