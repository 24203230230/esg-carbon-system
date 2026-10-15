#!/usr/bin/env python3
import os
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH

doc = Document()
style = doc.styles['Normal']
style.font.name = 'Arial'
style.font.size = Pt(10)

# ── Title ──
doc.add_heading('E维度碳信息提取指标框架', level=0).alignment = WD_ALIGN_PARAGRAPH.CENTER
doc.add_paragraph('').alignment = WD_ALIGN_PARAGRAPH.CENTER
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = p.add_run('（4层颗粒度 · 供应链碳画像方向 v1.0）')
r.font.size = Pt(14)
r.font.color.rgb = RGBColor(0, 102, 204)

doc.add_paragraph('')
m = doc.add_paragraph()
m.add_run('来源：').bold = True
m.add_run('老师供应链碳画像框架 + 五粮液2025 ESG报告样本')
doc.add_paragraph('版本：v1.0 | 2026-06-04')
doc.add_page_break()

# ── Section 1: Framework Overview ──
doc.add_heading('一、框架总览', level=1)

doc.add_paragraph('框架结构（4层颗粒度）：')
items = [
    'L1 画像维度 —— 行为 / 制度 / 承诺 / 管理机制',
    'L2 碳主题   —— 碳排放 / 能源 / 供应链碳 / 碳金融 / 废弃物 / 环境合规',
    'L3 主体行为 —— 企业自身 / 供应商 / 客户下游 / 行业生态',
    'L4 提取字段 —— 行为 | 主体 | 指标名 | 数值 | 单位 | 时间 | 证据原文',
]
for item in items:
    p = doc.add_paragraph(style='List Bullet')
    r = p.add_run(item)
    r.font.size = Pt(11)
    r.bold = True

# Definition table
doc.add_paragraph('')
doc.add_heading('画像四要素定义', level=2)
t = doc.add_table(rows=5, cols=3)
t.style = 'Light Shading Accent 1'
for i, h in enumerate(['要素', '含义', '例子']):
    t.rows[0].cells[i].text = h
rows_data = [
    ['行为', '实际执行的动作、项目、投资', '"建设沼气发电项目""更换低氮锅炉"'],
    ['制度', '建立的规则、标准、流程', '"制定绿色采购标准""建立供应商ESG准入制度"'],
    ['承诺', '对外公开的目标、承诺、宣言', '"2030年前碳达峰""2028年绿电占比50%"'],
    ['管理机制', '治理结构、监督考核、保障体系', '"设立ESG委员会""碳绩效纳入高管考核"'],
]
for i, row in enumerate(rows_data):
    for j, val in enumerate(row):
        t.rows[i+1].cells[j].text = val

doc.add_paragraph('')
doc.add_heading('L4统一提取格式', level=2)
doc.add_paragraph('{ "行为": "", "主体": "", "客体": "", "指标名": "", "数值": null, "单位": "", "时间": "", "画像要素": "行为|制度|承诺|管理机制", "证据原文": "" }')

doc.add_page_break()

# ── Section 2: Complete Tree ──
doc.add_heading('二、完整指标树', level=1)

def add_theme(doc, title, subtitle, records):
    doc.add_heading(title, level=2)
    if subtitle:
        doc.add_paragraph(subtitle).italic = True
    t = doc.add_table(rows=len(records)+1, cols=4)
    t.style = 'Light Grid Accent 1'
    for i, h in enumerate(['L4提取字段', '行为含义', '主体', '原文示例摘要']):
        t.rows[0].cells[i].text = h
        for p in t.rows[0].cells[i].paragraphs:
            for r in p.runs:
                r.bold = True
                r.font.size = Pt(9)
    for i, row in enumerate(records):
        for j, val in enumerate(row):
            t.rows[i+1].cells[j].text = val
            for p in t.rows[i+1].cells[j].paragraphs:
                for r in p.runs:
                    r.font.size = Pt(8)

def add_subtheme(doc, title, records):
    doc.add_heading(title, level=3)
    t = doc.add_table(rows=len(records)+1, cols=4)
    t.style = 'Light Grid Accent 1'
    for i, h in enumerate(['L4提取字段', '行为含义', '主体', '原文示例摘要']):
        t.rows[0].cells[i].text = h
        for p in t.rows[0].cells[i].paragraphs:
            for r in p.runs:
                r.bold = True
                r.font.size = Pt(9)
    for i, row in enumerate(records):
        for j, val in enumerate(row):
            t.rows[i+1].cells[j].text = val
            for p in t.rows[i+1].cells[j].paragraphs:
                for r in p.runs:
                    r.font.size = Pt(8)

# Theme 1: 碳排放
doc.add_heading('主题1：碳排放（Scope 1/2/3）', level=2)
add_subtheme(doc, 'L3-1 企业自身碳排放', [
    ['scope1_排放', '核算/披露范围一排放', '企业自身', '范围1温室气体排放量43.07万吨'],
    ['scope2_排放', '核算/披露范围二排放', '企业自身', '范围2温室气体排放量6.77万吨'],
    ['scope1+2_总量', '核算/披露排放总量', '企业自身', '温室气体排放总量49.84万吨'],
    ['scope3_排放', '核算/披露范围三排放', '企业自身', '暂未核算范围三排放'],
    ['碳排放强度', '计算/披露单位营收排放', '企业自身', '碳排放强度0.123吨/万元'],
    ['同比变化', '对比上年排放变化', '企业自身', '排放总量较上年增长11.8%'],
    ['基准年设定', '设定碳排放基准年份', '企业自身', '以2023年为排放基准年'],
    ['减排量核算', '核算实际减排量', '企业自身', '沼气发电减少CO2约4,000余吨'],
    ['碳抵消', '购买碳抵消/碳信用', '企业自身', '购买CCER碳信用10,000吨'],
    ['碳足迹核算', '产品全生命周期碳足迹', '企业自身', '对核心产品开展全生命周期碳足迹核算'],
])
add_subtheme(doc, 'L3-2 供应商碳排放管理', [
    ['供应商碳披露要求', '要求供应商披露碳数据', '企业->供应商', '要求主要供应商披露碳排放数据'],
    ['供应商碳目标', '要求供应商设定减排目标', '企业->供应商', '要求核心供应商设定碳中和目标'],
    ['供应商绿电要求', '要求供应商使用绿电', '企业->供应商', '要求供应商逐年提升绿电比例'],
    ['供应商碳审核', '对供应商进行碳审核', '企业->供应商', '完成12家主要供应商碳审核'],
    ['供应商碳培训', '对供应商开展碳管理培训', '企业->供应商', '开展供应商碳管理培训6次'],
])

doc.add_page_break()

# Theme 2: 能源
doc.add_heading('主题2：能源管理', level=2)
add_subtheme(doc, 'L3-1 自身能源消耗', [
    ['综合能耗', '核算年度综合能耗', '企业自身', '综合能源消耗量26.12万吨标准煤'],
    ['天然气消耗', '消耗/使用天然气', '企业自身', '天然气使用量1.88亿立方米'],
    ['电力消耗', '消耗/使用外购电力', '企业自身', '外购电力2.65亿千瓦时'],
    ['汽油消耗', '消耗汽油', '企业自身', '汽油用量47.87吨'],
    ['柴油消耗', '消耗柴油', '企业自身', '柴油用量131.68吨'],
    ['直接能耗', '直接能源消耗量', '企业自身', '直接能源消耗量22.86万吨标准煤'],
    ['间接能耗', '间接能源消耗量', '企业自身', '间接能源消耗量3.25万吨标准煤'],
    ['能源强度', '单位营收能耗', '企业自身', '能源使用强度0.0644万吨标准煤/亿元'],
    ['能耗同比变化', '能源消耗同比变化', '企业自身', '综合能耗较上年增长11.1%'],
])
add_subtheme(doc, 'L3-2 清洁能源与节能', [
    ['可再生能源使用', '使用/采购可再生能源', '企业自身', '清洁能源(沼气发电)5,419兆瓦时'],
    ['光伏发电', '建设/运营光伏项目', '企业自身', '楼顶光伏发电组件提供可再生能源'],
    ['沼气发电', '建设/运营沼气发电', '企业自身', '年处理沼气400万立方米'],
    ['节能改造', '实施节能技术改造', '企业自身', '天然气锅炉超低氮排放提标升级'],
    ['能效数字化', '建设能效管理平台', '企业自身', '数字化能源管理系统实时采集分析'],
    ['绿色建筑', '建设绿色认证建筑', '企业自身', '新建办公区按绿色建筑三星标准建设'],
])
add_subtheme(doc, 'L3-3 供应链能源协同', [
    ['供应商绿电采购', '推动供应商使用绿电', '企业->供应商', '鼓励供应商使用绿电并纳入考核'],
    ['物流运输优化', '优化物流降低运输能耗', '企业+供应商', '鼓励公铁联运、新能源运输'],
    ['运输碳减排', '运输环节碳减排量', '企业自身', '同比减少运输碳排放734吨'],
    ['包材减量', '减量包装/循环包装', '企业->供应商', '减少87万个包装袋，减碳约260吨'],
])

doc.add_page_break()

# Theme 3: 供应链碳
doc.add_heading('主题3：供应链碳管理', level=2)
add_subtheme(doc, 'L3-1 供应商ESG准入', [
    ['供应商ESG审核', '对供应商进行ESG审核', '企业->供应商', '对供应商质保能力、ESG表现开展评审'],
    ['绿色采购标准', '建立绿色采购标准', '企业自身', '将环境表现嵌入供应商全生命周期管理'],
    ['供应商行为准则', '制定供应商行为准则', '企业自身', '与供应商签署《行为准则》'],
    ['ESG纳入遴选', 'ESG表现作为遴选依据', '企业自身', '将ESG报告作为重要遴选依据'],
    ['供应商分级', '对供应商ESG分级管理', '企业自身', '临时/合格/优质/战略四级'],
    ['环保加分项', '评审设置环境加分项', '企业自身', '考察废水/废气/废渣/温室气体'],
    ['黑名单制度', '环境违规供应商清退', '企业自身', '对环境违规供应商坚决清退'],
])
add_subtheme(doc, 'L3-2 供应链协同减排', [
    ['供应商培训', '开展ESG培训', '企业->供应商', '开展供应商培训6次/84人次'],
    ['供应商激励', '优质供应商激励', '企业->供应商', '可持续发展奖获奖比例4.8%'],
    ['绿色物流', '共建绿色物流体系', '企业+供应商', '共建绿色物流，新能源运输'],
    ['循环供应链', '循环经济供应链', '企业+供应商', '包装闭环：设计-制造-使用-回收'],
    ['区域化采购', '缩短运输半径', '企业自身', '本地化采购比例89.58%'],
    ['供应链碳目标', '供应链减排目标', '企业自身', '供应链碳减排目标纳入年度绩效考核'],
])
add_subtheme(doc, 'L3-3 供应链透明度', [
    ['一级供应商披露', '披露一级供应商', '企业自身', '已披露前十大供应商'],
    ['采购地区披露', '披露采购地区', '企业自身', '川内基地占比53.2%'],
    ['产品碳足迹', '产品碳足迹计算', '企业自身', '开展全生命周期碳足迹核算'],
    ['第三方核查', '数据经第三方核查', '企业自身', '聘请第三方核查温室气体数据'],
    ['ISO认证', '获得ISO体系认证', '企业自身', 'ISO14001/ISO14064认证'],
    ['数据追溯系统', '建立数据追溯系统', '企业自身', '数字化平台实现碳排放数据实时追踪'],
])

doc.add_page_break()

# Theme 4: 碳金融
doc.add_heading('主题4：碳金融与碳资产管理', level=2)
add_subtheme(doc, '', [
    ['碳交易参与', '参与碳市场交易', '企业自身', '参与全国碳排放权交易市场'],
    ['碳配额管理', '管理碳配额/履约', '企业自身', '完成年度碳配额履约'],
    ['CCER开发', '开发/购买碳信用', '企业自身', '开发林业碳汇CCER项目'],
    ['绿色债券', '发行绿色债券', '企业自身', '发行绿色债券用于环保项目'],
    ['可持续挂钩贷款', '获可持续发展贷款', '企业自身', '利率与碳减排目标挂钩'],
    ['ESG评级', '获第三方ESG评级', '企业自身', '获MSCI ESG评级AA级'],
    ['碳资产收益', '碳资产创造经济收益', '企业自身', '回收创造经济价值约1,144.75万元'],
])

doc.add_page_break()

# Theme 5: 环境合规
doc.add_heading('主题5：环境合规与治理机制', level=2)
add_subtheme(doc, '', [
    ['ESG治理架构', '建立ESG管理架构', '企业自身', 'ESG执行委员会下设环境议题执行小组'],
    ['环境管理体系', '建立环境管理体系', '企业自身', '持续通过ISO 14001认证'],
    ['环保投资', '年度环保投入', '企业自身', '环保投入1.8亿元'],
    ['环保培训', '开展环保培训', '企业自身', '环保培训20次，5万人次'],
    ['环境处罚', '受环境行政处罚', '企业自身', '报告期内未受到环境行政处罚'],
    ['碳信息披露', '披露碳排放数据', '企业自身', '连续三年披露温室气体数据'],
    ['目标考核', '碳绩效纳入考核', '企业自身', '碳减排目标纳入高管薪酬绩效评定'],
    ['环保荣誉', '获环保相关荣誉', '企业自身', '获评环保创新型企业'],
])

doc.add_page_break()

# ── Prompt ──
doc.add_heading('三、提取Prompt（行为画像版 v1.0）', level=1)
doc.add_paragraph('（完整prompt见同目录MD文件，此处列出核心指令）')
prompt_lines = [
    '角色：供应链碳画像信息提取专家',
    '范围：仅ESG报告环境(E)章节，聚焦碳信息',
    '任务：识别文本中的四种画像要素——行为、制度、承诺、管理机制',
    '主体关系：企业自身做了什么 / 企业对供应商做了什么 / 供应商做了什么',
    '输出格式：每条记录为 {行为描述, 主体, 客体, 指标名, 数值, 单位, 时间, 画像要素, 证据原文}',
    '特别注意供应商相关的碳管理信息要标清主体关系',
]
for line in prompt_lines:
    p = doc.add_paragraph(style='List Bullet')
    p.add_run(line)

doc.add_page_break()

# ── Comparison ──
doc.add_heading('四、与之前版本的区别', level=1)
comp = doc.add_table(rows=6, cols=3)
comp.style = 'Light Shading Accent 1'
for i, h in enumerate(['对比项', '之前(v1.0~v1.5)', '现在(行为画像版)']):
    comp.rows[0].cells[i].text = h
comp_data = [
    ['范围', 'E/S/G 全维度', '仅E维度碳信息'],
    ['提取目标', '填数字指标', '识别行为/制度/承诺/机制'],
    ['输出格式', '嵌套JSON字段', '行为记录数组'],
    ['供应链', '轻量提及', '核心维度(准入/碳管理/协同/透明度)'],
    ['证据原文', '无', '每字段保留原文引用'],
]
for i, row in enumerate(comp_data):
    for j, val in enumerate(row):
        comp.rows[i+1].cells[j].text = val

doc.add_paragraph('')
doc.add_heading('五、变更记录', level=1)
cl = doc.add_table(rows=4, cols=3)
cl.style = 'Light Shading Accent 1'
for i, h in enumerate(['版本', '日期', '变更']):
    cl.rows[0].cells[i].text = h
cl_data = [
    ['v1.0', '2026-05-15', '原始6分类关键词体系'],
    ['v1.5', '2026-05-16', '6类一级/19类二级指标'],
    ['v2.0 行为画像版', '2026-06-04', '供应链碳画像方向，仅E维度，4层颗粒度+行为画像格式'],
]
for i, row in enumerate(cl_data):
    for j, val in enumerate(row):
        cl.rows[i+1].cells[j].text = val

# Save
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output',
                   'E维度碳信息4层颗粒度_供应链碳画像.docx')
os.makedirs(os.path.dirname(out), exist_ok=True)
doc.save(out)
print(f'Saved: {out}')
