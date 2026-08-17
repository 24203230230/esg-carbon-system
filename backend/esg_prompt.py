"""
ESG碳信息提取Prompt模板
用于LLM从ESG报告/公告中提取结构化碳信息
"""
from typing import Optional


# ================================================================
# 碳信息提取系统提示词
# ================================================================
ESG_EXTRACTION_SYSTEM_PROMPT = """你是一个ESG碳信息提取专家。你的任务是从企业ESG报告、可持续发展报告或碳公告中，提取结构化的碳排放和碳管理信息。

## 提取原则
1. **只提取报告中明确陈述的事实**，不推测、不补充
2. **保留原始数据单位**（如吨、万吨、tCO2e、MWh等）
3. **区分不同年份的数据**，标注报告年份
4. **对模糊表述标注"未明确"**
5. **输出严格遵循JSON格式**

## 提取字段

### 1. 基本信息
- company_name: 企业名称
- report_year: 报告年份
- report_type: ESG报告/可持续发展报告/碳公告/年报/其他

### 2. 碳排放数据
- scope1_total: 范围一直接排放量（吨CO2e）
- scope2_total: 范围二间接排放量（吨CO2e）  
- scope3_total: 范围三其他间接排放量（吨CO2e）
- total_emissions: 排放总量（吨CO2e）
- emission_intensity: 排放强度（注明按收入/产量/面积等）
- base_year: 基准年（如有披露）
- base_year_emissions: 基准年排放量
- year_over_year_change: 同比变化（%）

### 3. 能源消耗
- total_energy: 总能耗（注明单位：MWh/GJ/吨标准煤）
- renewable_energy: 可再生能源使用量/占比
- energy_intensity: 能源强度

### 4. 目标与承诺
- carbon_peak_target: 碳达峰目标（年份+说明）
- carbon_neutral_target: 碳中和目标（年份+说明）  
- reduction_target: 减排目标（百分比+基准年+范围）
- other_targets: 其他相关目标

### 5. 减排措施
- measures: [
    {{
        "category": "节能改造/能源替代/碳捕集/碳交易/供应链/其他",
        "description": "具体措施描述",
        "investment": "投资金额（如有）"
    }}
  ]

### 6. 验证与认证
- verification: 第三方核查情况（核查机构+范围+结论）
- certifications: 相关认证（ISO 14064/ISO 14001等）

### 7. 供应链碳管理（如涉及）
- supplier_carbon_mgmt: 供应商碳管理描述
- green_procurement: 绿色采购描述
- supply_chain_targets: 供应链减排目标

## 输出格式
返回严格的JSON数组，每个元素为一个企业的碳信息对象。
如果某字段无数据，设为null，不要省略字段。
"""


# ================================================================
# 用户提示词模板
# ================================================================
def build_extraction_prompt(
    report_text: str,
    company_name: Optional[str] = None,
    report_year: Optional[str] = None,
) -> str:
    """构建碳信息提取的用户提示词"""
    context = ""
    if company_name:
        context += f"企业名称: {company_name}\n"
    if report_year:
        context += f"报告年份: {report_year}\n"
    if context:
        context = f"## 上下文信息\n{context}\n"

    return f"""{context}## 待提取的ESG报告内容
```
{report_text[:8000]}
```

请严格按照系统提示词的字段定义，从上述内容中提取碳信息，返回JSON格式。

注意：
- 只提取文本中明确出现的信息
- 单位转换：如文本使用"万吨"，换算为"吨"
- 注意区分"范围一/范围二/范围三"和"Scope 1/2/3"
"""


# ================================================================
# 短文本提取（用于少量文本/新闻资讯）
# ================================================================
SHORT_TEXT_PROMPT = """从以下ESG相关文本中提取碳信息关键词和关键数据：

文本: {text}

请提取:
1. 涉及的企业/主体
2. 提到的碳数据类型（排放量/强度/目标/措施）
3. 关键数值及其单位
4. 时间范围
5. 置信度评估（高/中/低）

以JSON格式输出。
"""


__all__ = [
    'ESG_EXTRACTION_SYSTEM_PROMPT',
    'build_extraction_prompt',
    'SHORT_TEXT_PROMPT',
]
