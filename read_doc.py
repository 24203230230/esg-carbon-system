#!/usr/bin/env python3
"""Read old .doc format"""
import subprocess, os

doc_path = r'[LOCALAPPDATA]\Local\Temp\workbuddy-weixin-media\inbound\weixin-file-d34f407a532e3d1d-供应链碳画像 (1).doc'

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
    out_dir = r'[PROJECT]\output'
    result = subprocess.run(
        ['libreoffice', '--headless', '--convert-to', 'txt:Text', '--outdir', out_dir, doc_path],
        capture_output=True, text=True, timeout=30
    )
    print(f'libreoffice stdout: {result.stdout}')
    print(f'libreoffice stderr: {result.stderr[:500]}')
except:
    print('libreoffice not available')

print('Done')
