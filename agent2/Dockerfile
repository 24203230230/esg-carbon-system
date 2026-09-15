# 智能体二 · 障碍度根因分析服务
# 双后端：deepseek（自主可控，默认）| adp（比赛展示）
# 基础镜像用本地已缓存的 python:3.13-slim（与 esg-extractor 一致，避免 Docker Hub 拉取失败）
FROM python:3.13-slim

WORKDIR /app

# 依赖
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 服务代码 + 双提示词（自包含，开源部署无需平台配置）
COPY suggest_service.py /app/
COPY system_prompt.md /app/
COPY user_prompt.md /app/
# LLM 网关多模型支持（v=78：one-api 统一接入）
COPY models.py /app/

# 容器内端口（宿主机映射 8083）
EXPOSE 8083

CMD ["python", "suggest_service.py", "--host", "0.0.0.0", "--port", "8083"]
