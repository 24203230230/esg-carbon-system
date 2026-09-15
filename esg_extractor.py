#!/usr/bin/env python3
"""
ESG碳信息提取主流程
1. 尝试CNINFO API → 获取ESG文本数据
2. 加载CSMAR数据 → 获取结构化碳排放数据
3. 生成提取报告
"""
import json
import os
import sys
import logging
from datetime import datetime

# 确保上级目录在路径中
sys.path.insert(0, os.path.dirname(__file__))

from config import OUTPUT_DIR
from cninfo_client import CNINFOClient
from csmar_loader import CSMARLoader
from esg_prompt import ESG_EXTRACTION_SYSTEM_PROMPT

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger('esg_extractor')


# ================================================================
# 阶段1: 尝试从CNINFO获取ESG文本数据
# ================================================================
def phase1_fetch_cninfo() -> dict:
    """尝试调用CNINFO ESG API，返回抽取到的数据"""
    client = CNINFOClient()
    results = {}

    for name, method_name in [
        ('governance', 'fetch_governance'),
        ('environment', 'fetch_environment'),
        ('social', 'fetch_social'),
        ('factor_score', 'fetch_factor_score'),
    ]:
        try:
            logger.info(f'CNINFO: 获取{name}数据...')
            data = getattr(client, method_name)({'@limit': '10', 'format': 'json'})
            results[name] = data
            logger.info(f'  ✓ {name}: 获取成功')
        except PermissionError as e:
            logger.warning(f'  ✗ {name}: 权限不足 (需商业token)')
            results[name] = {'error': str(e)}
        except Exception as e:
            logger.warning(f'  ✗ {name}: {e}')
            results[name] = {'error': str(e)}

    return results


# ================================================================
# 阶段2: 从本地CSMAR数据加载
# ================================================================
def phase2_load_csmar_data(limit_per_file: int = 5) -> dict:
    """从CSMAR数据加载碳排放相关表格"""
    loader = CSMARLoader()

    results = {
        'summary': {},
        'tables': [],
    }

    # 优先加载碳排放和碳治理数据
    for category in ['碳排放', '碳治理', '碳交易']:
        cat_results = loader.search_by_category(category, limit=limit_per_file)
        if cat_results:
            results['summary'][category] = len(cat_results)
            results['tables'].extend(cat_results)
            for r in cat_results:
                logger.info(f'CSMAR [{category}]: {r["file"]} ({r["total_rows"]}行)')

    return results


# ================================================================
# 阶段3: 生成报告
# ================================================================
def phase3_generate_report(
    cninfo_data: dict,
    csmar_data: dict,
) -> str:
    """汇总分析结果，生成报告"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    report_lines = []
    report_lines.append(f"# ESG碳信息提取报告")
    report_lines.append(f"生成时间: {timestamp}")
    report_lines.append("")

    # ---- CNINFO状态 ----
    report_lines.append("## 一、CNINFO数据API状态")
    if cninfo_data:
        for name, data in cninfo_data.items():
            if 'error' in data:
                report_lines.append(f"- {name}: ❌ {data['error']}")
            else:
                records = data.get('records', [])
                report_lines.append(f"- {name}: ✅ 获取到 {len(records)} 条数据")
    else:
        report_lines.append("- 未连接到CNINFO API")

    # ---- CSMAR数据概览 ----
    report_lines.append("\n## 二、CSMAR本地数据概览")
    summary = csmar_data.get('summary', {})
    if summary:
        for cat, count in summary.items():
            report_lines.append(f"- {cat}: {count} 个数据表")
    else:
        report_lines.append("- 未找到CSMAR本地数据")

    # ---- 样例数据 ----
    report_lines.append("\n## 三、CSMAR数据样例")
    for table in csmar_data.get('tables', [])[:3]:
        report_lines.append(f"\n### {table['file']}")
        report_lines.append(f"类别: {table['category']}")
        report_lines.append(f"总行数: {table['total_rows']}")
        
        # 显示字段映射
        cols_en = table['columns']
        cols_cn = table.get('columns_cn', [])
        units = table.get('units', [])
        field_lines = []
        for i, en in enumerate(cols_en[:10]):
            cn = cols_cn[i] if i < len(cols_cn) else ''
            u = units[i] if i < len(units) else ''
            if cn or u:
                field_lines.append(f"  {en} ({cn}) [{u}]")
            else:
                field_lines.append(f"  {en}")
        report_lines.append("字段映射:")
        report_lines.extend(field_lines)
        if table['total_rows'] > 10:
            report_lines.append(f"... (共{table['total_rows']}行)")
        report_lines.append("\n样例数据 (前3行):")
        for i, row in enumerate(table.get('sample_rows', [])[:3]):
            report_lines.append(f"  {i+1}. {row}")

    # ---- 提取提示词 ----
    report_lines.append("\n---")
    report_lines.append("\n## 附: ESG碳信息提取Prompt")
    report_lines.append("\n```")
    report_lines.append(ESG_EXTRACTION_SYSTEM_PROMPT)
    report_lines.append("```")

    return '\n'.join(report_lines)


# ================================================================
# 入口
# ================================================================
def main():
    logger.info('=' * 60)
    logger.info('ESG碳信息提取系统 启动')
    logger.info('=' * 60)

    # 阶段1: CNINFO API
    logger.info('\n[阶段1] 尝试连接CNINFO数据API...')
    cninfo_data = phase1_fetch_cninfo()
    cninfo_has_data = any(
        'error' not in d for d in cninfo_data.values()
    )
    if cninfo_has_data:
        logger.info('  CNINFO API: 部分接口可用')
    else:
        logger.info('  CNINFO API: 无可用数据（需商业订阅token）')

    # 阶段2: CSMAR数据
    logger.info('\n[阶段2] 加载本地CSMAR数据...')
    csmar_data = phase2_load_csmar_data()
    logger.info(f'  加载了 {len(csmar_data["tables"])} 个数据表')

    # 阶段3: 生成报告
    logger.info('\n[阶段3] 生成报告...')
    report = phase3_generate_report(cninfo_data, csmar_data)

    report_path = os.path.join(OUTPUT_DIR, 'esg_data_report.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    logger.info(f'  报告已保存: {report_path}')

    # 保存JSON格式的原始数据
    json_path = os.path.join(OUTPUT_DIR, 'csmar_sample_data.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(csmar_data, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f'  JSON数据已保存: {json_path}')

    print(f'\n✅ 提取完成')
    print(f'   报告: {report_path}')
    print(f'   数据: {json_path}')


if __name__ == '__main__':
    main()
