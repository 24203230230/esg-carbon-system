"""
CSMAR数据加载器 —— 从已下载的CSMAR数据中提取碳排放相关数据
"""
import os
import zipfile
import json
import csv
import logging
from typing import Optional
from io import BytesIO, TextIOWrapper

import openpyxl

from config import CSMAR_DATA_ROOT

logger = logging.getLogger(__name__)


class CSMARLoader:
    """加载已下载的CSMAR碳中和数据"""

    # CSMAR目录 → 内容描述
    CATEGORIES = {
        '碳排放': '上市公司碳排放数据',
        '碳治理': '上市公司环境治理/碳减排数据',
        '碳交易': '碳交易市场数据',
        '碳信贷': '碳信贷数据',
        '碳成分概念股': '碳成分概念股',
        '环境概览': '环境概览数据',
        '碳指数': '碳指数数据',
        '碳基金': '碳基金数据',
        '碳债券': '碳债券数据',
    }

    def __init__(self, data_root: str = CSMAR_DATA_ROOT):
        self.data_root = data_root

    def list_all_files(self) -> list:
        """列出所有可用的数据文件"""
        results = []
        for cat in self.CATEGORIES:
            cat_dir = os.path.join(self.data_root, cat)
            if not os.path.isdir(cat_dir):
                continue
            for f in sorted(os.listdir(cat_dir)):
                if f.endswith('.zip'):
                    results.append({
                        'category': cat,
                        'description': self.CATEGORIES[cat],
                        'file': f,
                        'path': os.path.join(cat_dir, f),
                    })
                elif f.endswith(('.xlsx', '.csv')):
                    results.append({
                        'category': cat,
                        'description': self.CATEGORIES[cat],
                        'file': f,
                        'path': os.path.join(cat_dir, f),
                    })
        return results

    def load_data(self, zip_path: str) -> Optional[dict]:
        """
        从zip中加载CSMAR数据
        CSMAR Excel格式:
          Row 1: 英文列名
          Row 2: 中文列名
          Row 3: 单位
          Row 4+: 实际数据
        返回: {'columns': [...], 'columns_cn': [...], 'units': [...], 'rows': [[...], ...]} 或 None
        """
        try:
            with zipfile.ZipFile(zip_path, 'r') as z:
                xlsx_files = [n for n in z.namelist() if n.endswith('.xlsx')]
                if not xlsx_files:
                    logger.warning(f'ZIP文件中找不到xlsx: {zip_path}')
                    return None

                # 注意: 不能用 read_only=True，否则某些列读取不全
                wb = openpyxl.load_workbook(
                    BytesIO(z.read(xlsx_files[0])), read_only=False, data_only=True
                )
                sheet = wb.active

                rows = list(sheet.iter_rows(values_only=True))
                if not rows:
                    return None

                # Row 1 (index 0): 英文列名
                columns = [str(c) if c is not None else '' for c in rows[0]]

                # Row 2 (index 1): 中文列名
                columns_cn = [str(c) if c is not None else '' for c in (rows[1] if len(rows) > 1 else [])]

                # Row 3 (index 2): 单位
                units = [str(c) if c is not None else '' for c in (rows[2] if len(rows) > 2 else [])]

                # Row 4+ (index 3+): 实际数据
                data_rows = []
                for row in rows[3:]:
                    data_rows.append(list(row))

                return {
                    'columns': columns,
                    'columns_cn': columns_cn,
                    'units': units,
                    'rows': data_rows,
                }

        except Exception as e:
            logger.error(f'加载失败 {zip_path}: {e}')
            return None

    def search_by_category(self, category: str, limit: int = 5) -> list:
        """按类别搜索数据"""
        cat_dir = os.path.join(self.data_root, category)
        if not os.path.isdir(cat_dir):
            return []

        results = []
        for f in sorted(os.listdir(cat_dir)):
            if not f.endswith('.zip'):
                continue
            full_path = os.path.join(cat_dir, f)
            data = self.load_data(full_path)
            if data:
                results.append({
                    'file': f,
                    'category': category,
                    'columns': data['columns'],
                    'columns_cn': data.get('columns_cn', []),
                    'units': data.get('units', []),
                    'sample_rows': data['rows'][:limit],
                    'total_rows': len(data['rows']),
                })
        return results

    def search_by_keyword(self, keyword: str, limit: int = 5) -> list:
        """在所有数据中搜索包含关键词的表"""
        results = []
        all_files = self.list_all_files()
        for f_info in all_files:
            if not f_info['file'].endswith('.zip'):
                continue
            data = self.load_data(f_info['path'])
            if data is None:
                continue
            # 检查列名是否包含关键词
            matched = False
            for col in data['columns']:
                if keyword.lower() in col.lower():
                    matched = True
                    break
            if not matched:
                # 检查前5行内容
                for row in data['rows'][:5]:
                    for cell in row:
                        if cell and keyword in str(cell):
                            matched = True
                            break
                    if matched:
                        break
            if matched:
                results.append({
                    'file': f_info['file'],
                    'category': f_info['category'],
                    'description': f_info['description'],
                    'columns': data['columns'],
                    'sample_rows': data['rows'][:limit],
                    'total_rows': len(data['rows']),
                })
        return results


# ---------------------------------------------------------------
# CLI测试入口
# ---------------------------------------------------------------
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    loader = CSMARLoader()

    print('=== 所有可用数据文件 ===')
    for f in loader.list_all_files():
        print(f'  [{f["category"]}] {f["file"]}')

    print('\n=== 碳排放数据样例 ===')
    for result in loader.search_by_category('碳排放', limit=3):
        print(f'\n--- {result["file"]} ({result["total_rows"]}条) ---')
        print(f'  列: {result["columns"]}')
        for i, row in enumerate(result['sample_rows']):
            print(f'  行{i+1}: {row}')
