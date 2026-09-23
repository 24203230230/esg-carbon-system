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

# v=111：中文思源黑体（**glyf/TrueType 轮廓**）→ /usr/share/fonts/truetype/noto/
#   fc-cache 自动注册（家族名 = Noto Sans CJK SC，HTML 模板用此名）
#   ⚠️ 必须是 glyf 轮廓，不能用 OTTO(CFF)：
#      旧文件扩展名叫 .ttf、内部是 OTTO，WeasyPrint 会嵌成 /CIDFontType0；
#      而它写正文时用的是 GID，CFF 的 charset 又非恒等映射（「中」GID=184 / CID=9544），
#      严格渲染器（iOS·macOS Quick Look·Adobe）按 CID 查不到字形 → 中文空白、
#      只有拉丁与数字可见（GID 恰好等于 CID）。详见 _perf/v111/ 下的排查脚本。
COPY templates/NotoSansCJKsc-Regular.ttf /usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.ttf
COPY templates/NotoSansCJKsc-Bold.ttf    /usr/share/fonts/truetype/noto/NotoSansCJKsc-Bold.ttf
RUN fc-cache -f

# 安装Python依赖
# pdfplumber 为纯Python实现，无需编译工具
# v=85：weasyprint（HTML→PDF 报告渲染）+ jinja2（模板填充）
# v=114：pip 源 清华 → 阿里云
#   ⚠️ 2026-09-20 实测：清华源对 pip 的请求直接返回 **403 Forbidden**
#      （`GET /simple/flask/ HTTP/1.1` → 403，pip 随即报
#       `Could not find a version that satisfies the requirement flask (from versions: none)`，
#       极易误判成"源地址写错"或"网络不通"）。同一时刻 urllib 手工 GET 却是 200，
#       且 aliyun / ustc 两个源 pip 均正常 ⇒ 换阿里云（与上方 apt 源同厂，一致好维护）。
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple \
        flask \
        requests \
        pdfplumber \
        python-docx \
        weasyprint \
        jinja2 \
        cos-python-sdk-v5

# v=93：扫描件 OCR 兜底（RapidOCR + onnxruntime，完全离线，评委现场断网也能跑）
#   ⚠️ 坑1：必须用 opencv-python-headless —— slim 镜像无 libGL.so.1，普通版直接 ImportError
#   ⚠️ 坑2：rapidocr 的依赖写死 opencv_python，故装完须卸载再补 headless 版
#   ⚠️ 坑3：onnxruntime 依赖 libgomp1（OpenMP），见上方 apt 安装列表
#   ⚠️ 坑4：pip 源同上方 —— v=114 起改阿里云（清华源 2026-09-20 起对 pip 返回 403）
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple \
        rapidocr-onnxruntime \
    && pip uninstall -y opencv-python \
    && pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple \
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
