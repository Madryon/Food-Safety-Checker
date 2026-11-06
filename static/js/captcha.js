/**
 * static/js/captcha.js
 * FoodCheck Reusable CAPTCHA Component & Hook
 * - useCaptcha: Hook-style state controller
 * - CaptchaCheckbox: Reusable "[ ] I'm not a robot" widget
 * - CaptchaModal: Accessible 3x3 / 4x4 image selection & audio alternative modal
 * - FoodCheckCaptcha: Unified initializer supporting Custom, reCAPTCHA, and Turnstile
 */

(function (window, document) {
    'use strict';

    // SVG Icons
    var ICONS = {
        check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><polyline points="20 6 9 17 4 12"></polyline></svg>',
        shield: '<svg class="fc-badge-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path></svg>',
        refresh: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>',
        audio: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"></path></svg>',
        image: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline></svg>',
        info: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>',
        play: '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>'
    };

    /**
     * useCaptcha: Core state manager hook
     */
    function useCaptcha(options) {
        options = options || {};
        var state = {
            status: 'unverified', // 'unverified' | 'loading' | 'modal_open' | 'verified'
            token: null,
            challenge: null,
            selectedIndexes: new Set(),
            isAudioMode: false,
            audioPlaying: false,
            errorMessage: null
        };

        var listeners = [];
        function emit() {
            for (var i = 0; i < listeners.length; i++) {
                try { listeners[i](state); } catch (e) { console.error(e); }
            }
        }

        return {
            getState: function () { return state; },
            subscribe: function (fn) {
                listeners.push(fn);
                return function () {
                    listeners = listeners.filter(function (l) { return l !== fn; });
                };
            },
            reset: function () {
                state.status = 'unverified';
                state.token = null;
                state.challenge = null;
                state.selectedIndexes.clear();
                state.isAudioMode = false;
                state.errorMessage = null;
                emit();
            },
            fetchChallenge: function (callback) {
                state.status = 'loading';
                state.errorMessage = null;
                state.selectedIndexes.clear();
                emit();

                fetch('/api/captcha/generate')
                    .then(function (res) {
                        return res.json().then(function (data) {
                            return { ok: res.ok, status: res.status, data: data };
                        });
                    })
                    .then(function (res) {
                        if (!res.ok || !res.data.success) {
                            state.status = 'unverified';
                            state.errorMessage = res.data.message || 'Error loading challenge.';
                            emit();
                            if (callback) callback(new Error(state.errorMessage));
                            return;
                        }
                        state.challenge = res.data;
                        state.status = 'modal_open';
                        emit();
                        if (callback) callback(null, res.data);
                    })
                    .catch(function (err) {
                        state.status = 'unverified';
                        state.errorMessage = 'Network error fetching CAPTCHA.';
                        emit();
                        if (callback) callback(err);
                    });
            },
            toggleIndex: function (idx) {
                if (state.selectedIndexes.has(idx)) {
                    state.selectedIndexes.delete(idx);
                } else {
                    state.selectedIndexes.add(idx);
                }
                emit();
            },
            toggleAudioMode: function () {
                state.isAudioMode = !state.isAudioMode;
                state.errorMessage = null;
                emit();
            },
            verify: function (audioAnswer, callback) {
                if (!state.challenge) return;
                var payload = {
                    challengeId: state.challenge.challengeId
                };

                if (state.isAudioMode) {
                    if (!audioAnswer) {
                        state.errorMessage = 'Please enter the audio digits you hear.';
                        emit();
                        return;
                    }
                    payload.audioAnswer = audioAnswer;
                } else {
                    payload.selectedIndexes = Array.from(state.selectedIndexes);
                }

                fetch('/api/captcha/verify', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                })
                    .then(function (res) {
                        return res.json().then(function (data) {
                            return { ok: res.ok, data: data };
                        });
                    })
                    .then(function (res) {
                        if (res.ok && res.data.success) {
                            state.status = 'verified';
                            state.token = res.data.token || res.data.captchaToken;
                            state.errorMessage = null;
                            emit();
                            if (callback) callback(null, state.token);
                        } else {
                            state.errorMessage = res.data.message || 'Verification failed. Try again.';
                            emit();
                            if (callback) callback(new Error(state.errorMessage));
                        }
                    })
                    .catch(function (err) {
                        state.errorMessage = 'Network error during verification.';
                        emit();
                        if (callback) callback(err);
                    });
            }
        };
    }

    /**
     * CaptchaModal: Renders the 3x3/4x4 dialog with keyboard trap & audio mode
     */
    function CaptchaModal(controller, options) {
        options = options || {};
        var overlay = document.createElement('div');
        overlay.className = 'fc-modal-overlay hidden';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.setAttribute('aria-labelledby', 'fc-modal-target');

        overlay.innerHTML =
            '<div class="fc-modal-card">' +
                '<div class="fc-modal-header">' +
                    '<div class="fc-instruction-prefix">Select all images with</div>' +
                    '<div class="fc-instruction-target" id="fc-modal-target">...</div>' +
                    '<div class="fc-instruction-hint">Click verify once none are left, or switch to audio alternative.</div>' +
                '</div>' +
                '<div class="fc-alert-banner" id="fc-alert-banner"></div>' +
                '<div class="fc-grid-container" id="fc-grid-wrap">' +
                    '<div class="fc-grid grid-3x3" id="fc-grid" role="group" aria-label="Image selection grid"></div>' +
                '</div>' +
                '<div class="fc-audio-view" id="fc-audio-wrap">' +
                    '<p style="font-size:0.86rem; color:var(--muted); text-align:center;">Press play to hear the 4 acoustic numbers:</p>' +
                    '<button type="button" class="fc-audio-btn" id="fc-play-audio-btn">' +
                        ICONS.play + '<span>Play Audio</span>' +
                    '</button>' +
                    '<audio id="fc-audio-elem" preload="none"></audio>' +
                    '<input type="text" class="fc-audio-input" id="fc-audio-input" placeholder="____" maxlength="4" autocomplete="off" aria-label="Enter the 4 numbers you heard">' +
                '</div>' +
                '<div class="fc-modal-footer">' +
                    '<div class="fc-tools-left">' +
                        '<button type="button" class="fc-tool-btn" id="fc-refresh-btn" title="Refresh challenge" aria-label="Get a new challenge">' + ICONS.refresh + '</button>' +
                        '<button type="button" class="fc-tool-btn" id="fc-audio-toggle-btn" title="Audio alternative" aria-label="Switch to audio alternative">' + ICONS.audio + '</button>' +
                        '<button type="button" class="fc-tool-btn" id="fc-info-btn" title="FoodCheck Security" aria-label="Information">' + ICONS.info + '</button>' +
                    '</div>' +
                    '<button type="button" class="fc-verify-btn" id="fc-verify-btn">Verify</button>' +
                '</div>' +
            '</div>';

        document.body.appendChild(overlay);

        var targetElem = overlay.querySelector('#fc-modal-target');
        var alertBanner = overlay.querySelector('#fc-alert-banner');
        var gridWrap = overlay.querySelector('#fc-grid-wrap');
        var gridElem = overlay.querySelector('#fc-grid');
        var audioWrap = overlay.querySelector('#fc-audio-wrap');
        var audioElem = overlay.querySelector('#fc-audio-elem');
        var audioInput = overlay.querySelector('#fc-audio-input');
        var playAudioBtn = overlay.querySelector('#fc-play-audio-btn');
        var refreshBtn = overlay.querySelector('#fc-refresh-btn');
        var audioToggleBtn = overlay.querySelector('#fc-audio-toggle-btn');
        var verifyBtn = overlay.querySelector('#fc-verify-btn');

        function openModal() {
            overlay.classList.remove('hidden');
            if (!controller.getState().challenge) {
                controller.fetchChallenge();
            }
        }

        function closeModal() {
            overlay.classList.add('hidden');
            if (audioElem) {
                audioElem.pause();
                audioElem.currentTime = 0;
            }
        }

        // Close on escape or clicking outside card
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) closeModal();
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && !overlay.classList.contains('hidden')) {
                closeModal();
            }
        });

        // Refresh Challenge
        refreshBtn.addEventListener('click', function () {
            controller.fetchChallenge();
        });

        // Switch to/from Audio mode
        audioToggleBtn.addEventListener('click', function () {
            controller.toggleAudioMode();
        });

        // Audio Play
        playAudioBtn.addEventListener('click', function () {
            var challenge = controller.getState().challenge;
            if (!challenge) return;
            audioElem.src = '/api/captcha/audio/' + encodeURIComponent(challenge.challengeId) + '?t=' + Date.now();
            audioElem.play().catch(function (err) { console.error('Audio play error:', err); });
        });

        // Verify button
        verifyBtn.addEventListener('click', function () {
            verifyBtn.disabled = true;
            verifyBtn.textContent = 'Verifying...';

            var audioAnswer = audioInput.value.trim();
            controller.verify(audioAnswer, function (err, token) {
                verifyBtn.disabled = false;
                verifyBtn.textContent = 'Verify';
                if (!err && token) {
                    closeModal();
                    if (options.onVerified) options.onVerified(token);
                } else {
                    // Auto-load fresh challenge on wrong answer
                    setTimeout(function () {
                        controller.fetchChallenge();
                    }, 900);
                }
            });
        });

        // Subscribe to controller state changes
        controller.subscribe(function (state) {
            if (state.status === 'modal_open') {
                overlay.classList.remove('hidden');
                if (state.challenge) {
                    targetElem.textContent = state.challenge.label || 'items';
                    // Render Grid
                    renderGrid(state.challenge, state.selectedIndexes);
                }
            } else if (state.status === 'verified') {
                closeModal();
            }

            // Audio Mode UI toggle
            if (state.isAudioMode) {
                gridWrap.style.display = 'none';
                audioWrap.classList.add('active');
                audioToggleBtn.innerHTML = ICONS.image;
                audioToggleBtn.setAttribute('title', 'Switch to image challenge');
                audioToggleBtn.setAttribute('aria-label', 'Switch to image challenge');
                audioInput.focus();
            } else {
                gridWrap.style.display = 'block';
                audioWrap.classList.remove('active');
                audioToggleBtn.innerHTML = ICONS.audio;
                audioToggleBtn.setAttribute('title', 'Switch to audio challenge');
                audioToggleBtn.setAttribute('aria-label', 'Switch to audio alternative');
            }

            // Error banner
            if (state.errorMessage) {
                alertBanner.textContent = state.errorMessage;
                alertBanner.classList.add('visible');
            } else {
                alertBanner.classList.remove('visible');
                alertBanner.textContent = '';
            }
        });

        function renderGrid(challenge, selectedSet) {
            gridElem.innerHTML = '';
            var size = challenge.gridSize || 3;
            gridElem.className = 'fc-grid grid-' + size + 'x' + size;

            var images = challenge.images || [];
            images.forEach(function (url, idx) {
                var tile = document.createElement('div');
                tile.className = 'fc-tile' + (selectedSet.has(idx) ? ' selected' : '');
                tile.setAttribute('role', 'checkbox');
                tile.setAttribute('tabindex', '0');
                tile.setAttribute('aria-checked', selectedSet.has(idx) ? 'true' : 'false');
                tile.setAttribute('aria-label', 'Image ' + (idx + 1) + ' of ' + images.length);

                tile.innerHTML =
                    '<img src="' + url + '" alt="Challenge Image ' + (idx + 1) + '" loading="eager">' +
                    '<div class="fc-tile-check">' + ICONS.check + '</div>';

                // Click toggle
                tile.addEventListener('click', function () {
                    controller.toggleIndex(idx);
                });

                // Keyboard Space/Enter toggle
                tile.addEventListener('keydown', function (e) {
                    if (e.key === ' ' || e.key === 'Enter') {
                        e.preventDefault();
                        controller.toggleIndex(idx);
                    }
                });

                gridElem.appendChild(tile);
            });
        }

        return {
            open: openModal,
            close: closeModal
        };
    }

    /**
     * CaptchaCheckbox: The classic "[ ] I'm not a robot" interactive box
     */
    function CaptchaCheckbox(container, controller, modal) {
        var widget = document.createElement('div');
        widget.className = 'fc-captcha-widget';
        widget.innerHTML =
            '<div class="fc-captcha-left" tabindex="0" role="button" aria-label="I\'m not a robot verification checkbox">' +
                '<div class="fc-checkbox-box" id="fc-chk-box">' +
                '</div>' +
                '<div class="fc-captcha-label" id="fc-chk-label">I\'m not a robot</div>' +
            '</div>' +
            '<div class="fc-captcha-badge">' +
                ICONS.shield +
                '<span class="fc-badge-text">FoodCheck</span>' +
            '</div>';

        container.appendChild(widget);

        var chkBox = widget.querySelector('#fc-chk-box');
        var chkLabel = widget.querySelector('#fc-chk-label');
        var clickTarget = widget.querySelector('.fc-captcha-left');

        function trigger() {
            var state = controller.getState();
            if (state.status === 'verified') return;
            modal.open();
        }

        clickTarget.addEventListener('click', trigger);
        clickTarget.addEventListener('keydown', function (e) {
            if (e.key === ' ' || e.key === 'Enter') {
                e.preventDefault();
                trigger();
            }
        });

        controller.subscribe(function (state) {
            chkBox.className = 'fc-checkbox-box';
            if (state.status === 'loading') {
                chkBox.classList.add('loading');
                chkLabel.textContent = 'Verifying...';
            } else if (state.status === 'verified') {
                chkBox.classList.add('verified');
                chkBox.innerHTML = ICONS.check;
                chkLabel.textContent = 'Verified';
            } else {
                chkBox.innerHTML = '';
                chkLabel.textContent = "I'm not a robot";
            }
        });
    }

    /**
     * FoodCheckCaptcha: High-level unified orchestrator
     * Config: { containerId, formId, submitBtnId, onVerified }
     */
    var FoodCheckCaptcha = {
        useCaptcha: useCaptcha,
        CaptchaModal: CaptchaModal,
        CaptchaCheckbox: CaptchaCheckbox,

        init: function (options) {
            options = options || {};
            var container = typeof options.containerId === 'string'
                ? document.getElementById(options.containerId)
                : options.container;

            if (!container) return null;

            var form = typeof options.formId === 'string'
                ? document.getElementById(options.formId)
                : options.form;

            var submitBtn = typeof options.submitBtnId === 'string'
                ? document.getElementById(options.submitBtnId)
                : options.submitBtn;

            // Form rendered timestamp for minimum 2-second time check
            var renderedAt = Date.now();

            // Disable submit button by default until verified
            if (submitBtn) {
                submitBtn.disabled = true;
            }

            // Hidden token input inside form
            var tokenInput = document.createElement('input');
            tokenInput.type = 'hidden';
            tokenInput.name = 'captcha_token';
            tokenInput.id = 'fc-token-input-' + Math.random().toString(36).slice(2, 7);
            if (form) form.appendChild(tokenInput);

            function initWidget(cfg) {
                var provider = (cfg && cfg.provider) || 'custom';
                var siteKey = (cfg && cfg.siteKey) || '6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI';

                if (provider === 'recaptcha' && siteKey) {
                    // Render official Google reCAPTCHA v2
                    renderRecaptcha(container, siteKey, function (token) {
                        tokenInput.value = token;
                        if (submitBtn) submitBtn.disabled = false;
                        if (options.onVerified) options.onVerified(token);
                    });
                } else if (provider === 'turnstile' && siteKey) {
                    // Render Cloudflare Turnstile
                    renderTurnstile(container, siteKey, function (token) {
                        tokenInput.value = token;
                        if (submitBtn) submitBtn.disabled = false;
                        if (options.onVerified) options.onVerified(token);
                    });
                } else {
                    // Render Custom 3x3 / 4x4 CAPTCHA
                    renderCustomCaptcha();
                }
            }

            var pageCfg = window.CAPTCHA_CONFIG || window.FOODCHECK_CAPTCHA_CONFIG;
            if (pageCfg && pageCfg.provider) {
                initWidget(pageCfg);
            } else {
                fetch('/api/captcha/config')
                    .then(function (res) { return res.json(); })
                    .then(function (cfg) { initWidget(cfg); })
                    .catch(function () {
                        initWidget({ provider: 'custom' });
                    });
            }

            var customController = null;
            var customModalInstance = null;
            function renderCustomCaptcha() {
                customController = useCaptcha();
                customModalInstance = CaptchaModal(customController, {
                    onVerified: function (token) {
                        tokenInput.value = token;
                        if (submitBtn) submitBtn.disabled = false;
                        if (options.onVerified) options.onVerified(token);
                    }
                });
                CaptchaCheckbox(container, customController, customModalInstance);
            }

            function renderRecaptcha(targetElem, siteKey, callback) {
                if (!targetElem) return;

                var isDark = true;
                try {
                    var themeVal = localStorage.getItem('foodcheck-theme');
                    if (themeVal === 'light') isDark = false;
                } catch (e) {}

                function executeRender() {
                    if (targetElem.getAttribute('data-recaptcha-widget-id')) {
                        return; // already rendered
                    }
                    if (window.grecaptcha && typeof window.grecaptcha.render === 'function') {
                        try {
                            targetElem.innerHTML = '';
                            var widgetId = window.grecaptcha.render(targetElem, {
                                sitekey: siteKey,
                                theme: isDark ? 'dark' : 'light',
                                callback: function (token) {
                                    tokenInput.value = token;
                                    if (submitBtn) submitBtn.disabled = false;
                                    if (options.onVerified) options.onVerified(token);
                                    if (callback) callback(token);
                                },
                                'expired-callback': function () {
                                    tokenInput.value = '';
                                    if (submitBtn) submitBtn.disabled = true;
                                    if (options.onExpired) options.onExpired();
                                }
                            });
                            targetElem.setAttribute('data-recaptcha-widget-id', String(widgetId));
                            window._lastRecaptchaWidgetId = widgetId;
                        } catch (err) {
                            console.warn('reCAPTCHA render notice:', err);
                        }
                    } else {
                        window._recaptchaQueue = window._recaptchaQueue || [];
                        window._recaptchaQueue.push(executeRender);
                        if (!targetElem._rcTimer) {
                            targetElem._rcTimer = setInterval(function () {
                                if (window.grecaptcha && typeof window.grecaptcha.render === 'function') {
                                    clearInterval(targetElem._rcTimer);
                                    targetElem._rcTimer = null;
                                    executeRender();
                                }
                            }, 100);
                        }
                    }
                }

                if (window.grecaptcha && typeof window.grecaptcha.ready === 'function') {
                    window.grecaptcha.ready(executeRender);
                } else {
                    executeRender();
                }
            }

            function renderTurnstile(targetElem, siteKey, callback) {
                if (window.turnstile && window.turnstile.render) {
                    window.turnstile.render(targetElem, {
                        sitekey: siteKey,
                        callback: callback
                    });
                } else {
                    targetElem.innerHTML = '<div class="cf-turnstile" data-sitekey="' + siteKey + '"></div>';
                }
            }

            return {
                getToken: function () { return tokenInput.value; },
                openModal: function () {
                    if (customModalInstance) customModalInstance.open();
                },
                reset: function () {
                    if (customController) customController.reset();
                    tokenInput.value = '';
                    if (submitBtn) submitBtn.disabled = true;
                }
            };
        }
    };

    window._recaptchaQueue = window._recaptchaQueue || [];
    window.onGoogleRecaptchaLoad = window.onRecaptchaLoaded = function () {
        if (window._recaptchaQueue) {
            while (window._recaptchaQueue.length > 0) {
                var fn = window._recaptchaQueue.shift();
                try { fn(); } catch (e) { console.error(e); }
            }
        }
    };

    window.FoodCheckCaptcha = FoodCheckCaptcha;

})(window, document);
