/* ============================================================
   ESG 碳治理能力画像系统 — 首屏特效层 v200
   1. 星空粒子：黑暗中星星点点闪烁
   2. 自转地球：基于球面映射的纹理自转（半个地球，底部渐隐）
   3. 光圈交互：hover 亮 / 移开恢复（CSS 配合）
   4. 点击地球：平滑下滑到上传区
   独立 IIFE，不依赖也不会与 app.bundle.js 冲突
   ============================================================ */
(function () {
    'use strict';

    /* ---------- 1. 星空粒子 ---------- */
    function initStarField(canvas) {
        if (!canvas || !canvas.getContext) return;
        var ctx = canvas.getContext('2d');
        var stars = [];
        var W = 0, H = 0;
        var DPR = Math.min(window.devicePixelRatio || 1, 2);

        function resize() {
            var parent = canvas.parentElement;
            W = parent ? parent.clientWidth : window.innerWidth;
            H = parent ? parent.clientHeight : window.innerHeight;
            canvas.width = Math.floor(W * DPR);
            canvas.height = Math.floor(H * DPR);
            canvas.style.width = W + 'px';
            canvas.style.height = H + 'px';
            ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
            buildStars();
        }

        function buildStars() {
            stars = [];
            var count = Math.min(160, Math.floor(W * H / 9000));
            for (var i = 0; i < count; i++) {
                stars.push({
                    x: Math.random() * W,
                    y: Math.random() * H,
                    r: Math.random() * 1.4 + 0.35,
                    base: Math.random() * 0.6 + 0.3,
                    speed: Math.random() * 0.02 + 0.005,
                    phase: Math.random() * Math.PI * 2,
                    hue: Math.random() < 0.18 ? '96, 165, 250' : (Math.random() < 0.14 ? '52, 211, 153' : '226, 232, 240')
                });
            }
        }

        var t = 0;
        function draw() {
            t += 0.016;
            ctx.clearRect(0, 0, W, H);
            for (var i = 0; i < stars.length; i++) {
                var s = stars[i];
                var alpha = s.base + Math.sin(t * 8 + s.phase) * s.speed * 2.2;
                alpha = Math.max(0.04, Math.min(1, alpha));
                ctx.beginPath();
                ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
                ctx.fillStyle = 'rgba(' + s.hue + ',' + alpha + ')';
                ctx.shadowColor = 'rgba(' + s.hue + ',0.8)';
                ctx.shadowBlur = s.r * 4;
                ctx.fill();
                ctx.shadowBlur = 0;
            }
            requestAnimationFrame(draw);
        }

        window.addEventListener('resize', resize);
        resize();
        draw();
    }

    /* ---------- 2. 自转地球（球面映射） ---------- */
    function initEarth(canvas) {
        if (!canvas || !canvas.getContext) return;

        var img = new Image();
        img.src = 'assets/earth.jpg';
        var ctx = canvas.getContext('2d');
        var ready = false;
        var DPR = Math.min(window.devicePixelRatio || 1, 2);
        var angle = 0;
        var SIZE = 0;
        var cx = 0, cy = 0, r = 0;
        var glowCanvas = document.getElementById('earthGlow');
        var glowCtx = glowCanvas && glowCanvas.getContext ? glowCanvas.getContext('2d') : null;

        function resize() {
            var parent = canvas.parentElement;
            if (!parent) return;
            SIZE = parent.clientWidth * 0.88;
            canvas.width = Math.floor(SIZE * DPR);
            canvas.height = Math.floor(SIZE * DPR);
            canvas.style.width = SIZE + 'px';
            canvas.style.height = SIZE + 'px';
            ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
            cx = SIZE / 2;
            cy = SIZE / 2;
            r = SIZE * 0.42;
        }

        function draw() {
            ctx.clearRect(0, 0, SIZE, SIZE);
            if (!ready) return;

            // —— 球面映射：逐列取纹理绘制，形成自转立体球 ——
            var w = img.width;
            var h = img.height;
            var step = Math.max(1, Math.round(r / 190)); // 性能：球直径像素列按比例取整
            var dx;
            for (dx = -r; dx < r; dx += step) {
                var phi = Math.acos(-dx / r);          // 0..PI
                var tx = ((w * phi) / Math.PI + angle) % w;
                var halfH = r * Math.sin(phi);          // 该列可见高度
                var px = (tx | 0);
                ctx.drawImage(img, px, 0, step, h, cx + dx, cy - halfH, step, halfH * 2);
            }

            // —— 球体立体感：边缘阴影 + 高光 ——
            var grad = ctx.createRadialGradient(
                cx - r * 0.35, cy - r * 0.4, r * 0.05,
                cx, cy, r
            );
            grad.addColorStop(0, 'rgba(255,255,255,0.16)');
            grad.addColorStop(0.45, 'rgba(255,255,255,0)');
            grad.addColorStop(0.78, 'rgba(2,6,14,0.38)');
            grad.addColorStop(1, 'rgba(2,6,14,0.92)');
            ctx.beginPath();
            ctx.arc(cx, cy, r, 0, Math.PI * 2);
            ctx.fillStyle = grad;
            ctx.fill();

            // —— 底部渐隐：地球沉入黑暗中（"半个地球"效果） ——
            var fade = ctx.createLinearGradient(0, cy - r * 0.2, 0, cy + r * 0.55);
            fade.addColorStop(0, 'rgba(4,7,13,0)');
            fade.addColorStop(1, 'rgba(4,7,13,1)');
            ctx.beginPath();
            ctx.arc(cx, cy, r, 0, Math.PI * 2);
            ctx.fillStyle = fade;
            ctx.fill();

            // —— 大气辉光画布（由 CSS 控制 hover 透明度） ——
            if (glowCtx) {
                var gs = glowCanvas.clientWidth;
                glowCanvas.width = Math.floor(gs * DPR);
                glowCanvas.height = Math.floor(gs * DPR);
                glowCtx.setTransform(DPR, 0, 0, DPR, 0, 0);
                glowCtx.clearRect(0, 0, gs, gs);
                var glowGrad = glowCtx.createRadialGradient(gs / 2, gs / 2, gs * 0.34, gs / 2, gs / 2, gs * 0.5);
                glowGrad.addColorStop(0, 'rgba(52,211,153,0.14)');
                glowGrad.addColorStop(0.55, 'rgba(96,165,250,0.08)');
                glowGrad.addColorStop(1, 'rgba(52,211,153,0)');
                glowCtx.beginPath();
                glowCtx.arc(gs / 2, gs / 2, gs * 0.5, 0, Math.PI * 2);
                glowCtx.fillStyle = glowGrad;
                glowCtx.fill();
            }

            angle += 0.0035; // 自转速度
            requestAnimationFrame(draw);
        }

        img.onload = function () {
            ready = true;
            draw();
        };
        img.onerror = function () {
            // 纹理加载失败时退化为纯色发光球，不影响页面
            ready = true;
            var fallback = ctx.createRadialGradient(cx, cy, r * 0.1, cx, cy, r);
            fallback.addColorStop(0, 'rgba(16,185,129,0.5)');
            fallback.addColorStop(1, 'rgba(2,6,14,0.95)');
            ctx.beginPath();
            ctx.arc(cx, cy, r, 0, Math.PI * 2);
            ctx.fillStyle = fallback;
            ctx.fill();
        };

        window.addEventListener('resize', resize);
        resize();
        // 若图片已缓存（onload 未触发）主动重试
        if (img.complete && img.naturalWidth > 0) {
            ready = true;
            draw();
        }
    }

    /* ---------- 3. 点击地球下滑到上传区 ---------- */
    function bindEarthClick() {
        var wrap = document.querySelector('.hero-earth-wrap');
        if (!wrap) return;
        wrap.addEventListener('click', function (e) {
            e.preventDefault();
            if (typeof scrollToSection === 'function') {
                scrollToSection('upload');
            } else {
                var target = document.getElementById('upload');
                if (target) target.scrollIntoView({ behavior: 'smooth' });
            }
        });
    }

    /* ---------- 4. 滚动视差（轻微） ---------- */
    function initParallax() {
        var hero = document.getElementById('hero');
        var glow = hero && hero.querySelector('.hero-bg-glow');
        var earthWrap = document.querySelector('.hero-earth-wrap');
        if (!glow && !earthWrap) return;
        var ticking = false;
        function onScroll() {
            if (ticking) return;
            ticking = true;
            requestAnimationFrame(function () {
                var y = window.scrollY || 0;
                if (y < window.innerHeight * 1.2) {
                    if (glow) glow.style.transform = 'translate(-50%, calc(-50% + ' + y * 0.12 + 'px))';
                    if (earthWrap) earthWrap.style.transform = 'translateY(' + y * 0.05 + 'px)';
                }
                ticking = false;
            });
        }
        window.addEventListener('scroll', onScroll, { passive: true });
    }

    /* ---------- 启动 ---------- */
    function boot() {
        var star = document.getElementById('starCanvas');
        var earth = document.getElementById('earthCanvas');
        if (star) initStarField(star);
        if (earth) initEarth(earth);
        bindEarthClick();
        initParallax();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
