/* 個別解説（学習者プロファイル・解説生成）画面の共通処理 */
(function (global) {
    function resolveApiBaseUrl() {
        const stored = localStorage.getItem('apiBaseUrl');
        if (stored) {
            return stored.replace(/\/$/, '');
        }
        if (window.location.protocol === 'file:') {
            return 'http://127.0.0.1:5001/api';
        }
        // Flask が配信している画面（/screens/...）なら，起動したポート（5001 が埋まっていて 5002 などでも）の API を使う
        if (window.location.pathname.startsWith('/screens/') && !['5500', '5501'].includes(window.location.port)) {
            return '/api';
        }
        return `${window.location.protocol}//${window.location.hostname}:5001/api`;
    }

    const API_BASE_URL = resolveApiBaseUrl();

    function resolveScreenUrl(screenPath) {
        const normalizedPath = screenPath.startsWith('/') ? screenPath : `/${screenPath}`;
        if (window.location.protocol === 'file:') {
            return `../../${screenPath.replace(/^\/+/, '').replace(/^screens\//, 'screens/')}`;
        }
        return `${window.location.origin}${normalizedPath}`;
    }

    const PAGES = [
        { key: 'exercises', label: '① 演習の反映', path: 'screens/learning/exercises.html' },
        { key: 'profiles', label: '② 学習者プロファイル', path: 'screens/learning/profiles.html' },
        { key: 'problems', label: '③ 定期テスト問題と解説', path: 'screens/learning/problems.html' },
    ];

    const JUDGMENT_LABELS = {
        correct: '正解',
        correct_form_issue: '正解（書き方に問題）',
        wrong: '誤答',
        incomplete: '不完全',
        form_issue: '書き方の問題',
        blank: '空欄',
        unreadable: '判読不能',
        unmarked: '判定なし',
    };

    const ELEMENT_LABELS = {
        result: '結果と公式',
        reasoning: '途中式を省略しない',
        definition_review: '定義の復習',
        similar_check: '類題',
        error_caution: '誤りへの注意',
    };

    function escapeHtml(value) {
        if (value === null || value === undefined) {
            return '';
        }
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    async function api(method, path, body) {
        const options = { method, credentials: 'include', headers: {} };
        if (body !== undefined) {
            options.headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(body);
        }
        let response;
        try {
            response = await fetch(`${API_BASE_URL}${path}`, options);
        } catch (error) {
            throw new Error('サーバーに接続できませんでした。バックエンドが起動しているか確認してください。');
        }
        let payload = {};
        try {
            payload = await response.json();
        } catch (error) {
            payload = {};
        }
        if (response.status === 401) {
            window.location.href = resolveScreenUrl('screens/login/login.html');
            throw new Error('ログインが必要です');
        }
        if (!response.ok || payload.success === false) {
            const message = payload.message || payload.error || `エラーが発生しました（${response.status}）`;
            const error = new Error(message);
            error.status = response.status;
            error.payload = payload;
            throw error;
        }
        return payload;
    }

    async function requireLogin() {
        const session = await api('GET', '/auth/session');
        if (!session.authenticated) {
            window.location.href = resolveScreenUrl('screens/login/login.html');
            throw new Error('ログインが必要です');
        }
        return session;
    }

    function renderHeader(activeKey, title, description) {
        const nav = PAGES.map((page) => (
            `<a class="nav-link${page.key === activeKey ? ' active' : ''}" href="${resolveScreenUrl(page.path)}">${page.label}</a>`
        )).join('');
        const header = document.createElement('header');
        header.className = 'page-header';
        header.innerHTML = `
            <div class="page-header-top">
                <div>
                    <h1>${escapeHtml(title)}</h1>
                    <p class="page-description">${escapeHtml(description)}</p>
                </div>
                <div class="header-actions">
                    <span class="teacher-badge" id="teacherBadge"></span>
                    <a class="button button-light" href="${resolveScreenUrl('screens/home/home.html')}">ホームへ</a>
                </div>
            </div>
            <nav class="page-nav">${nav}</nav>`;
        document.body.prepend(header);
    }

    let toastTimer = null;
    function toast(message, type) {
        let element = document.getElementById('toast');
        if (!element) {
            element = document.createElement('div');
            element.id = 'toast';
            document.body.appendChild(element);
        }
        element.className = `toast toast-${type || 'info'} visible`;
        element.textContent = message;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => element.classList.remove('visible'), type === 'error' ? 7000 : 3500);
    }

    let graphCache = null;
    async function loadGraph() {
        if (!graphCache) {
            graphCache = await api('GET', '/learning/graph');
        }
        return graphCache;
    }

    function nodeLabelMap(graphPayload) {
        const map = {};
        Object.values(graphPayload.graph.units).forEach((unit) => {
            unit.nodes.forEach((node) => { map[node.id] = node.label; });
        });
        return map;
    }

    function confidenceBar(value, threshold) {
        if (value === null || value === undefined) {
            return '<span class="muted">記録なし（0.5）</span>';
        }
        const percent = Math.max(0, Math.min(100, Math.round(value * 100)));
        const weak = value < (threshold || 0.5);
        return `<div class="confidence">
            <div class="confidence-track"><div class="confidence-fill${weak ? ' weak' : ''}" style="width:${percent}%"></div>
            <div class="confidence-threshold" style="left:${(threshold || 0.5) * 100}%"></div></div>
            <span class="confidence-value${weak ? ' weak' : ''}">${Number(value).toFixed(2)}</span></div>`;
    }

    function judgmentBadge(judgment) {
        return `<span class="badge badge-${escapeHtml(judgment)}">${escapeHtml(JUDGMENT_LABELS[judgment] || judgment)}</span>`;
    }

    function formatDateTime(value) {
        if (!value) {
            return '—';
        }
        const date = new Date(value.endsWith('Z') || value.includes('+') ? value : `${value}Z`);
        if (Number.isNaN(date.getTime())) {
            return value;
        }
        return date.toLocaleString('ja-JP', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
    }

    /* 解説の Markdown（見出し・段落・箇条書き・太字）を HTML にする。数式（$...$）はそのまま残して MathJax で表示する。 */
    function renderMarkdown(markdown) {
        const lines = escapeHtml(markdown || '').split('\n');
        const html = [];
        let paragraph = [];
        let list = [];
        const inline = (text) => text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        const flush = () => {
            if (paragraph.length) {
                html.push(`<p>${inline(paragraph.join('<br>'))}</p>`);
                paragraph = [];
            }
            if (list.length) {
                html.push(`<ul>${list.map((item) => `<li>${inline(item)}</li>`).join('')}</ul>`);
                list = [];
            }
        };
        lines.forEach((line) => {
            const heading = line.match(/^(#{1,6})\s+(.*)$/);
            const item = line.match(/^\s*[-・]\s+(.*)$/);
            if (heading) {
                flush();
                const level = Math.min(heading[1].length + 1, 6);
                html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
            } else if (item) {
                if (paragraph.length) {
                    flush();
                }
                list.push(item[1]);
            } else if (!line.trim()) {
                flush();
            } else {
                if (list.length) {
                    flush();
                }
                paragraph.push(line);
            }
        });
        flush();
        return html.join('\n');
    }

    /* MathJax は非同期に読み込むので，読み込みが終わるまで少し待ってから数式を表示する（読めなければ $...$ のまま） */
    function typesetMath(element, retries) {
        const remaining = retries === undefined ? 30 : retries;
        if (global.MathJax && typeof global.MathJax.typesetPromise === 'function') {
            global.MathJax.typesetPromise(element ? [element] : undefined).catch(() => {});
        } else if (remaining > 0) {
            setTimeout(() => typesetMath(element, remaining - 1), 300);
        }
    }

    async function initPage(activeKey, title, description) {
        renderHeader(activeKey, title, description);
        if (global.ApiDebugBadge && typeof global.ApiDebugBadge.init === 'function') {
            global.ApiDebugBadge.init(API_BASE_URL);
        }
        const session = await requireLogin();
        const badge = document.getElementById('teacherBadge');
        if (badge && session.teacher_id) {
            badge.textContent = `ID: ${session.teacher_id}`;
        }
        return session;
    }

    function queryParam(name) {
        return new URLSearchParams(window.location.search).get(name);
    }

    global.Learning = {
        API_BASE_URL,
        JUDGMENT_LABELS,
        ELEMENT_LABELS,
        api,
        initPage,
        resolveScreenUrl,
        escapeHtml,
        toast,
        loadGraph,
        nodeLabelMap,
        confidenceBar,
        judgmentBadge,
        formatDateTime,
        renderMarkdown,
        typesetMath,
        queryParam,
    };
})(window);
