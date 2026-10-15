#!/usr/bin/env python3
"""Read old .doc format"""
import subprocess, os

doc_path = os.environ.get('DOC_PATH', '')
if not doc_path or not os.path.exists(doc_path):
    print('请用环境变量 DOC_PATH 指定待转换的 .doc 文件路径')
    raise SystemExit(1)

# Try antiword
try:
    result = subprocess.run(['antiword', doc_path], capture_output=True, text=True, timeout=10)
    if result.returncode == 0 and result.stdout.strip():
        print(result.stdout)
    else:
        print('antiword failed, trying catdoc...')
except:
    print('antiword not available, trying catdoc...')

# Try catdoc
try:
    result = subprocess.run(['catdoc', doc_path], capture_output=True, text=True, timeout=10)
    if result.returncode == 0:
        print(result.stdout)
    else:
        print(f'catdoc failed: {result.stderr}')
except:
    print('catdoc not available')

# Try libreoffice
try:
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')
    result = subprocess.run(
        ['libreoffice', '--headless', '--convert-to', 'txt:Text', '--outdir', out_dir, doc_path],
        capture_output=True, text=True, timeout=30
    )
    print(f'libreoffice stdout: {result.stdout}')
    print(f'libreoffice stderr: {result.stderr[:500]}')
except:
    print('libreoffice not available')

print('Done')
