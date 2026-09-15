FROM python:3.13-slim

WORKDIR /app

# v=85：WeasyPrint 系统依赖（pango/cairo/gdk-pixbuf，PDF 渲染）
# 校园网 debian.org/清华源不通 → 换阿里云镜像源（已验证可达，5s 拉 8.9MB）
RUN cat > /etc/apt/sources.list.d/debian.sources << 'EOF'
Types: deb
URIs: https://mirrors.aliyun.com/debian
Suites: bookworm
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        shared-mime-info \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# v=85：中文思源黑体子集 → /usr/share/fonts/truetype/noto/（fontconfig 标准路径）
#   fc-cache 自动注册（实际名称 = Noto Sans CJK SC，HTML 模板用此名）
COPY templates/NotoSansSC-Regular.ttf /usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttf
COPY templates/NotoSansSC-Bold.ttf    /usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttf
RUN fc-cache -f

# 安装Python依赖（官方源已验证连通）
# pdfplumber 为纯Python实现，无需编译工具
# v=85：weasyprint（HTML→PDF 报告渲染）+ jinja2（模板填充）
RUN pip install --no-cache-dir \
        flask \
        requests \
        pdfplumber \
        python-docx \
        weasyprint \
        jinja2

# v=93：扫描件 OCR 兜底（RapidOCR + onnxruntime，完全离线，评委现场断网也能跑）
#   ⚠️ 坑1：必须用 opencv-python-headless —— slim 镜像无 libGL.so.1，普通版直接 ImportError
#   ⚠️ 坑2：rapidocr 的依赖写死 opencv_python，故装完须卸载再补 headless 版
#   ⚠️ 坑3：onnxruntime 依赖 libgomp1（OpenMP），见上方 apt 安装列表
#   ⚠️ 坑4：走清华源 —— 校园网直连 PyPI 官方源易超时
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
        rapidocr-onnxruntime \
    && pip uninstall -y opencv-python \
    && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
        opencv-python-headless

# 复制代码和提示词
COPY extract_service.py /app/
COPY extraction_prompt.md /app/
# LLM 网关多模型支持（v=78：one-api 统一接入）
COPY models.py /app/
# 评分查表数据（42指标权重+行业基准库+全市场中位数+评分规则）
COPY weights.json /app/
COPY weights_logic.py /app/
# v=85：WeasyPrint HTML 报告模板（Jinja2）
COPY templates/ /app/templates/

# 共享目录（供ADP容器读取提取结果）
RUN mkdir -p /app/output
VOLUME ["/app/output"]

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import requests; r = requests.get('http://localhost:8082/api/health', timeout=5); exit(0 if r.status_code == 200 else 1)" || exit 1

EXPOSE 8082

CMD ["python", "extract_service.py"]
