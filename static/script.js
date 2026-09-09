// === FoodCheck — Progressive 3-Stage Pipeline + Slider Theme Engine ===

document.addEventListener('DOMContentLoaded', () => {

    // ── Theme Slider Engine ────────────────────────────────────────────────
    const themeSlider = document.getElementById('theme-slider');
    const themeMap = ['light', 'dark', 'techno'];
    const root = document.documentElement;

    function applyTheme(theme) {
        root.setAttribute('data-theme', theme);
        localStorage.setItem('fc-theme', theme);
        const idx = themeMap.indexOf(theme);
        if (idx !== -1) themeSlider.value = idx;
        // Highlight active label
        document.querySelectorAll('.slider-labels span').forEach(s => {
            s.classList.toggle('active-label', Number(s.dataset.val) === idx);
        });
    }

    // Init from saved preference
    const saved = localStorage.getItem('fc-theme');
    applyTheme(saved && themeMap.includes(saved) ? saved : 'dark');

    themeSlider.addEventListener('input', () => {
        applyTheme(themeMap[Number(themeSlider.value)]);
    });

    // ── DOM References ─────────────────────────────────────────────────────
    const dropZone       = document.getElementById('drop-zone');
    const fileInput      = document.getElementById('file-input');
    const browseBtn      = document.getElementById('browse-btn');
    const openCameraBtn  = document.getElementById('open-camera-btn');
    const cameraTrigger  = document.getElementById('camera-card-trigger');
    const fallbackCamBtn = document.getElementById('fallback-camera-btn');
    const mobileCamInput = document.getElementById('mobile-camera-input');
    const cameraModal    = document.getElementById('camera-modal');
    const cameraVideo    = document.getElementById('camera-video');
    const cameraCanvas   = document.getElementById('camera-canvas');
    const snapBtn        = document.getElementById('snap-btn');
    const flipCameraBtn  = document.getElementById('flip-camera-btn');
    const closeCameraBtn = document.getElementById('close-camera-btn');
    const previewSection = document.getElementById('preview-section');
    const imagePreview   = document.getElementById('image-preview');
    const changeBtn      = document.getElementById('change-btn');
    const analyzeBtn     = document.getElementById('analyze-btn');
    const uploadSection  = document.getElementById('upload-section');
    const loadingSection = document.getElementById('loading-section');
    const errorSection   = document.getElementById('error-section');
    const errorMessage   = document.getElementById('error-message');
    const retryBtn       = document.getElementById('retry-btn');
    const reportSection  = document.getElementById('report-section');

    let selectedFile = null;
    let cameraStream = null;
    let currentFacingMode = 'environment';

    // ── File & Drop ────────────────────────────────────────────────────────
    dropZone.addEventListener('click', (e) => {
        if (e.target.closest('#browse-btn')) return;
        fileInput.click();
    });
    browseBtn.addEventListener('click', (e) => { e.stopPropagation(); fileInput.click(); });

    dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('dragover'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
    dropZone.addEventListener('drop', (e) => {
        e.preventDefault(); dropZone.classList.remove('dragover');
        if (e.dataTransfer.files.length > 0) handleFile(e.dataTransfer.files[0]);
    });

    fileInput.addEventListener('change', (e) => { if (e.target.files.length) handleFile(e.target.files[0]); });
    mobileCamInput.addEventListener('change', (e) => {
        if (e.target.files.length) { stopCamera(); cameraModal.classList.add('hidden'); handleFile(e.target.files[0]); }
    });
    fallbackCamBtn.addEventListener('click', () => mobileCamInput.click());

    // Camera card click opens camera
    cameraTrigger.addEventListener('click', (e) => {
        if (e.target.closest('#open-camera-btn')) return;
        startCamera();
    });
    openCameraBtn.addEventListener('click', (e) => { e.stopPropagation(); startCamera(); });

    // ── Camera ─────────────────────────────────────────────────────────────
    closeCameraBtn.addEventListener('click', () => { stopCamera(); cameraModal.classList.add('hidden'); });
    flipCameraBtn.addEventListener('click', () => {
        currentFacingMode = currentFacingMode === 'environment' ? 'user' : 'environment';
        stopCamera(); startCamera();
    });

    async function startCamera() {
        cameraModal.classList.remove('hidden');
        try {
            if (!navigator.mediaDevices?.getUserMedia) throw new Error('not supported');
            cameraStream = await navigator.mediaDevices.getUserMedia({
                video: { facingMode: { ideal: currentFacingMode }, width: { ideal: 1920 }, height: { ideal: 1080 } },
                audio: false
            });
            cameraVideo.srcObject = cameraStream;
            await cameraVideo.play();
        } catch {
            stopCamera(); cameraModal.classList.add('hidden');
            if (confirm('Camera unavailable. Use device camera app instead?')) mobileCamInput.click();
        }
    }
    function stopCamera() {
        if (cameraStream) { cameraStream.getTracks().forEach(t => t.stop()); cameraStream = null; }
        if (cameraVideo.srcObject) cameraVideo.srcObject = null;
    }
    snapBtn.addEventListener('click', () => {
        if (!cameraVideo.videoWidth) return;
        cameraCanvas.width = cameraVideo.videoWidth;
        cameraCanvas.height = cameraVideo.videoHeight;
        cameraCanvas.getContext('2d').drawImage(cameraVideo, 0, 0);
        cameraCanvas.toBlob((blob) => {
            if (!blob) return;
            stopCamera(); cameraModal.classList.add('hidden');
            handleFile(new File([blob], `snap-${Date.now()}.jpg`, { type: 'image/jpeg' }));
        }, 'image/jpeg', 0.92);
    });

    changeBtn.addEventListener('click', () => resetUpload());
    retryBtn.addEventListener('click', () => resetUpload());
    analyzeBtn.addEventListener('click', () => { if (selectedFile) runPipeline(selectedFile); });

    function handleFile(file) {
        const ok = ['image/jpeg', 'image/png', 'image/webp'];
        if (!ok.includes(file.type)) { showError('Invalid file type. Upload JPG, PNG, or WebP.'); return; }
        if (file.size > 5 * 1024 * 1024) { showError('File too large. Max 5MB.'); return; }
        selectedFile = file;
        const r = new FileReader();
        r.onload = (e) => {
            imagePreview.src = e.target.result;
            // Hide hero elements, show preview
            document.querySelector('.hero-grid').classList.add('hidden');
            document.querySelector('.facts-strip').classList.add('hidden');
            document.querySelector('.stats-grid').classList.add('hidden');
            previewSection.classList.remove('hidden');
        };
        r.readAsDataURL(file);
    }

    function resetUpload() {
        selectedFile = null;
        fileInput.value = ''; mobileCamInput.value = '';
        imagePreview.src = '';
        stopCamera(); cameraModal.classList.add('hidden');
        document.querySelector('.hero-grid').classList.remove('hidden');
        document.querySelector('.facts-strip').classList.remove('hidden');
        document.querySelector('.stats-grid').classList.remove('hidden');
        previewSection.classList.add('hidden');
        errorSection.classList.add('hidden');
        reportSection.classList.add('hidden');
        uploadSection.classList.remove('hidden');
        loadingSection.classList.add('hidden');
        ['product-card','ingredients-card','nutrition-card','health-card','flags-card','alternatives-card','news-card','report-actions-card']
            .forEach(id => document.getElementById(id).innerHTML = '');
    }

    function showError(msg) {
        loadingSection.classList.add('hidden');
        uploadSection.classList.add('hidden');
        errorSection.classList.remove('hidden');
        errorMessage.textContent = msg;
        document.querySelectorAll('.step').forEach(s => s.classList.remove('active','done'));
    }

    // ── 3-STAGE PROGRESSIVE PIPELINE ───────────────────────────────────────
    async function runPipeline(file) {
        uploadSection.classList.add('hidden');
        loadingSection.classList.remove('hidden');
        errorSection.classList.add('hidden');
        reportSection.classList.add('hidden');

        document.querySelectorAll('.step').forEach(s => s.classList.remove('active','done'));
        document.getElementById('step-vision').classList.add('active');

        const fd = new FormData();
        fd.append('image', file);

        try {
            // ── STAGE 1: Vision ─────────────────────────────────────────
            const vr = await fetch('/analyze/vision', { method: 'POST', body: fd });
            document.getElementById('step-vision').classList.remove('active');
            document.getElementById('step-vision').classList.add('done');

            let vision;
            try { vision = await vr.json(); } catch { showError('Server returned invalid response.'); return; }
            if (!vr.ok) { showError(vision.error || 'Vision analysis failed.'); return; }

            // ── Show product + ingredients + nutrition INSTANTLY ─────────
            loadingSection.classList.add('hidden');
            reportSection.classList.remove('hidden');
            renderProductCard(vision);
            renderIngredientsCard(vision);
            renderNutritionCard(vision);

            // Placeholders for loading sections
            renderHealthPlaceholder();
            renderNewsPlaceholder();
            renderActionsCard();

            // ── STAGE 2 + 3: Parallel ───────────────────────────────────
            document.getElementById('step-health').classList.add('active');
            document.getElementById('step-news').classList.add('active');

            const hp = fetchHealth(vision);
            const np = fetchNews(vision.brand);

            hp.then(h => {
                document.getElementById('step-health').classList.remove('active');
                document.getElementById('step-health').classList.add('done');
                renderHealthCard(h);
                renderFlagsCard(h);
                renderAlternativesCard(h);
            });

            np.then(articles => {
                document.getElementById('step-news').classList.remove('active');
                document.getElementById('step-news').classList.add('done');
                renderNewsCard(articles);
            });

        } catch (err) {
            console.error('Pipeline error:', err);
            showError(err.message || 'Network error. Check your connection.');
        }
    }

    async function fetchHealth(vision) {
        try {
            const r = await fetch('/analyze/health', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    ingredients_text: vision.ingredients_text,
                    nutrition_facts: vision.nutrition_facts,
                    product_name: vision.product_name,
                }),
            });
            return await r.json();
        } catch {
            return { score: null, verdict: 'unknown', flags: [], alternatives: [], reasoning: 'Health analysis unavailable.' };
        }
    }

    async function fetchNews(brand) {
        try {
            const r = await fetch(`/news?brand=${encodeURIComponent(brand)}`);
            const d = await r.json();
            return d.articles || [];
        } catch { return []; }
    }

    // ── RENDER FUNCTIONS ───────────────────────────────────────────────────

    function renderProductCard(v) {
        const thumb = v.off_match?.image_url ? `<img src="${v.off_match.image_url}" alt="" class="product-thumb">` : '';
        document.getElementById('product-card').innerHTML = `
            <section class="report-card product-info fade-in">
                <div class="product-header">
                    ${thumb}
                    <div>
                        <h2>${esc(v.product_name)}</h2>
                        <p class="brand-name">by ${esc(v.brand)}</p>
                    </div>
                </div>
            </section>`;
    }

    function renderIngredientsCard(v) {
        document.getElementById('ingredients-card').innerHTML = `
            <section class="report-card fade-in">
                <h3>📋 Ingredients</h3>
                <p class="ingredients-text">${esc(v.ingredients_text || 'Not available')}</p>
                ${v.data_source === 'ai_knowledge' || v.data_source === 'partial'
                    ? '<div class="db-match-note warn">🤖 Data sourced from AI knowledge</div>' : ''}
            </section>`;
    }

    function renderNutritionCard(v) {
        if (!v.nutrition_facts || Object.keys(v.nutrition_facts).length === 0) return;
        const labels = {
            energy_kcal: 'Energy (kcal)', protein_g: 'Protein (g)',
            total_fat_g: 'Total Fat (g)', saturated_fat_g: 'Saturated Fat (g)',
            trans_fat_g: 'Trans Fat (g)', carbohydrates_g: 'Carbohydrates (g)',
            total_sugar_g: 'Total Sugar (g)', sodium_mg: 'Sodium (mg)',
            fiber_g: 'Dietary Fiber (g)'
        };
        const rows = Object.entries(labels)
            .filter(([k]) => v.nutrition_facts[k] != null)
            .map(([k, l]) => `<tr><td>${l}</td><td>${Number(v.nutrition_facts[k]).toFixed(1)}</td></tr>`)
            .join('');

        let dbNote = '';
        if (v.off_match) {
            let extra = '';
            if (v.off_match.nutriscore_grade) extra += ` • Nutri-Score: <strong>${v.off_match.nutriscore_grade.toUpperCase()}</strong>`;
            if (v.off_match.nova_group) extra += ` • NOVA: <strong>${v.off_match.nova_group}</strong>`;
            dbNote = `<div class="db-match-note">✅ Cross-checked with Open Food Facts${extra}</div>`;
        } else if (v.data_source === 'ai_knowledge' || v.data_source === 'partial') {
            dbNote = `<div class="db-match-note warn">🤖 AI knowledge data</div>`;
        } else {
            dbNote = `<div class="db-match-note warn">ℹ️ AI analysis only (no database match)</div>`;
        }

        document.getElementById('nutrition-card').innerHTML = `
            <section class="report-card fade-in">
                <h3>📊 Nutrition Facts <span class="per-serving">(per 100g)</span></h3>
                <table class="nutrition-table">
                    <thead><tr><th>Nutrient</th><th>Value</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
                ${dbNote}
            </section>`;
    }

    function renderHealthPlaceholder() {
        document.getElementById('health-card').innerHTML = `
            <section class="report-card fade-in">
                <h3>Health & Safety Score</h3>
                <div class="section-loader"><div class="spinner-small"></div><span>Analyzing health & safety...</span></div>
            </section>`;
        document.getElementById('flags-card').innerHTML = '';
        document.getElementById('alternatives-card').innerHTML = '';
    }

    function renderHealthCard(h) {
        const sc = h.verdict === 'safe' ? 'safe' : h.verdict === 'moderate' ? 'moderate' : 'unsafe';
        document.getElementById('health-card').innerHTML = `
            <section class="report-card fade-in health-score">
                <h3>Health & Safety Score</h3>
                ${h.score != null ? `
                    <div class="score-display">
                        <div class="score-badge score-${sc}">${h.score}</div>
                        <span class="verdict-label text-${sc}">${h.verdict.toUpperCase()}</span>
                    </div>` : '<p class="muted">Score could not be determined</p>'}
                <p class="reasoning">${esc(h.reasoning)}</p>
            </section>`;
    }

    function renderFlagsCard(h) {
        if (!h.flags?.length) { document.getElementById('flags-card').innerHTML = ''; return; }
        document.getElementById('flags-card').innerHTML = `
            <section class="report-card fade-in flags-section">
                <h3>⚠️ Health Flags</h3>
                <div class="flags-list">
                    ${h.flags.map(f => `
                        <div class="flag-item severity-${f.severity || 'medium'}">
                            <span class="flag-type">${esc(f.type)}</span>
                            <span class="flag-severity">${esc((f.severity||'medium').toUpperCase())}</span>
                            <p>${esc(f.explanation)}</p>
                        </div>`).join('')}
                </div>
            </section>`;
    }

    function renderAlternativesCard(h) {
        if (!h.alternatives?.length) { document.getElementById('alternatives-card').innerHTML = ''; return; }
        document.getElementById('alternatives-card').innerHTML = `
            <section class="report-card fade-in alternatives-section">
                <div class="alternatives-header">
                    <h3>💡 Healthier Alternatives</h3>
                    <span class="alt-header-tag">Indian Market</span>
                </div>
                <div class="alternatives-grid">
                    ${h.alternatives.map(a => `
                        <div class="alt-card">
                            <div class="alt-card-header">
                                <div class="alt-title-wrap"><span class="alt-icon">🌿</span><h4>${esc(a.name)}</h4></div>
                                <span class="alt-category-badge">${esc(a.category || 'Healthier Swap')}</span>
                            </div>
                            <p class="alt-why"><strong>Why better:</strong> ${esc(a.why_better)}</p>
                        </div>`).join('')}
                </div>
            </section>`;
    }

    function renderNewsPlaceholder() {
        document.getElementById('news-card').innerHTML = `
            <section class="report-card fade-in news-section">
                <h3>📰 Company News & Controversies</h3>
                <div class="section-loader"><div class="spinner-small"></div><span>Fetching news...</span></div>
            </section>`;
    }

    function renderNewsCard(articles) {
        if (!articles?.length) {
            document.getElementById('news-card').innerHTML = `
                <section class="report-card fade-in news-section">
                    <h3>📰 Company News</h3>
                    <p class="muted">No recent news or safety controversies found.</p>
                </section>`;
            return;
        }
        document.getElementById('news-card').innerHTML = `
            <section class="report-card fade-in news-section">
                <h3>📰 Company News & Controversies</h3>
                <div class="news-grid">
                    ${articles.map(a => `
                        <a href="${a.url}" target="_blank" rel="noopener" class="news-card">
                            ${a.image ? `<img src="${a.image}" alt="" class="news-thumb" loading="lazy">` : '<div class="news-icon-badge">📰</div>'}
                            <div class="news-content">
                                <h4>${esc(a.title)}</h4>
                                ${a.description ? `<p class="news-desc">${esc(a.description)}</p>` : ''}
                                <div class="news-meta">
                                    <span class="news-source">🏛️ ${esc(a.source)}</span>
                                    <span>${a.published_at ? esc(a.published_at.slice(0,16)) : ''}</span>
                                </div>
                            </div>
                        </a>`).join('')}
                </div>
            </section>`;
    }

    function renderActionsCard() {
        document.getElementById('report-actions-card').innerHTML = `
            <div class="report-actions">
                <button class="btn-primary" style="width:auto;display:inline-block;" onclick="location.reload()">← Analyze Another Product</button>
            </div>`;
    }

    function esc(str) {
        if (!str) return '';
        const d = document.createElement('div');
        d.textContent = str;
        return d.innerHTML;
    }
});
