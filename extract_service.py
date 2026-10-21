#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESG碳信息提取服务（Web API沙箱）
=================================

提供两个核心接口：
1. POST /api/extract         - 接收PDF/Word → 提取为结构化JSON
2. GET  /api/health          - 健康检查
3. POST /api/extract_score   - 提取+评分一站式（评分需ADP接入）

调用流程：
  前端 → POST /api/extract (上传文件) → 提取为JSON
       ↓
  智能体一提示词 + DeepSeek API
       ↓
  返回: {报告元数据, 指标提取结果{4维度42指标}}
       ↓
  前端 → 传给 ADP 评分智能体二

启动：python extract_service.py [--port 8082]
环境变量：
  DEEPSEEK_API_KEY      - DeepSeek API Key（必需）
  DEEPSEEK_BASE_URL     - DeepSeek API地址（默认 https://api.deepseek.com）
  DEEPSEEK_MODEL        - 模型名（默认 deepseek-chat）
  PORT                  - 监听端口（默认 8082）
"""
import os
import sys
import json
import logging
import argparse
import time
import traceback
import threading
import uuid
from pathlib import Path
from typing import Optional

# 第三方依赖
try:
    import pdfplumber
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

try:
    from docx import Document as DocxDocument
    DOCX_SUPPORT = True
except ImportError:
    DOCX_SUPPORT = False

# v=93：扫描件 OCR 兜底（RapidOCR + onnxruntime，完全离线、零成本）
#   仅当文字层提不出内容时才加载模型 → 96.8% 的正常报告零开销
_OCR_IMPORT_ERROR = ''      # 防御性初始化：无论导入成败都保证该名字存在
try:
    import numpy as np
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None   # 扫描件单页位图极大，解除像素上限保护
    from rapidocr_onnxruntime import RapidOCR
    OCR_SUPPORT = True
except ImportError as _ocr_err:
    OCR_SUPPORT = False
    _OCR_IMPORT_ERROR = str(_ocr_err)

import requests
from flask import Flask, request, jsonify, send_file

# v=85：WeasyPrint PDF 报告渲染（HTML→PDF）
try:
    from weasyprint import HTML
    WEASYPRINT_SUPPORT = True
except ImportError:
    WEASYPRINT_SUPPORT = False

try:
    from jinja2 import Template
    JINJA_SUPPORT = True
except ImportError:
    JINJA_SUPPORT = False

# ============================== 日志 ==============================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger('extract_service')

# ============================== 服务实例 ==============================
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB上限（ESG报告PDF可能很大）

# ============================== 配置 ==============================
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_BASE_URL = os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
DEEPSEEK_MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash')
# 深度思考模式：enabled 开启 | disabled 关闭（提取任务默认关闭，速度更快更便宜）
DEEPSEEK_THINKING = os.environ.get('DEEPSEEK_THINKING', 'disabled')

# 提示词路径（容器内 /app；本地运行可用 EXTRACTION_PROMPT_FILE 指向提示词文件）
PROMPT_FILE = os.environ.get('EXTRACTION_PROMPT_FILE', '/app/extraction_prompt.md')

# ============================== OCR 兜底配置（v=93） ==============================
# 扫描件（纯图片 PDF）无文字层，原逻辑直接 400。改为自动转 OCR，对上层透明。
#
# v=124：三个参数改为**环境变量可覆盖**（原先写死 ⇒ 调优要重打镜像）。
#   实测场景（宜宾五粮液 英文版，59 页纯扫描件）整份 OCR 812s ≈ 13.8s/页 —— 用户侧体感 16 分钟。
def _env_int(name, default):
    v = os.environ.get(name, '').strip()
    return int(v) if v.isdigit() else default


OCR_MIN_TEXT = 100        # 文字层低于此字符数视为扫描件（与 /api/extract 判定阈值一致）
# 降采样目标宽度：实测精度仅降 4.5%，速度快 4.8 倍（甜点值）。
# 调小可线性提速（耗时∝面积）；报告正文 10pt 在 1500px 下仍有 ~21px 字高，余量充足。
OCR_TARGET_WIDTH = _env_int('OCR_TARGET_WIDTH', 1800)
OCR_MAX_PAGES = _env_int('OCR_MAX_PAGES', 60)   # 单份最多识别页数，防超厚报告无限等待
# 无内嵌位图时的整页渲染分辨率（v=124 起可调；A4 在 150DPI ≈ 1240px 宽，
# 低于 OCR_TARGET_WIDTH ⇒ 该路径本来就不会再缩放，故 DPI 直接决定耗时）。
OCR_RENDER_DPI = _env_int('OCR_RENDER_DPI', 150)

# v=97：字体编码损坏检测。判定从「只看字符数量」升级为「数量 + 质量」。
# 依据（30 份实验实测）：受损 4 份最低 4.36%，完好 26 份最高 0.07% → 60 倍安全间隔，
# 正常报告不会误触（OCR 约 7s/页、上限 60 页，误触代价高）。
OCR_NOISE_MAX_RATIO = 1.0  # 噪声占比上限(%)，超过则判定文字层不可用

# v=98：文字层「稀薄」检测 —— OCR 兜底的第三条判据。
# 背景：振德医疗 32.5MB / 67 页，文字层仅 5,731 字符（85.5 字符/页），
#       字符数 > OCR_MIN_TEXT(100) 且噪声率低 → 旧两条判据都不触发，**绕过 OCR**，
#       42 个指标全填「未披露」，覆盖率 11.9% → 0%。
# 依据（30 份实测）：需要触发的只有 1 份（振德医疗 85.5 字符/页），
#       次低为 620.1 字符/页 → **7.3 倍安全间隔**，正常报告不会误触。
# 注：受损 4 份是 1,065~7,231 字符/页（"量"足"质"坏），由噪声率判据接管，互不冲突。
OCR_MIN_CHARS_PER_PAGE = 200  # 每页平均字符数低于此值视为文字层稀薄

# v=97：判定页内图片是否为「整页扫描图」的面积阈值。
# 背景：原 _ocr_one_page 无条件取 page.images[0]，对扫描件（每页即一整张 JPEG）没问题，
# 但对**文字层编码损坏的文本 PDF** 会抽到页内插图/Logo —— 实测深纺织Ａ 42 页只识别出
# 285 字符、成都华微 45 万字符缩水到 1.3 万，OCR 等于白跑。低于该比例一律改为渲染整页。
OCR_EMBED_MIN_AREA_RATIO = 0.6
# onnxruntime 默认吃满全部核心；如需给 agent2 留余量，可设环境变量限制线程数
# （实测 12 核已被吃满，并行/限流均无收益，故默认不限制）
_ocr_threads = os.environ.get('OCR_NUM_THREADS', '').strip()
OCR_NUM_THREADS = int(_ocr_threads) if _ocr_threads.isdigit() else None

_OCR_ENGINE = None        # 惰性单例：只有真正遇到扫描件才初始化模型


def get_ocr_engine():
    """惰性加载 OCR 引擎（首次调用约需数秒初始化，之后复用）"""
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        if not OCR_SUPPORT:
            raise RuntimeError(f"OCR 依赖未安装: {_OCR_IMPORT_ERROR}")
        logger.info("🔧 初始化 RapidOCR 引擎（首次使用，加载模型）…")
        t0 = time.time()
        if OCR_NUM_THREADS:
            try:
                _OCR_ENGINE = RapidOCR(intra_op_num_threads=OCR_NUM_THREADS)
            except TypeError:
                logger.warning("当前 RapidOCR 版本不支持线程数参数，使用默认值")
                _OCR_ENGINE = RapidOCR()
        else:
            _OCR_ENGINE = RapidOCR()
        logger.info(f"✅ OCR 引擎就绪（耗时 {time.time() - t0:.1f}s）")
    return _OCR_ENGINE


def _encoding_noise_ratio(text: str) -> float:
    """字体编码损坏率（%）：PDF 字体缺少正确 CMap/ToUnicode 映射导致的噪声占比。

    v=97 新增。两类表现（与评估器 _perf/extract_quality.py 同公式，便于前后对照）：
      ① `(cid:NNNNN)` 占位符 —— CMap 缺失，每处约 11 字符
      ② U+FFFD(`�`)         —— 字符映射失败

    为什么需要它：这类文件文字层「长度足够但内容是坏的」，只按字符数判定会漏判，
    坏字符被直接喂给 LLM 后会诱发模型自行编造数字（实测该组数字准确率仅 61.6%，
    且结果结构完整、数值齐全，不核对发现不了）。
    """
    n = len(text)
    if not n:
        return 0.0
    noise = text.count('(cid:') * 11 + text.count('\ufffd')
    return noise * 100.0 / n


# ============================== 加载提示词 ==============================
def load_extraction_prompt() -> str:
    """加载智能体一提取提示词（启动时加载一次）"""
    candidates = [
        PROMPT_FILE,
        os.path.join(os.path.dirname(__file__), 'extraction_prompt.md'),
        'extraction_prompt.md',
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                prompt = f.read()
            logger.info(f"✅ 提示词已加载: {path} ({len(prompt)} 字符)")
            return prompt
    raise FileNotFoundError(
        f"找不到提取提示词！请确认 extraction_prompt.md 在以下路径之一：{candidates}"
    )

EXTRACTION_PROMPT = load_extraction_prompt()


# ============================== 文档解析 ==============================
def _safe_progress(cb, **fields):
    """进度回调兜底包装（v=94）

    异步任务用它把 OCR 页进度暴露给前端轮询。回调本身出错绝不能影响主流程，
    因此整体 try 包裹；cb 为 None 时零开销（同步路径不受任何影响）。
    """
    if not cb:
        return
    try:
        cb(**fields)
    except Exception as e:
        logger.warning(f"进度回调异常（已忽略）: {e}")


_PDF_CACHE_EVERY = 5


def _release_pdf_cache(pdf, page_index=None, full=False):
    """逐页释放 pdfplumber / pdfminer 的缓存（v121，纯内存管理）

    ⚠️ 不改变任何解析结果：缓存 miss 时按需重建，内容逐页一致（已实测比对）。

    背景（实测数据，2026-09-27）：一份 52 页「文字转曲」PDF 在 pdfplumber 下
    进程 RSS 从 80MB **线性涨到 2281MB（约 44MB/页）** —— pdfminer 会为每页
    缓存解压后的内容流/图像流对象，且跨页累积、从不释放。后果：
      · 2 核 2G 的线上机器在第 30~40 页触发内核 OOM（dmesg: anon-rss 1.6GB，
        esg-extractor 容器被杀重启 → 内存态任务表清空 → 客户端轮询全 404）；
      · 或陷入 swap 抖动：CPU 仅 3%，单页 OCR 从 7.4s 恶化到 65s（9 倍）。
    修复后同一份文件：峰值 **2319MB → 235MB（降 90%）**，耗时不变。
    """
    try:
        if page_index is not None:
            pdf.pages[page_index].flush_cache()
    except Exception:
        pass
    if full:
        try:
            pdf.doc._cached_objs.clear()
        except Exception:
            pass


def _ocr_one_page(page, ocr) -> str:
    """单页 OCR：整页扫描图直接抽取，否则渲染整页

    v=97 修正：原逻辑无条件取 `page.images[0]`。对**扫描件**没问题（每页即一整张
    JPEG/PNG），但对**文字层编码损坏的文本 PDF** 会抽到页内插图/Logo，OCR 等于白跑
    （实测深纺织Ａ 42 页只出 285 字符）。现改为：仅当内嵌图覆盖页面 ≥60% 面积
    （即整页扫描图）才直接抽取，否则渲染整页。

    返回识别到的纯文本（已按 RapidOCR 的阅读顺序拼接）。
    """
    import io
    img = None

    # 路径一：内嵌图接近整页 → 扫描件（省掉「渲染页面」环节，无需 poppler）
    #         内嵌图远小于整页 → 页内插图/Logo，不可用于 OCR，必须走渲染
    try:
        imgs = page.images
        if imgs:
            def _area(im):
                w = im.get('x1', 0) - im.get('x0', 0)
                h = im.get('bottom', 0) - im.get('top', 0)
                return max(0.0, w) * max(0.0, h)

            im = max(imgs, key=_area)
            page_area = float(page.width) * float(page.height)
            ratio = (_area(im) / page_area) if page_area > 0 else 0.0
            if ratio >= OCR_EMBED_MIN_AREA_RATIO:
                raw = im["stream"].get_data()
                img = Image.open(io.BytesIO(raw))
            else:
                logger.info(
                    f"页内图片仅占页面 {ratio:.1%}（阈值 {OCR_EMBED_MIN_AREA_RATIO:.0%}）"
                    f"，判定为插图 → 改为渲染整页"
                )
    except Exception as e:
        logger.warning(f"抽取内嵌图片失败，尝试页面渲染: {e}")

    # 路径二：兜底渲染页面位图（处理「文字转曲线」的矢量页，无内嵌位图）
    if img is None:
        try:
            img = page.to_image(resolution=OCR_RENDER_DPI).original
        except Exception as e:
            logger.warning(f"页面渲染失败，跳过该页: {e}")
            return ""

    # 降采样：扫描件原图常为 4961×3366（约 600DPI），按原尺寸 OCR 慢 4.8 倍。
    #
    # v=124：⚠️ 曾试 `img.draft()` + BILINEAR 提速，**实测后已回退**（证据见 _perf/v124_D/）。
    #   ① 提速不可测：同一夹具 4.075s/页 → 4.07~4.53s/页。
    #      （同容器重复实验逐字节一致 ⇒ OCR 本身是确定性的，故 0 收益不是噪声掩盖的）
    #   ② 识别结果**有损**：同一页整行消失（相似度 0.7606，
    #      "Water withdrawal: 2,345,678.90 tonnes; ..." 那行在新代码下没有输出）
    #      ⇒ 文本变少 ⇒ 分数被系统性压低。这与"不碰原逻辑/不做有损修复"的红线冲突。
    #   ③ 根因：送入 OCR 的**像素尺寸**虽未变，但 draft 改了解码的 DCT 比例、BILINEAR 改了
    #      重采样核 ⇒ 位图内容变了，而 OCR 对像素非尺度不变。
    #   保留（均为零行为变更的可用性改进）：
    #      · OCR_TARGET_WIDTH / OCR_MAX_PAGES / OCR_RENDER_DPI 三个环境变量旋钮（默认值同旧版）
    #      · OCR 进度日志补 ETA
    try:
        if img.width > OCR_TARGET_WIDTH:
            ratio = OCR_TARGET_WIDTH / img.width
            img = img.resize(
                (OCR_TARGET_WIDTH, max(1, int(img.height * ratio))),
                Image.LANCZOS
            )
        arr = np.array(img.convert("RGB"))
    except Exception as e:
        logger.warning(f"图片预处理失败，跳过该页: {e}")
        return ""

    try:
        result, _elapse = ocr(arr)
    finally:
        # 逐页释放，避免多页大图同时驻留内存（单页解码可达 150MB）
        del arr
        del img

    if not result:
        return ""
    # result 结构: [[box, text, score], ...]
    return "\n".join(str(r[1]) for r in result if len(r) > 1 and r[1])


def ocr_pdf_pages(file_bytes: bytes, max_pages: int = OCR_MAX_PAGES,
                  progress_cb=None) -> str:
    """扫描件 OCR 兜底：逐页识别并拼接文本

    ⚠️ 必须串行：实测 onnxruntime 本身已用满全部 CPU 核心，
       多进程并行只会互抢资源，单页耗时从 7.4s 恶化到 30-79s。

    progress_cb: 可选回调，签名 cb(stage=..., current=..., total=...)，用于异步任务报进度。
    """
    import io
    ocr = get_ocr_engine()
    text_parts = []
    total = 0
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        total = len(pdf.pages)
        limit = min(total, max_pages)
        if total > max_pages:
            logger.warning(
                f"扫描件共 {total} 页，超过上限 {max_pages} 页，仅识别前 {max_pages} 页"
            )
        _safe_progress(progress_cb, stage='ocr', current=0, total=limit)
        t0 = time.time()
        for i in range(limit):
            try:
                page_text = _ocr_one_page(pdf.pages[i], ocr)
                if page_text.strip():
                    text_parts.append(f"[第{i+1}页]\n{page_text}")
            except Exception as e:
                logger.warning(f"OCR 第{i+1}页失败: {e}")
            # v121：逐页释放缓存。扫描件 OCR 是本链路的内存峰值来源
            #（实测 52 页累积到 2.3GB；逐页释放后稳定在 235MB），
            # 必须在长循环里释放，否则 2G 机器必然 OOM/换页抖动。
            _release_pdf_cache(pdf, i, full=((i + 1) % _PDF_CACHE_EVERY == 0))
            # 每页都回报进度（前端轮询据此渲染 "OCR 12/49 页"）
            _safe_progress(progress_cb, stage='ocr', current=i + 1, total=limit)
            # 每 5 页报一次进度，便于从容器日志观察长任务
            if (i + 1) % 5 == 0 or (i + 1) == limit:
                elapsed = time.time() - t0
                # v=124：补 ETA —— 整份扫描件动辄十几分钟，日志里只有"已耗时"
                # 时无法判断是卡住还是正常推进（用户侧曾反馈"OCR 跑了 16 分钟"）。
                eta = elapsed / (i + 1) * (limit - i - 1)
                logger.info(
                    f"⏳ OCR 进度: {i+1}/{limit} 页，已耗时 {elapsed:.0f}s"
                    f"（{elapsed/(i+1):.1f}s/页），预计剩余 {eta:.0f}s"
                )
    logger.info(f"🔍 OCR 扫描完成: {total} 页中共识别 {len(text_parts)} 页有效文本")
    return "\n\n".join(text_parts)


def extract_text_from_pdf(file_bytes: bytes, progress_cb=None) -> str:
    """使用 pdfplumber 提取 PDF 文本（中文PDF兼容）

    v=93：文字层提不出内容时自动转 OCR 兜底（扫描件），对上层调用方完全透明。
    v=94：新增 progress_cb（可选），把解析/OCR 阶段与页进度透出给异步任务。
    """
    if not PDF_SUPPORT:
        raise RuntimeError("缺少 pdfplumber 依赖")
    import io
    _safe_progress(progress_cb, stage='parse', current=0, total=0)
    text_parts = []
    page_count = 0
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        page_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            try:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    text_parts.append(f"[第{i+1}页]\n{page_text}")
            except Exception as e:
                logger.warning(f"PDF第{i+1}页提取失败: {e}")
            # v121：逐页释放 pdfminer 缓存，避免内存随页数线性累积（见 _release_pdf_cache）
            _release_pdf_cache(pdf, i, full=((i + 1) % _PDF_CACHE_EVERY == 0))
    text = "\n\n".join(text_parts)

    # 扫描件/坏编码/稀薄文字层兜底 → 自动 OCR（OCR 渲染成图识别，不依赖 PDF 字体表）
    # v=97：判定从「只看字符数量」升级为「数量 + 质量」。字体编码损坏的文件文字层
    #        长度充足，原逻辑会漏判，把坏字符直接喂给 LLM 诱发编造数字。
    # v=98：再补第三条「密度」判据。文字层非零但极稀薄的报告（如 67 页仅 5,731 字符）
    #        同样会被原判据漏掉，且噪声率也正常 → 必须按「每页平均字符数」识别。
    text_noise = _encoding_noise_ratio(text)
    text_chars = len(text.strip())
    chars_per_page = (text_chars / page_count) if page_count else float(text_chars)
    is_scan = text_chars < OCR_MIN_TEXT
    is_broken = text_noise > OCR_NOISE_MAX_RATIO
    is_thin = page_count > 0 and chars_per_page < OCR_MIN_CHARS_PER_PAGE
    if is_scan or is_broken or is_thin:
        if not OCR_SUPPORT:
            logger.warning(
                f"检测到扫描件/坏编码文字层，但 OCR 依赖未安装（{_OCR_IMPORT_ERROR}），跳过兜底"
            )
            return text
        if is_scan:
            _why = f"文字层仅 {text_chars} 字符，判定为扫描件"
        elif is_thin:
            _why = (f"文字层仅 {chars_per_page:.0f} 字符/页"
                    f"（{text_chars} 字符 / {page_count} 页）< {OCR_MIN_CHARS_PER_PAGE}，"
                    f"判定为文字层稀薄")
        else:
            _why = (f"文字层编码损坏率 {text_noise:.2f}% > {OCR_NOISE_MAX_RATIO}%，"
                    f"判定为字体编码异常")
        logger.info(f"🔍 {_why} → 启动 OCR 兜底")
        _safe_progress(progress_cb, stage='ocr', current=0, total=0)
        try:
            ocr_text = ocr_pdf_pages(file_bytes, progress_cb=progress_cb)
        except Exception as e:
            logger.error(f"OCR 兜底失败: {e}")
            logger.error(traceback.format_exc())
            return text

        # v=97：采用条件按触发原因分流。
        # 坏编码场景必须比「噪声率」——`(cid:NNNNN)` 每处占约 11 字符会虚增原文长度，
        # 干净的 OCR 结果反而「更短」，原长度判据在此必然返回 False（OCR 白跑被丢弃）。
        # 扫描件场景维持原「比长度」判据，行为与 v=96 完全一致。
        ocr_noise = _encoding_noise_ratio(ocr_text)
        if is_broken:
            improved = ocr_noise < text_noise
            _cmp = f"噪声率 {text_noise:.2f}% → {ocr_noise:.2f}%"
        else:
            improved = len(ocr_text.strip()) > len(text.strip())
            _cmp = f"字符数 {len(text.strip())} → {len(ocr_text.strip())}"

        if is_broken and ocr_noise > OCR_NOISE_MAX_RATIO:
            # 本机救不回：落日志标记（第三层 vision 兜底的唯一接入点）
            logger.warning(
                f"⚠️ OCR 后噪声率仍为 {ocr_noise:.2f}%，本机无法救回，该份数值可信度低"
            )
        if improved:
            logger.info(f"✅ OCR 兜底成功: {_cmp}，输出 {len(ocr_text)} 字符")
            return ocr_text
        logger.warning(f"OCR 未改善文字层质量（{_cmp}），返回原始文字层结果")

    return text


def extract_text_from_docx(file_bytes: bytes) -> str:
    """使用 python-docx 提取 Word 文本"""
    if not DOCX_SUPPORT:
        raise RuntimeError("缺少 python-docx 依赖")
    import io
    doc = DocxDocument(io.BytesIO(file_bytes))
    text_parts = []
    for para in doc.paragraphs:
        if para.text.strip():
            text_parts.append(para.text)
    # 提取表格
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(cell.text.strip() for cell in row.cells)
            if row_text.strip():
                text_parts.append(row_text)
    return "\n".join(text_parts)


def extract_text_from_file(filename: str, file_bytes: bytes, progress_cb=None) -> str:
    """根据文件类型路由到对应解析器（v=94：progress_cb 可选，默认 None 时行为不变）"""
    ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
    if ext == 'pdf':
        return extract_text_from_pdf(file_bytes, progress_cb=progress_cb)
    elif ext in ('docx', 'doc'):
        return extract_text_from_docx(file_bytes)
    else:
        raise ValueError(f"不支持的文件类型: .{ext}（仅支持 .pdf/.docx/.doc）")


# ============================== LLM 调用（one-api 网关，v=78） ==============================
# 统一走 models.py 的 call_llm → http://esg-one-api:3000/v1 → 按 model 路由到对应 provider
# 保留 call_deepseek 作为兼容别名（历史调用点），实际走 one-api 网关
def call_deepseek(system_prompt: str, user_text: str, max_retries: int = 2) -> dict:
    """兼容别名：调用 one-api 网关（默认 deepseek-v4-flash）"""
    from models import call_llm
    return call_llm(system_prompt, user_text, model_id='deepseek-v4-flash', max_retries=max_retries)


# ============================== 4 维度结构兜底 ==============================
def _strip_md_fence(text: str) -> str:
    """剥掉外层 Markdown 代码栅栏（```json … ```）。

    v=204b：MiniMax M3 **无视** response_format=json_object，即便要求"只输出 JSON"，
    仍会把结果包进 ```json 围栏 ⇒ 提取侧直接 json.loads 必然失败，表现为
    「选 MiniMax 就报 LLM返回非JSON格式」。agent2 侧（suggest_service）早就有这层
    兜底，提取侧一直没做 —— 这里补齐，只做"剥壳"，不改任何判定逻辑。
    非栅栏文本原样返回（调用方据此判断是否发生了剥离）。
    """
    t = (text or '').strip()
    if not t.startswith('```'):
        return text
    lines = t.split('\n')
    if len(lines) < 2:
        return text
    inner = lines[1:-1] if lines[-1].strip().startswith('```') else lines[1:]
    return '\n'.join(inner).strip()


def normalize_extracted_structure(data: dict) -> dict:
    """保证 LLM 返回的 JSON 结构完整：4 维度嵌套 + 报告元数据齐备。

    LLM 偶尔抽风：拍扁结构、缺维度、漏报元数据。这里统一修补，并保留真实数据。
    """
    DIMS = ['企业内部碳管理', '供应链碳管理', '透明度', '金融化情况']
    EMPTY_DIM = {"指标": {}, "未覆盖指标": []}

    if not isinstance(data, dict):
        return {
            '报告元数据': {'企业名称': '', '报告名称': '', '报告年份': '', '所属行业': '',
                       '主营业务': '', '企业性质': '', '营收规模': '', '报告页码/章节': ''},
            '指标提取结果': {d: dict(EMPTY_DIM) for d in DIMS}
        }

    # 智能定位数据所在层级
    if '指标提取结果' in data:
        # LLM 直接返回 报告元数据+指标提取结果 在根（按 prompt 5.1 节）
        dims_root = data
    elif 'extracted' in data and isinstance(data.get('extracted'), dict):
        if '指标提取结果' in data['extracted'] or '报告元数据' in data['extracted']:
            # LLM 包了一层 extracted
            data = data['extracted']
            dims_root = data
        else:
            dims_root = data['extracted']
    else:
        # 完全没有结构
        data = {
            '报告元数据': {'企业名称': '', '报告名称': '', '报告年份': '', '所属行业': '',
                       '主营业务': '', '企业性质': '', '营收规模': '', '报告页码/章节': ''},
            '指标提取结果': {}
        }
        dims_root = data

    # 报告元数据兜底（含企业画像字段：供 Agent2 说明「这是什么企业、为什么这样做」）
    # 纯补齐缺失键，不覆盖 LLM 已返回的任何值
    _META_DEFAULTS = {
        '企业名称': '', '报告名称': '', '报告年份': '',
        '所属行业': '', '主营业务': '', '企业性质': '', '营收规模': '',
        '报告页码/章节': '',
    }
    dims_root.setdefault('报告元数据', dict(_META_DEFAULTS))
    if isinstance(dims_root.get('报告元数据'), dict):
        for _k, _v in _META_DEFAULTS.items():
            dims_root['报告元数据'].setdefault(_k, _v)

    # 指标提取结果兜底
    dims_root.setdefault('指标提取结果', {})
    dims_root = dims_root['指标提取结果']

    # 检测 4 维度嵌套
    has_dims = all(d in dims_root for d in DIMS)

    # 拍扁模式：叶子指标直接出现在 dims_root 根下
    if not has_dims:
        if not dims_root:
            for d in DIMS:
                dims_root[d] = dict(EMPTY_DIM)
        else:
            leaf_dump = {}
            for k, v in list(dims_root.items()):
                leaf_dump[k] = v
                del dims_root[k]
            dims_root['企业内部碳管理'] = {"指标": leaf_dump, "未覆盖指标": []}
            for d in DIMS:
                if d not in dims_root:
                    dims_root[d] = dict(EMPTY_DIM)
            logger.warning(f"⚠️ LLM 输出结构被拍扁→自动归位到「企业内部碳管理」（其余维度空补全）")

    # 保证 4 维度都存在
    for d in DIMS:
        if not isinstance(dims_root.get(d), dict):
            dims_root[d] = dict(EMPTY_DIM)
        else:
            dims_root[d].setdefault('指标', {})
            dims_root[d].setdefault('未覆盖指标', [])

    total = sum(sum(len(v) for v in dims_root[d].get('指标', {}).values() if isinstance(v, dict)) for d in DIMS)
    logger.info(f"✅ 规范化完成: 4 维度齐全, 叶子指标 {total} 个")
    return data




# ============================== API端点 ==============================
def _baseline_info():
    """v=122：只读暴露基准库版本/样本量（供部署验收 —— 判断基准库是否真的随镜像生效）。
    任何异常返回 (None, None)，绝不影响健康检查。"""
    try:
        import json as _json
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'weights.json')
        with open(_p, encoding='utf-8') as _f:
            _w = _json.load(_f)
        _am = (_w.get('行业基准库') or {}).get('【全市场均值】') or {}
        return _w.get('版本'), _am.get('样本数')
    except Exception:
        return None, None


def _evidence_info():
    """只读暴露证据校验的开关（供部署验收判断线上实际生效的版本）。
    任何异常返回全 None，绝不影响健康检查。"""
    try:
        from evidence_verify import (ENABLED as _en, STRIP_THOUSANDS as _th,
                                     MIN_TEXT_RATE as _t, MIN_NUM_RATE as _n,
                                     STRIP_CITATIONS as _sc, LABEL_NORM as _ln,
                                     RELAX_TEXT_RATE as _rt)
        return (bool(_en), ('on' if _th else 'off'), _t, _n,
                ('on' if _sc else 'off'), ('on' if _ln else 'off'), _rt)
    except Exception:
        return None, None, None, None, None, None, None


@app.route('/api/health', methods=['GET'])
def health():
    """健康检查"""
    _bv, _bs = _baseline_info()
    _ee, _eth, _emt, _emn, _esc, _eln, _ert = _evidence_info()
    return jsonify({
        "status": "ok",
        "service": "esg-extractor",
        "version": "1.0.0",
        "build": "v204b",  # v204b：MiniMax M3 无视 response_format、把 JSON 包进 ```json 围栏 ⇒ 提取侧补一层"剥围栏后重试"（agent2 侧早有，提取侧一直缺），否则一选 MiniMax 必报「LLM返回非JSON格式」｜v204：修复「切 GLM 5.2 报 LLM返回非JSON格式」——根因是 one-api 的智谱渠道用 DTO 重建请求体、把 thinking 字段丢弃，思考实际仍开着，16384 的 max_tokens 被思维链吃干（实测 tokens=63136 / content_len=3），content 只剩 "\n{\n"；改发 reasoning_effort="none"（智谱适配器会翻成 thinking.disabled，实测 reasoning 归零、prompt_tokens 与直连一致）；同时把「输出被截断」与「格式错」分开报错（原提示会把排查方向带偏到提示词）；顺带修 one-api 令牌白名单缺 minimax-m3（前端发小写 id、白名单只有上游名 MiniMax-M3 ⇒ 一选 MiniMax 就 403，已补）｜v128：权重库全量重建（B_alt）—— 底座换成 92 份 baseline_105 重提取（v119b 生产链路）披露频率；L2 冻结为已修真源方向（企业内部碳管理9/供应链碳管理5/透明度7/金融化情况3，修正 v2.1 的搬移失误），L3/L4 由频率五分位（1/3/5/7/9）重新分档；Excel AHP 矩阵重算 ω 与模型逐项一致（57/57，最大偏差 0.000000pp，各组 CR=0）；42 项全局权重全部变化（C33 监管强制披露要求符合性 6.889%→0.518%、交易所ESG披露指引符合性 6.889%→4.660%、碳排放披露范围 4.727%→6.040%）；行业基准库/全市场中位数/评分规则一字不动；weights.json 的「版本」字段同步为 v2.1（B_alt 权重库重建 2026-10-06）｜v124：①报告字体全 CJK 覆盖（6990→29833 码位，根治繁体/生僻字渲染空白；旧字体实测繁体串 108 字缺 52）②证据校验 v124b（模型自附引用剥离 + 作用域标签同义归一 + 通路B「值数字全命中则放宽文本阈值」；92 份真实数据 A/B：新增放行 29 条、值字面查无 0、意外回收 0）③OCR：三个参数改环境变量可调 + 进度补 ETA（曾试 draft+BILINEAR 提速，实测 0 收益且同一页丢整行 ⇒ **已回退**，见 _perf/v124_D/）｜v123：证据校验千分位归一（evidence_verify v118b：两侧去千分位分隔符，修复「真实数据带/不带千分位被判编造」的误杀；92 份回归中 25 条误拦恢复、真幻觉 0 漏放）｜v122b：修复 v122 的 health 路由错位（_baseline_info 被插到 @app.route 与 def health 之间 ⇒ 装饰器绑错函数）+ 基准库样本量键名（样本量→样本数）｜v122：B 阶段基准库重建（92 家重提取 + 现行口径重算；全局权重一字不动）｜v121：PDF 解析逐页释放缓存（纯内存管理，不改解析结果）——修复 52 页文档 RSS 线性涨到 2.3GB ⇒ 2G 机器 OOM/swap 抖动（单页 OCR 7.4s→65s）｜v120：并发内存闸门（纯防御：限"同时驻留的重任务数"）｜v119b：A2 混合控长｜v119 = A2 配额采样 + C1 判定词繁简/英文归一（zhconv）+ C2 提示词消歧义｜v118 = 证据强制校验（evidence_verify.py）｜v117 = EMPTY_VALUES 占位符全集｜v116 = 判据表4.4 + temp 0.0｜v114 = COS 直传
        "model": DEEPSEEK_MODEL,
        "thinking": DEEPSEEK_THINKING,
        "pdf_support": PDF_SUPPORT,
        "docx_support": DOCX_SUPPORT,
        "ocr_support": OCR_SUPPORT,
        "ocr_max_pages": OCR_MAX_PAGES,
        "ocr_target_width": OCR_TARGET_WIDTH,
        "ocr_render_dpi": OCR_RENDER_DPI,
        "ocr_num_threads": OCR_NUM_THREADS if OCR_NUM_THREADS else 'default',
        "prompt_loaded": len(EXTRACTION_PROMPT) > 0,
        "deepseek_key_set": bool(DEEPSEEK_API_KEY),
        # v=114：COS 直传就绪状态（前端据此决定走直传还是原分片链路）
        "cos_support": COS_SUPPORT,
        "cos_ready": _safe_cos_ready(),
        "cos_bucket": _safe_cos_get('bucket'),
        "cos_upload_endpoint": _safe_cos_get('upload_endpoint'),
        # v=120：并发内存闸门的可观测状态（只读；失败一律返回 None，绝不影响健康检查）
        "extract_slot_total": EXTRACT_SLOT_TOTAL,
        "extract_excl_mb": EXTRACT_EXCL_MB,
        "extract_active": _extract_active_count(),
        "mem_available_mb": _mem_available_mb(),
        # v=121：PDF 解析逐页释放缓存的开关状态（只读，用于部署核验）
        "page_cache_release": "on",
        "pdf_cache_every": _PDF_CACHE_EVERY,
        # v=122：基准库版本/样本量（只读；用于判断 B 阶段基准库是否随镜像真正生效）
        "baseline_version": _bv,
        "baseline_sample": _bs,
        # v=123：证据校验千分位归一的生效状态（只读；用于部署核验）
        "evidence_enabled": _ee,
        "evidence_strip_thousands": _eth,
        "evidence_min_text": _emt,
        "evidence_min_num": _emn,
        # v=124b：证据校验的三处新增归一/通路（只读；用于部署核验）
        "evidence_strip_citations": _esc,
        "evidence_label_norm": _eln,
        "evidence_relax_text": _ert,
    })


# ============================== 异步提取任务（v=94）==============================
# 背景：扫描件 OCR 实测约 7s/页，49 页需 ~355s；而 Cloudflare 免费版边缘默认
#       约 100s 就返回 524（已实测在 127.9s 触发）。故引入「提交即返回 + 轮询」：
#       每个 HTTP 请求都远短于边缘超时，长耗时全部发生在后台线程里。
# 说明：内存态任务表，随进程重启清空（单容器单进程，够用且零依赖）。
EXTRACT_TASKS = {}
EXTRACT_TASKS_LOCK = threading.Lock()
EXTRACT_TASK_TTL = 7200      # 已完成任务保留 2 小时，供前端取回结果
EXTRACT_TASK_MAX = 50        # 任务表上限，超出时优先清理最旧的已完成任务


def _task_update(task_id, **fields):
    """线程安全地更新任务字段（并刷新时间戳）"""
    with EXTRACT_TASKS_LOCK:
        t = EXTRACT_TASKS.get(task_id)
        if not t:
            return
        t.update(fields)
        t['updated_at'] = time.time()


def _task_gc():
    """清理过期 / 超量任务，避免内存无上限增长"""
    now = time.time()
    with EXTRACT_TASKS_LOCK:
        for tid in [k for k, v in EXTRACT_TASKS.items()
                    if v.get('status') in ('done', 'error')
                    and now - v.get('updated_at', now) > EXTRACT_TASK_TTL]:
            EXTRACT_TASKS.pop(tid, None)
        if len(EXTRACT_TASKS) > EXTRACT_TASK_MAX:
            finished = sorted(
                [kv for kv in EXTRACT_TASKS.items()
                 if kv[1].get('status') in ('done', 'error')],
                key=lambda kv: kv[1].get('updated_at', 0)
            )
            for tid, _ in finished[:len(EXTRACT_TASKS) - EXTRACT_TASK_MAX]:
                EXTRACT_TASKS.pop(tid, None)


# ============================== v=120 并发内存闸门（纯防御）==============================
# 背景（2026-09-27 实测 OOM）：服务器仅 ~2GB 内存。批处理以 3 并发同时提交
#   兰剑智能 98.4MB（扫描件、OCR 60 页）+ 德石股份 50.8MB + 振德医疗 33.3MB
#   → Flask `file.read()` 全量入内存 + pdfplumber + RapidOCR 逐页大图
#   → `dmesg`: `Out of memory: Killed process (python) anon-rss:1.6GB`
#   → esg-extractor 容器被 OOM killer 重启 → **内存态 EXTRACT_TASKS 被清空**
#   → 所有在飞任务永久 404（调用方若把 404 当"未完成"，就会无限重试 → 再 OOM）。
#
# 本闸门只限制「**同时驻留的重任务数**」，**不改变任何提取 / 评分 / 文本处理逻辑**：
#   · 槽位总数 EXTRACT_SLOT_TOTAL（默认 2）；
#   · 单份 ≥ EXTRACT_EXCL_MB（默认 30MB）的 PDF 独占「全部」槽位
#     ⇒ 构造上不可能出现「多个 30MB+ 的 PDF 同时解析」。
#   实测单份 98.4MB 扫描件峰值 RSS ≈ 350MB，叠常态 ~590MB 远低于 1935MB。
# 获取带超时（EXTRACT_SLOT_WAIT），避免某个卡死任务把后来者永久阻塞；超时按普通失败
# 处理（任务标 error 并写明原因），且**不会漏放槽位**。
# 关停开关：EXTRACT_SLOT_TOTAL=0 或 1 ⇒ 等价于不限并发（行为完全回到 v119b）。
EXTRACT_SLOT_TOTAL = int(os.environ.get('EXTRACT_SLOT_TOTAL', '2') or 2)
EXTRACT_EXCL_MB = int(os.environ.get('EXTRACT_EXCL_MB', '30') or 30)
EXTRACT_SLOT_WAIT = int(os.environ.get('EXTRACT_SLOT_WAIT', '1800') or 1800)
_EXTRACT_SLOTS = threading.Semaphore(EXTRACT_SLOT_TOTAL)
_EXTRACT_EXCL = threading.Lock()
_EXTRACT_ACTIVE = 0
_EXTRACT_ACTIVE_LOCK = threading.Lock()


def _extract_slots_needed(nbytes):
    """≥EXTRACT_EXCL_MB 的文件独占全部槽位；其余占 1 个。返回 0 = 不加闸（已关停）"""
    if EXTRACT_SLOT_TOTAL <= 1:
        return 0
    if nbytes >= EXTRACT_EXCL_MB * 1024 * 1024:
        return EXTRACT_SLOT_TOTAL
    return 1


def _extract_gate_enter(n):
    """获取 n 个槽位；返回实际获取数。中途失败时内部已回滚，绝不泄漏槽位。"""
    if n <= 0:
        return 0
    if n >= EXTRACT_SLOT_TOTAL:
        if not _EXTRACT_EXCL.acquire(timeout=EXTRACT_SLOT_WAIT):
            raise RuntimeError(f'等待大文件独占位超时（{EXTRACT_SLOT_WAIT}s）')
    got = 0
    for _ in range(n):
        if _EXTRACT_SLOTS.acquire(timeout=EXTRACT_SLOT_WAIT):
            got += 1
        else:
            for _ in range(got):
                _EXTRACT_SLOTS.release()
            if n >= EXTRACT_SLOT_TOTAL:
                _EXTRACT_EXCL.release()
            raise RuntimeError(f'等待提取槽位超时（{EXTRACT_SLOT_WAIT}s）')
    return got


def _extract_gate_exit(got):
    """释放 got 个槽位（got 必须等于 _extract_gate_enter 的返回值）"""
    if got <= 0:
        return
    for _ in range(got):
        _EXTRACT_SLOTS.release()
    if got >= EXTRACT_SLOT_TOTAL:
        _EXTRACT_EXCL.release()


def _extract_active_inc():
    global _EXTRACT_ACTIVE
    with _EXTRACT_ACTIVE_LOCK:
        _EXTRACT_ACTIVE += 1


def _extract_active_dec():
    global _EXTRACT_ACTIVE
    with _EXTRACT_ACTIVE_LOCK:
        _EXTRACT_ACTIVE = max(0, _EXTRACT_ACTIVE - 1)


def _extract_active_count():
    with _EXTRACT_ACTIVE_LOCK:
        return _EXTRACT_ACTIVE


def _mem_available_mb():
    """读 /proc/meminfo 的 MemAvailable（容器内也可读）。任何异常一律返回 None —— 
    健康检查是运维生命线，绝不允许被这个"锦上添花"的指标搞挂。"""
    try:
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return round(int(line.split()[1]) / 1024.0, 1)
        return None
    except Exception:
        return None


def _run_extraction_task(task_id, filename, file_bytes, company_type, model_id):
    """后台线程主体：跑完整提取流程，把结果写回任务表

    v=120：进出 run_extraction 前后走并发内存闸门（详见上方说明）。
    """
    def _progress(stage=None, current=None, total=None):
        _task_update(task_id, stage=stage or 'working',
                     current=current, total=total)

    nslots = _extract_slots_needed(len(file_bytes))
    _task_update(task_id, status='running', stage='waiting')
    got = 0
    try:
        if nslots:
            logger.info(
                f"⏳ 等待提取槽位: {task_id} | {filename} "
                f"({len(file_bytes)/1048576:.1f}MB，需 {nslots}/{EXTRACT_SLOT_TOTAL} 槽)"
            )
        got = _extract_gate_enter(nslots)
        if got:
            _extract_active_inc()
            logger.info(f"▶ 开始提取: {task_id} | 占 {got} 槽 | 当前在跑 {_extract_active_count()}")
        _task_update(task_id, stage='parse')
        payload, status_code = run_extraction(
            filename, file_bytes, company_type=company_type,
            model_id=model_id, progress_cb=_progress
        )
        if status_code == 200:
            _task_update(task_id, status='done', stage='done', result=payload)
            logger.info(f"🧵 异步提取任务完成: {task_id}")
        else:
            _task_update(
                task_id, status='error', stage='failed',
                error=(payload or {}).get('error', '提取失败'), detail=payload
            )
            logger.warning(f"🧵 异步提取任务失败: {task_id} | {payload}")
    except Exception as e:
        logger.error(f"异步提取任务异常: {e}")
        logger.error(traceback.format_exc())
        _task_update(task_id, status='error', stage='failed',
                     error=f"服务内部异常: {str(e)}")
    finally:
        if got:
            _extract_active_dec()
            _extract_gate_exit(got)
        _task_gc()


def _parse_extract_request():
    """校验并解析提取请求（同步 /api/extract、/api/extract_async、/api/extract_score 共用）

    成功 → dict：{filename, file_bytes, company_type, model_id}
    失败 → (Flask响应, 状态码) 元组，调用方用 isinstance(x, tuple) 判别
    """
    if 'file' not in request.files:
        return jsonify({"error": "请上传文件（file字段）"}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({"error": "文件名为空"}), 400

    # 公司类型（评分分流：listed=上市公司 / unlisted=非上市公司）
    company_type = request.form.get('company_type', 'listed')
    if company_type not in ('listed', 'unlisted'):
        company_type = 'listed'

    filename = file.filename
    file_bytes = file.read()
    logger.info(f"📄 接收文件: {filename} ({len(file_bytes)/1024:.1f}KB)")
    model_id = request.form.get('model', 'deepseek-v4-flash')
    return {
        'filename': filename,
        'file_bytes': file_bytes,
        'company_type': company_type,
        'model_id': model_id,
    }


@app.route('/api/extract', methods=['POST'])
def extract():
    """同步提取（本机直连、脚本调用、短文档用）

    ⚠️ 经 Cloudflare 隧道访问时，耗时超过边缘超时（实测 ~128s）会被 524 掐断，
       因此公网前端统一改用 /api/extract_async；本接口保留用于本机与脚本。
    """
    parsed = _parse_extract_request()
    if isinstance(parsed, tuple):
        return parsed
    payload, status_code = run_extraction(
        parsed['filename'], parsed['file_bytes'],
        company_type=parsed['company_type'], model_id=parsed['model_id']
    )
    return jsonify(payload), status_code


@app.route('/api/extract_text', methods=['POST'])
def extract_text_only():
    """仅抽文、不调用 LLM → 返回文字层文本与编码噪声率。

    v=97 新增，诊断/评估专用（**零 LLM 成本**）：
      ① 让评估器拿到与后端「完全同源」的基准文本 —— 此前评估用 pymupdf 做基准
         与后端 pdfplumber 差 6000+ 字符、数字准确率被低估 30pp，教训必须守住；
      ② 可直接观测 OCR 兜底是否生效、噪声率从多少降到多少。
    form 字段与 /api/extract 完全一致（file / company_type / model）。
    """
    parsed = _parse_extract_request()
    if isinstance(parsed, tuple):
        return parsed
    t0 = time.time()
    try:
        text = extract_text_from_pdf(parsed['file_bytes'])
    except Exception as e:
        logger.error(f"抽文失败: {e}")
        logger.error(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({
        'success': True,
        'filename': parsed['filename'],
        'text_chars': len(text),
        'noise_ratio': round(_encoding_noise_ratio(text), 4),
        'duration_sec': round(time.time() - t0, 1),
        'text': text,
    })


@app.route('/api/extract_async', methods=['POST'])
def extract_async():
    """异步提交提取任务 → 立即返回 task_id（绕开边缘 100s 超时）

    返回 202 + {"task_id": ...}；进度与结果通过 GET /api/extract_status/<task_id> 轮询。
    """
    parsed = _parse_extract_request()
    if isinstance(parsed, tuple):
        return parsed

    filename = parsed['filename']
    file_bytes = parsed['file_bytes']
    task_id = uuid.uuid4().hex
    with EXTRACT_TASKS_LOCK:
        EXTRACT_TASKS[task_id] = {
            'task_id': task_id,
            'status': 'queued',
            'stage': 'queued',
            'filename': filename,
            'file_size_kb': round(len(file_bytes) / 1024, 1),
            'current': 0,
            'total': 0,
            'created_at': time.time(),
            'updated_at': time.time(),
        }
    threading.Thread(
        target=_run_extraction_task,
        args=(task_id, filename, file_bytes,
              parsed['company_type'], parsed['model_id']),
        daemon=True
    ).start()
    logger.info(
        f"🧵 异步提取任务已创建: {task_id} | {filename} ({len(file_bytes)/1024:.1f}KB)"
    )
    return jsonify({'task_id': task_id, 'status': 'queued'}), 202


@app.route('/api/extract_status/<task_id>', methods=['GET'])
def extract_status(task_id):
    """轮询任务进度 / 取回结果（响应体很小，单次请求远短于边缘超时）"""
    with EXTRACT_TASKS_LOCK:
        t = EXTRACT_TASKS.get(task_id)
        snapshot = dict(t) if t else None
    if snapshot is None:
        return jsonify({'error': '任务不存在或已过期'}), 404
    return jsonify(snapshot)


# ============================== 分片上传（v=96）==============================
# 背景：单次 POST 提交大文件的耗时 = 体积 / 上行带宽。
#   实测（2026-09-12，公网 60MB 分片实测）：真实上行仅 139 KB/s，且抖动极大
#   （单片 4MB 最快 ~15s、最慢 84.9s，相差 5.7 倍）。
#   ⚠️ 此前用「98.4MB 在 126.9s 断连」反推出的 449KB/s 是错的 —— 126.9s 是被
#      Cloudflare 掐断的时刻，而非上传完成的时刻，速度被高估约 3 倍。
#   → 真实 100s 边缘超时边界约 14MB。故 >6MB 的文件改走「分片上传」：
#         ① 前端按 1MB 切片，逐片 POST /api/upload_chunk
#            （单片 139KB/s 下约 7.5s，最坏抖动按 3 倍算约 22s，安全余量 ~4.5 倍；
#              单请求体也远小于 Cloudflare 免费版 100MB 上限）
#         ② 全部到齐后 POST /api/upload_complete → 服务端顺序合并成完整文件
#         ③ 服务端建异步任务并返回 task_id，前端复用既有 /api/extract_status 轮询
# 说明：本段为纯新增接口，未改动 /api/extract 与 /api/extract_async 的既有行为。
import tempfile
import shutil
import re as _re

UPLOAD_TMP_ROOT = Path(tempfile.gettempdir()) / 'esg_upload_chunks'
UPLOAD_CHUNK_MAX_BYTES = 8 * 1024 * 1024    # 单片上限 8MB（前端只发 1MB，此处为防御性上限）
UPLOAD_MAX_CHUNKS = 400                     # 最多 400 片 × 1MB ≈ 400MB 覆盖上限
UPLOAD_SESSION_TTL = 3600                   # 未完成会话保留 1 小时，之后自动回收
_UPLOAD_ID_RE = _re.compile(r'^[0-9a-f]{32}$')   # 只认 32 位十六进制，杜绝路径穿越
# v=104：分片文件名的唯一格式定义（落盘/校验/续传查询三处共用，避免各写一份正则走偏）
_CHUNK_FILE_RE = _re.compile(r'^chunk_(\d{5})$')


def _upload_dir(upload_id):
    """把 upload_id 映射到临时目录（ID 已通过白名单正则校验）"""
    return UPLOAD_TMP_ROOT / upload_id


def _chunk_path(upload_dir, chunk_index):
    """分片在会话目录内的落盘路径（索引已校验为 int，格式化天然无路径穿越）"""
    return upload_dir / f'chunk_{chunk_index:05d}'


def _existing_chunks(upload_dir):
    """列出会话目录内已落盘的分片索引（升序）。目录不存在时返回空表。

    v=104：⚠️ 必须显式过滤 `is_file()`。`_upload_gc()` 是并发跑的（每个分片请求
    可能触发一次回收），若回收发生在遍历过程中，目录里的项可能在两次调用之间消失，
    裸 iterdir() 会把已删除的名字也报成"已上传"，前端据此跳片 → 合并时缺片失败。
    """
    out = []
    try:
        if not upload_dir.is_dir():
            return out
        for p in upload_dir.iterdir():
            m = _CHUNK_FILE_RE.match(p.name)
            if m and p.is_file():
                out.append(int(m.group(1)))
    except OSError:
        pass
    out.sort()
    return out


def _upload_gc():
    """回收超时未完成的分片目录，避免磁盘无上限增长（best-effort）"""
    try:
        if not UPLOAD_TMP_ROOT.exists():
            return
        now = time.time()
        for d in UPLOAD_TMP_ROOT.iterdir():
            if d.is_dir() and now - d.stat().st_mtime > UPLOAD_SESSION_TTL:
                shutil.rmtree(d, ignore_errors=True)
                logger.info(f"🧹 回收过期上传会话: {d.name}")
    except Exception as e:
        logger.warning(f"上传会话回收失败（忽略）: {e}")


_upload_req_counter = 0


def _upload_gc_tick():
    """每 50 次分片请求触发一次会话回收

    必须在 upload_chunk 里也触发：用户中途关掉浏览器时不会有人调用
    upload_complete，若只在那里回收，残留分片将永久留在磁盘上。
    """
    global _upload_req_counter
    _upload_req_counter += 1
    if _upload_req_counter % 50 == 0:
        _upload_gc()


def _create_extract_task(filename, file_bytes, company_type='listed',
                         model_id='deepseek-v4-flash'):
    """建任务表项 + 起后台线程（v=96 新增，供分片上传路径复用）

    与 /api/extract_async 内的建任务逻辑保持一致；extract_async 原实现未改动，
    本函数只是把同一套逻辑抽出来给 upload_complete 使用。
    """
    task_id = uuid.uuid4().hex
    with EXTRACT_TASKS_LOCK:
        EXTRACT_TASKS[task_id] = {
            'task_id': task_id,
            'status': 'queued',
            'stage': 'queued',
            'filename': filename,
            'file_size_kb': round(len(file_bytes) / 1024, 1),
            'current': 0,
            'total': 0,
            'created_at': time.time(),
            'updated_at': time.time(),
        }
    threading.Thread(
        target=_run_extraction_task,
        args=(task_id, filename, file_bytes, company_type, model_id),
        daemon=True
    ).start()
    logger.info(
        f"🧵 异步提取任务已创建: {task_id} | {filename} ({len(file_bytes)/1024:.1f}KB)"
    )
    return {'task_id': task_id, 'status': 'queued'}


@app.route('/api/upload_chunk', methods=['POST'])
def upload_chunk():
    """接收单个分片 → 落盘（每个请求都很小很快，不会被边缘超时掐断）

    form 字段：upload_id / chunk_index / file(该片二进制)
    """
    upload_id = (request.form.get('upload_id') or '').strip().lower()
    if not _UPLOAD_ID_RE.match(upload_id):
        return jsonify({'error': 'upload_id 不合法'}), 400

    try:
        chunk_index = int(request.form.get('chunk_index', -1))
    except (TypeError, ValueError):
        return jsonify({'error': 'chunk_index 必须是整数'}), 400
    if chunk_index < 0 or chunk_index >= UPLOAD_MAX_CHUNKS:
        return jsonify({'error': f'chunk_index 超出范围（0-{UPLOAD_MAX_CHUNKS - 1}）'}), 400

    if 'file' not in request.files:
        return jsonify({'error': '缺少分片数据（file字段）'}), 400
    blob = request.files['file'].read()
    if len(blob) > UPLOAD_CHUNK_MAX_BYTES:
        return jsonify({'error': f'单个分片超过 {UPLOAD_CHUNK_MAX_BYTES // 1048576}MB 上限'}), 413

    try:
        d = _upload_dir(upload_id)
        d.mkdir(parents=True, exist_ok=True)
        _chunk_path(d, chunk_index).write_bytes(blob)
    except Exception as e:
        logger.error(f"分片落盘失败: {e}")
        return jsonify({'error': f'分片落盘失败: {e}'}), 500

    _upload_gc_tick()
    return jsonify({'ok': True, 'upload_id': upload_id,
                    'chunk_index': chunk_index, 'size': len(blob)})


@app.route('/api/upload_status', methods=['GET'])
def upload_status():
    """查询某次分片上传已落盘的分片 → 供前端「断点续传」（v=104）

    为什么需要它（2026-09-16 真实故障）：
      Cloudflare 快速隧道到边缘的 TCP 连接会周期性被掐断（实测 9 小时内 2 次，
      重连耗时约 21s）。连接断开的瞬间，正在上传的分片会被边缘直接判为 530，
      分片只传了一半 → nginx 记 `400 0`。原先前端重试 3 次（退避合计仅 3s）
      远小于重连窗口，必然失败；失败后还会调 /api/upload_abort 把**整批分片删掉**，
      用户只能从第 1 片重来 —— 100MB 报告 = 100 片，实际上永远传不完。

      有了本接口，重试时可以带着同一个 upload_id 先问"哪些片已经在服务端了"，
      只补缺失的片，把「一次断线 = 整个文件重传」降为「一次断线 = 重传 1 片」。

    路由：GET /api/upload_status?upload_id=<32位hex>
    返回：{"ok":true,"upload_id":..,"chunks":[0,1,2,..],"count":N}
    """
    upload_id = (request.args.get('upload_id') or '').strip().lower()
    if not _UPLOAD_ID_RE.match(upload_id):
        return jsonify({'error': 'upload_id 不合法'}), 400
    chunks = _existing_chunks(_upload_dir(upload_id))
    return jsonify({'ok': True, 'upload_id': upload_id,
                    'chunks': chunks, 'count': len(chunks)})


@app.route('/api/upload_complete', methods=['POST'])
def upload_complete():
    """分片全部到齐 → 顺序合并 → 建异步任务 → 返回 task_id

    JSON 字段：upload_id / total_chunks / filename / company_type / model
    """
    data = request.get_json(silent=True) or {}
    upload_id = str(data.get('upload_id') or '').strip().lower()
    if not _UPLOAD_ID_RE.match(upload_id):
        return jsonify({'error': 'upload_id 不合法'}), 400

    try:
        total_chunks = int(data.get('total_chunks', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'total_chunks 必须是整数'}), 400
    if total_chunks <= 0 or total_chunks > UPLOAD_MAX_CHUNKS:
        return jsonify({'error': f'total_chunks 超出范围（1-{UPLOAD_MAX_CHUNKS}）'}), 400

    d = _upload_dir(upload_id)
    if not d.is_dir():
        return jsonify({'error': '上传会话不存在或已过期，请重新上传'}), 404

    # 文件名只取 basename，杜绝 ../ 之类的路径穿越
    raw_name = str(data.get('filename') or 'upload.pdf')
    filename = os.path.basename(raw_name.replace('\\', '/')).strip() or 'upload.pdf'

    company_type = data.get('company_type', 'listed')
    if company_type not in ('listed', 'unlisted'):
        company_type = 'listed'
    model_id = data.get('model') or 'deepseek-v4-flash'

    # 完整性校验：缺片时明确告知缺哪一片，便于定位重传
    # v=104：返回体带上缺失片索引，前端可据此「只补缺片」而不是整包重传
    missing = [i for i in range(total_chunks) if not _chunk_path(d, i).exists()]
    if missing:
        return jsonify({
            'error': f'分片不完整，缺失 {len(missing)} 片（如第 {missing[0] + 1} 片），请重试上传',
            'missing_chunks': missing[:50],
            'missing_count': len(missing)
        }), 400

    try:
        buf = bytearray()
        for i in range(total_chunks):
            buf += _chunk_path(d, i).read_bytes()
        file_bytes = bytes(buf)
    except Exception as e:
        logger.error(f"合并分片失败: {e}")
        return jsonify({'error': f'合并分片失败: {e}'}), 500
    finally:
        # 无论成败都清理临时分片，避免磁盘残留
        shutil.rmtree(d, ignore_errors=True)

    if not file_bytes:
        return jsonify({'error': '合并后的文件为空'}), 400

    logger.info(
        f"🧩 分片合并完成: {filename} | {total_chunks} 片 / {len(file_bytes) / 1048576:.1f}MB"
    )

    _upload_gc()
    return jsonify(_create_extract_task(
        filename, file_bytes, company_type, model_id
    )), 202


@app.route('/api/upload_abort', methods=['POST'])
def upload_abort():
    """取消上传 → 清理已落盘的分片（best-effort，前端失败/取消时调用）"""
    data = request.get_json(silent=True) or {}
    upload_id = str(data.get('upload_id') or '').strip().lower()
    if not _UPLOAD_ID_RE.match(upload_id):
        return jsonify({'error': 'upload_id 不合法'}), 400
    d = _upload_dir(upload_id)
    existed = d.is_dir()
    shutil.rmtree(d, ignore_errors=True)
    return jsonify({'ok': True, 'cleaned': existed})


# ============================== COS 直传（v=114）==============================
# 背景：存在 99.8MB 量级报告。原链路（浏览器 → CF 边缘 → 香港源站）实测 **3.0 Mbps**，
#       100MB 需 **4.4 分钟**，不可接受。
# 方案：浏览器 → COS（国内就近接入，实测 18–21 Mbps ≈ 43 秒）→
#       服务器从 COS 取件（腾讯云内网/全球加速，实测 **205–346 Mbps**）
#       ⇒ 全链路约 40 秒（对比原路径快约 6 倍）。
# ⚠️ 本段为**纯新增**：/api/upload_chunk|upload_status|upload_complete|upload_abort
#    四个旧路由**一字不改**，作为自动回落链路（COS 任一环不可用，前端自动退回）。
# 配置全部可经 .env 覆盖 ⇒ 换桶/换地域不用重打镜像。
_COS_IMPORT_ERROR = ''      # 防御性初始化：无论导入成败都保证该名字存在
try:
    from qcloud_cos import CosConfig, CosS3Client
    COS_SUPPORT = True
except ImportError as _cos_err:
    COS_SUPPORT = False
    _COS_IMPORT_ERROR = str(_cos_err)


def _cos_cfg():
    """读取 COS 配置（每次调用都读环境变量 ⇒ 改 .env 重启即生效）"""
    return {
        'enabled': os.getenv('COS_ENABLED', '0').strip().lower() in ('1', 'true', 'yes', 'on'),
        'secret_id': os.getenv('COS_SECRET_ID', '').strip(),
        'secret_key': os.getenv('COS_SECRET_KEY', '').strip(),
        'bucket': os.getenv('COS_BUCKET', '').strip(),
        'region': os.getenv('COS_REGION', 'ap-guangzhou').strip(),
        # 前端直传用（预签名 URL 的 host 必须与此一致，否则 403）
        'upload_endpoint': os.getenv('COS_UPLOAD_ENDPOINT', '').strip(),
        # 服务器取件用，按顺序回退（内网加速 → 公网加速 → 直连）
        'fetch_endpoints': [
            e.strip() for e in os.getenv('COS_FETCH_ENDPOINTS', '').split(',') if e.strip()
        ],
        'key_prefix': os.getenv('COS_KEY_PREFIX', 'uploads/').strip() or 'uploads/',
        'max_upload_mb': int(os.getenv('COS_MAX_UPLOAD_MB', '500') or 500),
        'presign_ttl': int(os.getenv('COS_PRESIGN_TTL', '3600') or 3600),
        'delete_after_fetch': os.getenv('COS_DELETE_AFTER_FETCH', '1').strip().lower() in ('1', 'true', 'yes', 'on'),
    }


def _cos_ready(cfg):
    """COS 是否可用于直传（缺任一必需项即视为不可用 ⇒ 前端自动回落旧链路）

    返回 (ready: bool, why: str)
    """
    if not COS_SUPPORT:
        return False, f'服务端未安装 cos SDK（{_COS_IMPORT_ERROR}）'
    if not cfg['enabled']:
        return False, 'COS_ENABLED 未开启'
    for k in ('secret_id', 'secret_key', 'bucket', 'upload_endpoint'):
        if not cfg[k]:
            return False, f'缺少配置 {k.upper()}'
    if not cfg['fetch_endpoints']:
        return False, '缺少配置 COS_FETCH_ENDPOINTS'
    return True, ''


def _cos_client(cfg, endpoint):
    """按指定域名建 client。

    ⚠️ Endpoint 必须传**不含桶名前缀**的域名（SDK 会自己拼 `<bucket>.`）；
    传完整桶域名会拼成 `bucket.bucket.…` 而连接失败。
    """
    conf = CosConfig(
        Region=cfg['region'],
        SecretId=cfg['secret_id'],
        SecretKey=cfg['secret_key'],
        Endpoint=endpoint,
        Scheme='https',
    )
    return CosS3Client(conf)


def _safe_cos_ready():
    """供 /api/health 使用的只读探针 —— 配置写错也不能让健康检查 500"""
    try:
        return _cos_ready(_cos_cfg())[0]
    except Exception:
        return False


def _safe_cos_get(field):
    """供 /api/health 读取单项配置（异常一律返回空串）"""
    try:
        return str(_cos_cfg().get(field, '') or '')
    except Exception:
        return ''


def _cos_fetch_bytes(cfg, key):
    """从 COS 取对象（三级域名回退：内网加速 → 公网加速 → 直连）

    为什么必须回退：**内网加速域名只在腾讯云内网有效**（服务器上解析到 169.254.x，
    本机开发环境根本解析不通）⇒ 本地跑必须能落到公网加速或直连域名。
    返回 (bytes, endpoint_used)；全部失败时抛最后一个异常。
    """
    last_err = None
    for ep in cfg['fetch_endpoints']:
        try:
            client = _cos_client(cfg, ep)
            resp = client.get_object(Bucket=cfg['bucket'], Key=key)
            buf = bytearray()
            for chunk in resp['Body'].get_raw_stream().stream(1024 * 1024):
                buf += chunk
            data = bytes(buf)
            if not data:
                raise RuntimeError('COS 返回空对象')
            logger.info(f"📥 COS 取件成功: {key} | {len(data)/1048576:.1f}MB | {ep}")
            return data, ep
        except Exception as e:
            last_err = e
            logger.warning(f"COS 取件失败（{ep}）: {type(e).__name__}: {e}")
    raise last_err if last_err is not None else RuntimeError('无可用 COS 取件域名')


@app.route('/api/cos_upload_prepare', methods=['POST'])
def cos_upload_prepare():
    """签发「直传 COS 的预签名 PUT URL」（v=114 新增）

    设计取舍：
      ① 用「单对象预签名 PUT」而非 STS 临时密钥 —— 前者只能写后端指定的**一个 key**、
         1 小时过期，爆炸半径远小于 STS（STS 能写整个桶前缀）；且**不依赖
         GetFederationToken 权限**，主账号/子账号密钥都能直接用。
      ② **不签 Content-Type** —— COS 只校验签名里列出的 header，不签它就能兼容浏览器
         自动带上的 `file.type`，避免"前端少设一个 header 就 403"的脆性。
      ③ 预签名 URL 的 host 由服务端固定，**前端不得改写** —— COS v5 签名**含 host**，
         换 host 必然 403（网络通、0.1s 快失败，极易误判成"网络不通"）。

    JSON：{filename, size, company_type, model}
    返回：{ok:true, key, url, upload_id, expires_in, max_bytes}
          不可用时 {ok:false, reason} —— 前端据此**自动回落**到原分片链路（HTTP 200）
    """
    cfg = _cos_cfg()
    ready, why = _cos_ready(cfg)
    if not ready:
        logger.info(f"COS 直传不可用，前端将走原分片链路: {why}")
        return jsonify({'ok': False, 'reason': why}), 200

    data = request.get_json(silent=True) or {}
    raw_name = str(data.get('filename') or 'upload.pdf')
    filename = os.path.basename(raw_name.replace('\\', '/')).strip() or 'upload.pdf'
    try:
        size = int(data.get('size', 0))
    except (TypeError, ValueError):
        size = 0

    max_bytes = cfg['max_upload_mb'] * 1024 * 1024
    if size <= 0 or size > max_bytes:
        return jsonify({'ok': False,
                        'reason': '文件大小超出 COS 直传上限（%dMB）' % cfg['max_upload_mb']}), 200

    ext = os.path.splitext(filename)[1].lower()
    if ext not in ('.pdf', '.docx', '.doc', '.ppt', '.pptx', '.xlsx', '.xls'):
        ext = '.bin'
    upload_id = uuid.uuid4().hex
    key = '%s%s/%s%s' % (cfg['key_prefix'], time.strftime('%Y%m%d'), upload_id, ext)

    try:
        client = _cos_client(cfg, cfg['upload_endpoint'])
        url = client.get_presigned_url(
            Method='PUT', Bucket=cfg['bucket'], Key=key, Expired=cfg['presign_ttl']
        )
    except Exception as e:
        logger.error(f"预签名生成失败: {type(e).__name__}: {e}")
        return jsonify({'ok': False, 'reason': f'预签名生成失败: {e}'}), 200

    logger.info(f"🔗 COS 直传已就绪: {key} ({size / 1048576:.1f}MB) via {cfg['upload_endpoint']}")
    return jsonify({
        'ok': True, 'key': key, 'url': url, 'upload_id': upload_id,
        'expires_in': cfg['presign_ttl'], 'max_bytes': max_bytes,
    })


@app.route('/api/cos_complete', methods=['POST'])
def cos_complete():
    """直传完成后：服务器从 COS 取件 → 建提取任务（v=114 新增）

    ⚠️ key 必须落在 COS_KEY_PREFIX 前缀内，否则拒 —— 防止把桶里别的对象拉下来。

    JSON：{key, filename, size, company_type, model}
    返回：202 {task_id, status:'queued', fetch_seconds, size_mb}
    """
    cfg = _cos_cfg()
    ready, why = _cos_ready(cfg)
    if not ready:
        return jsonify({'error': f'COS 不可用：{why}'}), 503

    data = request.get_json(silent=True) or {}
    key = str(data.get('key') or '').strip()
    if not key or not key.startswith(cfg['key_prefix']) or '..' in key:
        return jsonify({'error': 'key 不合法'}), 400

    raw_name = str(data.get('filename') or 'upload.pdf')
    filename = os.path.basename(raw_name.replace('\\', '/')).strip() or 'upload.pdf'
    company_type = data.get('company_type', 'listed')
    if company_type not in ('listed', 'unlisted'):
        company_type = 'listed'
    model_id = data.get('model') or 'deepseek-v4-flash'

    t0 = time.time()
    try:
        file_bytes, ep = _cos_fetch_bytes(cfg, key)
    except Exception as e:
        logger.error(f"COS 取件全部失败: {key} | {type(e).__name__}: {e}")
        return jsonify({'error': f'从 COS 取件失败: {e}'}), 502
    cost = time.time() - t0

    if not file_bytes:
        return jsonify({'error': 'COS 对象为空'}), 400

    logger.info(
        f"📥 COS 取件完成: {filename} | {len(file_bytes) / 1048576:.1f}MB | {cost:.1f}s | {ep}"
    )

    if cfg['delete_after_fetch']:
        try:
            _cos_client(cfg, cfg['fetch_endpoints'][0]).delete_object(
                Bucket=cfg['bucket'], Key=key
            )
        except Exception as e:
            logger.warning(f"COS 对象清理失败（忽略，不影响主流程）: {e}")

    payload = _create_extract_task(filename, file_bytes, company_type, model_id)
    payload['fetch_seconds'] = round(cost, 2)
    payload['size_mb'] = round(len(file_bytes) / 1048576, 1)
    return jsonify(payload), 202


# ============================== v119（A2）：超长文本「分页配额采样」 ==============================
# 背景：原实现 report_text[:60000] 是**从头截断**。
#   ① 字符数 ≠ 信息量：中文报告中位 4.3 万字符（28% 超限），
#      英文报告中位 24.2 万字符（88% 超限，平均只覆盖前 24%）；
#   ② 报告的数据密集区（附录 / 关键绩效表）普遍在后半段，被整段丢弃。
#   实测：同一份宏微报告，中文版抽取 62,234 字符 → 4.41 分，
#        英文版抽取 238,230 字符 → 1.59 分（内部 7.61 ↔ 2.75），
#        而 `ISO 14001`@偏移 92,839、`Scope 1`@80,393 均在 60,000 线外。
# 方案：把全文均分为 K 段，每段按配额保留「段首 + 段尾」，段间以省略标记衔接。
#   长报告不再"只读前 1/4"，且首尾与全篇分布都进模型。
# 开关：TEXT_SAMPLE_MODE=quota（默认）| legacy（回退原从头截断，用于 A/B 与紧急回滚）
TEXT_SAMPLE_MODE = (os.environ.get('TEXT_SAMPLE_MODE') or 'quota').strip().lower()
try:
    TEXT_SAMPLE_SEGMENTS = max(2, int(os.environ.get('TEXT_SAMPLE_SEGMENTS') or 10))
except (TypeError, ValueError):
    TEXT_SAMPLE_SEGMENTS = 10
# 头部连续保留比例（默认 0.5）。设计依据：让「头部连续区」**恰好等于旧版全部输入**
#   （120000 × 0.5 = 60000 = 原 MAX_TEXT_CHARS），从而保证
#   **前段证据一项不丢（严格不劣于 v118）**；其余预算全部用于全篇抽样（新增覆盖）。
try:
    TEXT_SAMPLE_HEAD_RATIO = float(os.environ.get('TEXT_SAMPLE_HEAD_RATIO') or 0.5)
except (TypeError, ValueError):
    TEXT_SAMPLE_HEAD_RATIO = 0.5
TEXT_SAMPLE_HEAD_RATIO = min(0.9, max(0.0, TEXT_SAMPLE_HEAD_RATIO))


def sample_text_by_quota(text: str, budget: int = 60000,
                         segments: int = TEXT_SAMPLE_SEGMENTS,
                         head_ratio: float = TEXT_SAMPLE_HEAD_RATIO) -> str:
    """超长文本 → 「头部连续保留 + 全篇配额采样」混合控长。

    实测依据（宏微英文版 24.1 万字符 / 87 页，同一份报告中英两版实测）：
      · 前段核心指标证据（GHG 总量 @19,431、能源强度 @19,683、环保投资 @19,558，
        均在第 10 页 ≈ 前 8%）—— **必须靠头部连续保留**才能拿到；
        若全用配额采样，这些会被"段内省略"切掉（实测总分因此少 0.7）。
      · 后段附录 / 关键绩效表证据（ISO 14001@93,640、包装@120,296、
        供应商准入@145,818）—— **必须靠全篇采样**才能拿到；
        原「从头截断」只看前 6 万字符（=前 22 页 / 87 页），这些全丢。
      ⇒ 两种策略各有不可替代的收益，故**组合**：
         头部 `head_ratio` 份额连续保留，其余份额对后半部做 `segments` 段配额采样。

    - `len(text) <= budget` 时**原样返回**（短报告零行为变化）；
    - 省略处插入 `……[本段省略 N 字符]……` 标记，让模型知道此处有断口，
      避免把断口两侧的文字误读成连续句。
    """
    if not text:
        return text
    if len(text) <= budget:
        return text
    head_len = int(budget * head_ratio)
    rest_budget = budget - head_len
    out = []
    if head_len > 0:
        out.append(text[:head_len])
        out.append('\n\n……[以下为该报告全篇抽样，段内省略处以「本段省略」标记]……\n\n')
    body = text[head_len:]
    if rest_budget > 0 and body:
        seg_len = len(body) / float(segments)
        per = rest_budget / float(segments)
        half = max(1, int(per * 0.5))
        for i in range(segments):
            s = int(i * seg_len)
            e = len(body) if i == segments - 1 else int((i + 1) * seg_len)
            chunk = body[s:e]
            if len(chunk) <= per:
                out.append(chunk)
                continue
            omitted = len(chunk) - half * 2
            out.append(chunk[:half]
                       + f"\n\n……[本段省略 {omitted} 字符]……\n\n"
                       + chunk[-half:])
    return ''.join(out)


def run_extraction(filename, file_bytes, company_type='listed',
                   model_id='deepseek-v4-flash', progress_cb=None):
    """提取主流程（同步接口与异步任务共用）

    v=94：从原 /api/extract 路由中原样抽出，逻辑未做任何改动，
          使同步与异步两条路径共享同一实现。
          progress_cb 为 None 时行为与改造前完全一致。

    返回 (payload: dict, http_status: int)
    """
    start_time = time.time()
    file_size = len(file_bytes)

    # 2. 提取文本
    try:
        report_text = extract_text_from_file(
            filename, file_bytes, progress_cb=progress_cb
        )
    except Exception as e:
        logger.error(f"文档解析失败: {e}")
        return {"error": f"文档解析失败: {str(e)}"}, 400

    text_len = len(report_text)
    logger.info(
        f"📝 文本提取完成: {text_len} 字符"
        f"（OCR 兜底={'可用' if OCR_SUPPORT else '未安装'}）"
    )
    if text_len < 100:
        # 正常情况下扫描件已由 extract_text_from_pdf 自动 OCR 兜底；
        # 走到这里说明连 OCR 也没识别出有效文本（如空白页/纯图形）
        if OCR_SUPPORT:
            err = "该文档无可提取的文字层，OCR 识别后仍无有效文本，建议改传文字版报告"
        else:
            err = "提取文本过短，可能为扫描件PDF，且服务端 OCR 依赖未安装"
        return {"error": err}, 400

    # v=118：留一份**截断前**全文副本 —— 证据强制校验需要在完整文本上定位「原文」，
    #   若用截断后文本，报告后半段的真实证据会被误判为「编造」而误降级。
    #   （仅新增一个变量，原截断逻辑一字未动）
    full_report_text = report_text

    # 3. 控长（避免超出 token 限制）
    #    v119b（A2）：默认「头部连续保留 + 后半全篇配额采样」混合控长。
    #    为什么预算从 60000 提到 120000：
    #      · 60000 预算下，「保住前段」与「覆盖后段」是**零和**的 —— 实测
    #        纯配额采样虽拿到后段 5 项证据，却把前段 3 项（GHG 总量/能源强度/
    #        环保投资，@偏移 ~19,500）切掉，净损失真实存在；
    #      · 把 budget 提到 120000、head_ratio 设 0.5 ⇒ 头部连续区 = 前 60000 字符
    #        **正好等于 v118 的全部输入** ⇒ 前段证据一项不丢（严格不劣于 v118），
    #        额外 60000 预算再覆盖后半段 ⇒ **单调改进**，无需实验即确定不会变差。
    #      · 成本：只影响「本来就会被截断」的报告（中文中位 4.3 万字符不受影响），
    #        每份增加约 2~3 万 token。若需回退：.env 设 MAX_TEXT_CHARS=60000。
    #    legacy 模式保留原从头截断，用于 A/B 对照与紧急回滚。
    try:
        MAX_TEXT_CHARS = int(os.environ.get('MAX_TEXT_CHARS') or 120000)
    except (TypeError, ValueError):
        MAX_TEXT_CHARS = 120000
    if text_len > MAX_TEXT_CHARS:
        if TEXT_SAMPLE_MODE == 'legacy':
            logger.warning(
                f"文本过长({text_len}字符)，[legacy]截断到{MAX_TEXT_CHARS}字符"
            )
            report_text = report_text[:MAX_TEXT_CHARS] + "\n\n[后续内容已截断]"
        else:
            sampled = sample_text_by_quota(report_text, MAX_TEXT_CHARS)
            logger.warning(
                f"文本过长({text_len}字符)，[quota head={TEXT_SAMPLE_HEAD_RATIO:.0%}"
                f"×{TEXT_SAMPLE_SEGMENTS}段] 控长为 {len(sampled)} 字符"
                f"（头部连续保留 + 后半全篇抽样，前后段证据都不再整体丢弃）"
            )
            report_text = sampled

    # 4. 构建用户提示词
    user_text = f"""请从以下ESG报告内容中提取碳信息（按完整字段结构输出JSON）。

## 文件名
{filename}

## 报告内容
```
{report_text}
```

## 额外要求
- 缺失字段标注为"未披露"
- 严格按提示词的输出格式返回，不要添加任何解释
"""

    # 5. 调用 LLM（one-api 网关，按 model 路由；默认 deepseek-v4-flash）
    try:
        from models import call_llm
        result = call_llm(EXTRACTION_PROMPT, user_text, model_id=model_id)
    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        return {"error": f"LLM调用失败: {str(e)}"}, 500

    # 6. 解析LLM返回的JSON
    extracted_json = None
    try:
        extracted_json = json.loads(result['content'])
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败: {e}")
        logger.debug(f"LLM原始输出: {result['content'][:500]}")
        # v=204b：二级尝试 —— 剥掉 Markdown 代码围栏（MiniMax 会包 ```json）。
        cleaned = _strip_md_fence(result['content'])
        if cleaned != result['content']:
            try:
                extracted_json = json.loads(cleaned)
                logger.warning(
                    f"🧽 LLM 输出带 Markdown 代码围栏，剥离后解析成功"
                    f"（model={result.get('model')}，该模型未遵守 response_format）"
                )
            except json.JSONDecodeError as e2:
                logger.error(f"剥离围栏后仍非 JSON: {e2}")
        if extracted_json is None:
            # v=204：区分「输出语法错」与「输出被 max_tokens 腰斩」——两者的用户提示
            #   与处置方式完全不同。截断的典型特征是 finish_reason=length 且 content 极短
            #   （思维链把 16384 的输出预算吃干，content 只剩几个字符），此时提示
            #   「非JSON格式」会把排查方向带偏到提示词上（2026-10-21 实证）。
            if result.get('finish_reason') == 'length':
                logger.warning(
                    f"⚠️ 判定为输出截断而非格式错误：model={result.get('model')} "
                    f"content_len={len(result['content'])} tokens={result['tokens_used']}"
                )
                return {
                    "error": "模型输出被截断（输出预算耗尽）：思维链占满了 max_tokens，JSON 未写完",
                    "hint": "请换用其他模型；若必须用该模型，需要关闭其思考模式（见 models.py 的 thinking/reasoning_effort 注入）",
                    "raw_content": result['content'][:2000],
                    "tokens_used": result['tokens_used'],
                    "finish_reason": result.get('finish_reason'),
                }, 500
            return {
                "error": "LLM返回非JSON格式",
                "raw_content": result['content'][:2000],
                "tokens_used": result['tokens_used'],
                "finish_reason": result.get('finish_reason'),
            }, 500

    # 6.4 记录 LLM 输出结构（用于排错）
    logger.info(f"LLM 提取结果 keys: {list(extracted_json.keys()) if isinstance(extracted_json, dict) else type(extracted_json)}")
    if isinstance(extracted_json, dict):
        ext = extracted_json.get('extracted', extracted_json)
        sample = json.dumps(ext, ensure_ascii=False)[:200]
        logger.info(f"LLM 提取 indicators 数据预览: {sample}")

    # 6.5 关键！后端兜底——保证 4 维度嵌套结构完整（防止 LLM 抽风拍扁）
    extracted_json = normalize_extracted_structure(extracted_json)

    # 6.55 证据强制校验（v=118，方案A）：**任何拿到分的指标，其「原文」必须能在
    #      报告全文中定位到**；定位不到 ⇒ 降级为未披露。作用：防「原文是编造的」幻觉，
    #      同时让每一条得分可溯源、可审计。
    #      防御性：任何异常只记录、绝不影响提取与评分主流程；EVIDENCE_VERIFY=0 可整体关停。
    #      ⚠️ 必须用 full_report_text（截断前全文），否则报告后半段证据会被误杀。
    try:
        from evidence_verify import verify_document
        extracted_json, ev_report = verify_document(extracted_json, full_report_text)
        extracted_json['证据校验'] = ev_report
        logger.info(
            f"🔍 证据校验: 校验 {ev_report['checked']} 项 / 降级 {ev_report['failed']} 项"
            f" / 跳过 {ev_report['skipped']} 项（{ev_report['reason'] or 'ok'}）"
        )
        if ev_report['failed']:
            logger.warning(f"🛡️ 证据不足已降级: {[d['指标'] for d in ev_report['degraded']]}")
    except Exception as e:
        logger.warning(f"证据校验异常（忽略，不影响提取）: {e}")

    # 6.6 评分增强（本地查表 + 确定性计算，无LLM参与）
    #     输入: 提取JSON + 行业 + 公司类型
    #     输出: L4计算结果(42项含权重/均值/完整度/质量/基础/对标/L4得分/障碍度) + 维度得分 + 总分 + 等级 + 障碍度Top10
    score_data = None
    try:
        from weights_logic import score_all
        meta = extracted_json.get('报告元数据', {})
        industry = str(meta.get('所属行业') or '').strip()
        score_data = score_all(extracted_json, industry, company_type)
        # 评分增强数据并入提取结果（前端两段式渲染第一段：分数立即填充）
        extracted_json['评分增强数据'] = score_data
        logger.info(f"🎯 评分增强完成: 行业={industry} 类型={company_type} 总分={score_data['总分']} 等级={score_data['评分等级']} 指标数={len(score_data['L4计算结果'])}")
    except Exception as e:
        logger.error(f"评分增强失败(不影响提取): {e}", exc_info=True)

    # 7. 组装最终返回
    duration = round(time.time() - start_time, 2)
    response = {
        "success": True,
        "filename": filename,
        "file_size_kb": round(file_size / 1024, 1),
        "text_chars": text_len,
        "duration_sec": duration,
        "model": result['model'],
        "tokens_used": result['tokens_used'],
        "company_type": company_type,
        "score_calculated": score_data is not None,
        "extracted": extracted_json
    }
    logger.info(f"✅ 提取完成: {filename} | {duration}s | {result['tokens_used']} tokens")
    return response, 200


@app.route('/api/extract_score', methods=['POST'])
def extract_score():
    """提取+评分一站式（评分需外部接入ADP）

    注意：当前版本只做提取，评分通过文件输出或专门的ADP接口处理
    """
    # 简化：复用提取主流程，结果存到共享卷供 ADP 读取
    # v=94：主流程已抽成 run_extraction()，不再返回 Flask 响应对象
    parsed = _parse_extract_request()
    if isinstance(parsed, tuple):  # 错误响应（缺文件/文件名为空）
        return parsed
    filename = parsed['filename']
    data, status_code = run_extraction(
        filename, parsed['file_bytes'],
        company_type=parsed['company_type'], model_id=parsed['model_id']
    )
    if status_code != 200:  # 提取失败：原样透出后端原因
        return jsonify(data), status_code

    extracted = data.get('extracted', {})

    # 可选：保存到共享目录，供 ADP 后续读取
    output_dir = os.environ.get('EXTRACT_OUTPUT_DIR', '/app/output')
    os.makedirs(output_dir, exist_ok=True)
    filename = data.get('filename', 'unknown')
    out_path = os.path.join(output_dir, f"{Path(filename).stem}_extracted.json")
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(extracted, f, ensure_ascii=False, indent=2)
    logger.info(f"💾 提取结果已保存: {out_path}")

    return jsonify({
        **data,
        "output_path": out_path,
        "next_step": "请将 extracted 字段传给 ADP 评分智能体二"
    })




@app.route('/api/report', methods=['POST'])
def generate_report():
    """v=85：WeasyPrint 生成精美 PDF 报告（替代前端 pdfmake 单调排版）

    请求体 JSON：
      company:     企业名称
      report_year: 报告年份
      total_score: 总分
      grade:       评分等级
      dim_scores:  {'企业内部碳管理': 7.46, ...} 四维度得分
      suggestions: [{'指标','失分类型','根源分析','建议','优先级','验证理由'}, ...]
      radar_img:   可选，雷达图 base64 PNG
    返回：application/pdf 文件流
    """
    start = time.time()
    try:
        if not WEASYPRINT_SUPPORT or not JINJA_SUPPORT:
            return jsonify({"error": "WeasyPrint/Jinja2 未安装，无法生成报告"}), 503

        req = request.get_json(force=True, silent=True)
        if not req:
            return jsonify({"error": "请求体为空（需 JSON）"}), 400

        # 加载 Jinja2 模板（容器内 /app/templates/report.html）
        template_path = os.path.join(os.path.dirname(__file__), 'templates', 'report.html')
        if not os.path.exists(template_path):
            return jsonify({"error": f"报告模板不存在: {template_path}"}), 500
        with open(template_path, encoding='utf-8') as f:
            template_str = f.read()

        # 维度得分（保证 4 维度都有值）
        dim_scores = req.get('dim_scores') or {}
        DIM_ORDER = ['企业内部碳管理', '供应链碳管理', '透明度', '金融化情况']
        dim_scores_ordered = {}
        for dim in DIM_ORDER:
            val = dim_scores.get(dim, 0)
            dim_scores_ordered[dim] = round(float(val), 2) if val is not None else 0.0

        total = req.get('total_score', 0)
        try:
            total = round(float(total), 2)
        except (TypeError, ValueError):
            total = 0.0

        render_ctx = {
            'company': str(req.get('company') or '待评估企业'),
            'report_year': str(req.get('report_year') or '—'),
            'total_score': total,
            'grade': str(req.get('grade') or '—'),
            'dim_scores': dim_scores_ordered,
            'suggestions': req.get('suggestions') or [],
            'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            # v=100：可视化图表（前端离屏 canvas 截图，base64 dataURL；空值则模板不渲染该章节）
            'radar_img': req.get('radar_img') or '',
            'obstacle_img': req.get('obstacle_img') or '',
            'benchmark_img': req.get('benchmark_img') or '',
            # v=100：Agent2 诊断摘要
            'conclusion': req.get('conclusion') or '',
            # v=101：Agent2 企业画像（说清"这是一家什么企业"，空值则模板不渲染该行）
            'profile': req.get('profile') or '',
        }

        html_content = Template(template_str).render(**render_ctx)

        # WeasyPrint 渲染（base_url 指向模板目录，支持相对资源）
        pdf_bytes = HTML(string=html_content, base_url=os.path.dirname(template_path)).write_pdf()

        import io
        duration = round(time.time() - start, 2)
        logger.info(f"📄 [report] PDF 生成成功: {render_ctx['company']} | {duration}s | {len(pdf_bytes)/1024:.1f}KB")
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f"碳治理画像报告-{render_ctx['company']}.pdf"
        )
    except Exception as e:
        logger.error(f"❌ [report] PDF 生成失败: {e}", exc_info=True)
        return jsonify({"error": f"PDF 生成失败: {str(e)}"}), 500


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "文件过大（超过200MB）"}), 413


@app.errorhandler(500)
def server_error(e):
    logger.error(f"服务器内部错误: {e}")
    return jsonify({"error": "服务器内部错误", "detail": str(e)}), 500


# ============================== 启动 ==============================
def main():
    parser = argparse.ArgumentParser(description='ESG碳信息提取服务')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8082)))
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("🌱 ESG 碳信息提取服务")
    logger.info("=" * 60)
    logger.info(f"监听: {args.host}:{args.port}")
    logger.info(f"模型: {DEEPSEEK_MODEL}")
    logger.info(f"API Key: {'已设置' if DEEPSEEK_API_KEY else '❌ 未设置！无法调用LLM'}")
    logger.info(f"PDF支持: {PDF_SUPPORT} | DOCX支持: {DOCX_SUPPORT}")
    logger.info(f"提示词: {len(EXTRACTION_PROMPT)}字符")
    logger.info("接口: GET /api/health | POST /api/extract | POST /api/extract_score")
    logger.info("异步: POST /api/extract_async | GET /api/extract_status/<task_id>")
    logger.info("=" * 60)
    # v=94：必须显式开启多线程 —— 异步提取任务在后台线程跑，
    #       轮询 /api/extract_status 必须能并发响应，否则会被长任务阻塞。
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == '__main__':
    main()
