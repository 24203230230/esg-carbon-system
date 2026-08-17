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
    MODE: 'live'
};