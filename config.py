"""
ESG提取系统配置
"""
import os

# ===== CSMAR数据路径 =====
CSMAR_DATA_ROOT = os.environ.get(
    'CSMAR_DATA_ROOT',
    os.path.join(os.path.expanduser('~'), 'esg-data', '碳中和')
)

# ===== CNINFO API 配置（可选，需要商业token） =====
CNINFO_CONFIG = {
    'base_url': 'http://webapi.cninfo.com.cn/api/stock',
    'mcode_enabled': True,
    'token': os.environ.get('CNINFO_TOKEN', ''),  # 商业订阅token
    'referer': 'http://webapi.cninfo.com.cn/',
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

# ===== ESG相关CNINFO API端点 =====
CNINFO_ENDPOINTS = {
    'governance': '/p_ESG2156',    # 公司治理议题
    'environment': '/p_ESG2157',   # 环境议题
    'social': '/p_ESG2158',        # 社会议题
    'factor_score': '/p_ESG2159',  # 因子库评分
    'dynamic': '/p_info3094',      # 动态数据
}

# ===== ESG评级因子 =====
ESG_FACTORS = {
    'E': ['碳排放', '温室气体', '能源管理', '水资源', '废弃物', '生物多样性', '气候变化'],
    'S': ['员工权益', '健康安全', '培训发展', '社区关系', '产品责任', '供应链管理'],
    'G': ['董事会', '股东权益', '信息披露', '商业道德', '反腐败', '合规管理'],
}

# ===== 输出路径 =====
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)
