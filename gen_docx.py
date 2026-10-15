#!/usr/bin/env python3
"""Generate the 4-layer ESG indicator framework docx"""
import os
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

doc = Document()
style = doc.styles['Normal']
style.font.name = 'Arial'
style.font.size = Pt(10)

# Title
title = doc.add_heading('ESG报告碳信息提取指标框架', level=0)
title.alignment = WD_ALIGN_PARAGRAPH.CENTER
subtitle = doc.add_paragraph()
subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = subtitle.add_run('(4层颗粒度 v2.0)')
run.font.size = Pt(14)
run.font.color.rgb = RGBColor(0, 102, 204)

meta = doc.add_paragraph()
meta.add_run('编制依据: ').bold = True
meta.add_run('CSMAR碳排放指标体系 + 深交所可持续发展报告指引 + GRI标准 + 五粮液2025 ESG报告样本分析')
meta2 = doc.add_paragraph()
meta2.add_run('基准版本: ').bold = True
meta2.add_run('v2.0 | 2026-06-04')

doc.add_page_break()

# Section 1
doc.add_heading('一、指标框架总览', level=1)
doc.add_paragraph('本框架将ESG报告中的碳信息从原始文本到具体提取字段划分为4层颗粒度:')

table = doc.add_table(rows=5, cols=4)
table.style = 'Light Shading Accent 1'
headers = ['层级', '名称', '说明', '数量']
for i, h in enumerate(headers):
    cell = table.rows[0].cells[i]
    cell.text = h
    for p in cell.paragraphs:
        for r in p.runs:
            r.bold = True

data = [
    ['L1', '维度', 'E(环境) / S(社会) / G(治理) 三大支柱', '3'],
    ['L2', '主题', '每个维度下的二级议题', '16'],
    ['L3', '指标类别', '每主题下的具体指标大类(定量/定性/目标/措施)', '48'],
    ['L4', '提取字段', '实际从文本中提取的数据字段', '127'],
]
for i, row_data in enumerate(data):
    for j, val in enumerate(row_data):
        table.rows[i+1].cells[j].text = val

doc.add_page_break()

# Section 2
doc.add_heading('二、完整指标树', level=1)

# E dimension
doc.add_heading('E -- 环境维度(Environmental)', level=2)

def add_indicator_table(doc, title, rows_data):
    doc.add_heading(title, level=3)
    table = doc.add_table(rows=len(rows_data)+1, cols=4)
    table.style = 'Light Grid Accent 1'
    hdrs = ['L3 指标类别', 'L4 提取字段', '数据类型', '示例']
    for i, h in enumerate(hdrs):
        table.rows[0].cells[i].text = h
        for p in table.rows[0].cells[i].paragraphs:
            for r in p.runs:
                r.bold = True
                r.font.size = Pt(9)
    for i, row in enumerate(rows_data):
        for j, val in enumerate(row):
            table.rows[i+1].cells[j].text = val
            for p in table.rows[i+1].cells[j].paragraphs:
                for r in p.runs:
                    r.font.size = Pt(8)

# E01
add_indicator_table(doc, 'E01 气候变化', [
    ['排放总量', 'greenhouse_gas_total_scope1', '数值(吨CO2e)', '43.07万吨'],
    ['排放总量', 'greenhouse_gas_total_scope2', '数值(吨CO2e)', '6.77万吨'],
    ['排放总量', 'greenhouse_gas_total_scope1_2', '数值(吨CO2e)', '49.84万吨'],
    ['排放总量', 'greenhouse_gas_total_scope3', '数值(吨CO2e)', 'null'],
    ['排放强度', 'emission_intensity_per_revenue', '数值', '0.123吨/万元'],
    ['排放强度', 'emission_intensity_denominator', '文本', '按营收/产量'],
    ['排放趋势', 'emission_yoy_change', '百分比', '+11.8%'],
    ['范围界定', 'scope_included', '文本', '范围一+范围二'],
    ['基准年', 'base_year/year/emission', '年份/数值', '2023/45.26万吨'],
    ['减排目标', 'reduction_target_list', 'JSON数组', '{"pct":25,"base":2023}'],
    ['碳达峰', 'carbon_peak_target', '年份+状态', '2030年前/已实现'],
    ['碳中和', 'carbon_neutral_target', '年份', '2050'],
    ['碳抵消', 'carbon_offset_volume', '数值(吨CO2e)', '10,000'],
    ['减排措施', 'emission_reduction_initiatives', 'JSON数组', '[措施列表]'],
])

# E02
add_indicator_table(doc, 'E02 能源利用', [
    ['总能耗', 'total_energy_consumption', '数值+万吨标准煤', '26.12'],
    ['分项能耗', 'energy_by_type', 'JSON对象', '天然气/电/柴油/汽油'],
    ['能耗结构', 'energy_direct/indirect', '数值+万吨标准煤', '22.86/3.25'],
    ['能源强度', 'energy_intensity', '数值', '0.0644'],
    ['可再生能源', 'renewable_energy', '数值+兆瓦时', '5,419'],
    ['清洁能源项目', 'clean_energy_projects', '数组', '[''沼气发电'']'],
    ['节能措施', 'energy_saving_measures', '数组', '[节能措施列表]'],
])

# E03
add_indicator_table(doc, 'E03 水资源', [
    ['取水量', 'water_withdrawal_total', '数值+万吨', '496.46'],
    ['水循环', 'water_recycle_rate', '百分比', '冷却水90%'],
    ['废水排放', 'wastewater_discharge_total', '数值+万吨', '496.46'],
    ['废水污染物', 'wastewater_cod/氨氮/总磷/总氮', '数值+吨', '118.92'],
    ['节水措施', 'water_saving_initiatives', '数组', '[措施列表]'],
])

# E04-E06
for item in [
    ('E04 污染物排放', '废气(NOx/SO2/PM/VOCs) + 废弃物(有害/无害) + 环保投入 + 合规处罚'),
    ('E05 循环经济', '包装材料(玻璃/塑料回收率) + 循环利用量 + 绿色设计'),
    ('E06 生物多样性', '政策文件 + 影响评估 + 保护措施 + 生态红线'),
]:
    doc.add_heading(item[0], level=3)
    doc.add_paragraph(item[1])

doc.add_page_break()

# S dimension
doc.add_heading('S -- 社会维度(Social)', level=2)
for item in [
    ('S01 员工权益', '员工结构(总数/性别/残障) + 薪酬福利 + 培训 + 流失率 + 民主管理'),
    ('S02 职业健康与安全', 'OHS认证 + 工伤指标(TRIR/死亡率) + 体检率 + 安全投入'),
    ('S03 客户与产品', '质量事故 + 客户满意度 + 隐私 + 研发投入 + 专利'),
    ('S04 供应链管理', '供应商ESG评估 + 绿色采购 + 供应链碳减排 + 供应商培训'),
    ('S05 社会贡献', '公益捐赠 + 志愿服务 + 乡村振兴(投入/惠及/项目)'),
]:
    doc.add_heading(item[0], level=3)
    doc.add_paragraph(item[1])

doc.add_page_break()

# G dimension
doc.add_heading('G -- 治理维度(Governance)', level=2)
for item in [
    ('G01 公司治理', '董事会构成 + ESG委员会 + ESG绩效挂钩'),
    ('G02 商业道德', '反腐败培训 + 腐败事件 + 行为准则 + 反不正当竞争'),
    ('G03 数据安全与风险管理', '数据安全认证 + 泄露事件 + 风控框架 + 内控审计'),
]:
    doc.add_heading(item[0], level=3)
    doc.add_paragraph(item[1])

doc.add_page_break()

# Prompt section
doc.add_heading('三、碳信息提取Prompt(v2.0, 4层框架)', level=1)
doc.add_paragraph('(完整Prompt JSON结构见附件ESG指标框架4层颗粒度.md，此处列出核心原则:)')

principles = [
    '输出格式: 4层嵌套JSON结构 (metadata -> E/S/G 三支柱 -> 主题 -> 字段)',
    '每字段格式: {"value": null, "unit": "", "year": null, "note": ""}',
    '只提取报告中明确陈述的事实，不推测',
    '保留原始单位并标注；模糊表述标注note',
    '数组字段: measures/targets/projects 使用JSON数组',
    '置信度: extraction_notes.confidence = "高/中/低"',
]
for p_text in principles:
    p = doc.add_paragraph(style='List Bullet')
    p.add_run(p_text)

doc.add_paragraph()
doc.add_paragraph('完整Prompt可直接粘贴到LLM，配合这批26份ESG报告(中文+英文)进行批量提取验证。')

doc.add_page_break()

# Changelog
doc.add_heading('四、变更记录', level=1)
cl_table = doc.add_table(rows=4, cols=3)
cl_table.style = 'Light Shading Accent 1'
for i, h in enumerate(['版本', '日期', '变更内容']):
    cl_table.rows[0].cells[i].text = h
for i, row in enumerate([
    ['v1.0', '2026-05-15', '原始6分类关键词体系(csmar_full_pipeline.py)'],
    ['v1.5', '2026-05-16', '6类一级/19类二级指标(指标体系框架.docx)'],
    ['v2.0', '2026-06-04', '4层颗粒度体系(L1-L4), 基于五粮液2025 ESG报告实际数据重构'],
]):
    for j, val in enumerate(row):
        cl_table.rows[i+1].cells[j].text = val

output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output',
                           'ESG指标框架4层颗粒度_v2.0.docx')
os.makedirs(os.path.dirname(output_path), exist_ok=True)
doc.save(output_path)
print(f'Saved: {output_path}')
