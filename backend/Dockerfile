FROM python:3.13-slim

WORKDIR /app

# 安装Python依赖（官方源已验证连通）
# pdfplumber 为纯Python实现，无需编译工具，省略apt-get步骤
RUN pip install --no-cache-dir \
        flask \
        requests \
        pdfplumber \
        python-docx

# 复制代码和提示词
COPY extract_service.py /app/
COPY extraction_prompt.md /app/
# LLM 网关多模型支持（v=78：one-api 统一接入）
COPY models.py /app/
# 评分查表数据（42指标权重+行业基准库+全市场中位数+评分规则）
COPY weights.json /app/
COPY weights_logic.py /app/

# 共享目录（供ADP容器读取提取结果）
RUN mkdir -p /app/output
VOLUME ["/app/output"]

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import requests; r = requests.get('http://localhost:8082/api/health', timeout=5); exit(0 if r.status_code == 200 else 1)" || exit 1

EXPOSE 8082

CMD ["python", "extract_service.py"]
