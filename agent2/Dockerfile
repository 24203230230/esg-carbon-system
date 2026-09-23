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
# v=100：检索层（Agent 1.5）—— 离线同行对标库（倒排索引 + 检索模块）
#   索引为构建产物（tools/build_peer_index.py 生成），随镜像发布 → 运行时零网络依赖
COPY peer_retrieval.py /app/
COPY peer_index.json /app/
# v=101：A 方案（联网检索企业公开信息）+ 确定性分析（适用性 / 改进收益）
#   web_search    : 东方财富公告 + 巨潮资讯检索（免费无 Key）；付费搜索 API 有 Key 才启用
#   materiality   : 纯 Python 计算；行业均值直接取「评分层已算好的值」，故无需 weights.json
COPY web_search.py /app/
COPY materiality.py /app/

# 容器内端口（宿主机映射 8083）
EXPOSE 8083

CMD ["python", "suggest_service.py", "--host", "0.0.0.0", "--port", "8083"]
