(function (global) {
    const defaultStateMap = {
        idle: { border: '#ccd6f6', bg: 'rgba(255,255,255,0.92)', color: '#3b4aa1', label: 'API endpoint' },
        ok: { border: '#b7dfc8', bg: 'rgba(235, 252, 241, 0.96)', color: '#18794e', label: 'API connected' },
        warn: { border: '#f2d3a2', bg: 'rgba(255, 247, 233, 0.96)', color: '#9a5f0a', label: 'API warning' },
        error: { border: '#f3b3b3', bg: 'rgba(255, 236, 236, 0.96)', color: '#b42318', label: 'API error' },
    };

    function createRoundedButton() {
        const button = document.createElement('button');
        button.type = 'button';
        button.style.border = '1px solid #ccd6f6';
        button.style.borderRadius = '999px';
        button.style.background = 'rgba(255,255,255,0.92)';
        button.style.color = '#3b4aa1';
        button.style.boxShadow = '0 2px 8px rgba(0,0,0,0.12)';
        button.style.cursor = 'pointer';
        return button;
    }

    function copyText(text) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            return navigator.clipboard.writeText(text);
        }

        return new Promise((resolve, reject) => {
            try {
                const temp = document.createElement('textarea');
                temp.value = text;
                temp.setAttribute('readonly', 'readonly');
                temp.style.position = 'absolute';
                temp.style.left = '-9999px';
                document.body.appendChild(temp);
                temp.select();
                document.execCommand('copy');
                temp.remove();
                resolve();
            } catch (error) {
                reject(error);
            }
        });
    }

    function init(apiBaseUrl, options) {
        if (!apiBaseUrl || global.__apiDebugBadgeMounted) {
            return;
        }
        global.__apiDebugBadgeMounted = true;

        const settings = options || {};
        const storageKeyVisible = settings.storageKeyVisible || 'apiDebugBadgeVisible';
        const stateMap = settings.stateMap || defaultStateMap;

        const container = document.createElement('div');
        container.id = 'apiDebugContainer';
        container.style.position = 'fixed';
        container.style.right = '12px';
        container.style.bottom = '12px';
        container.style.zIndex = '9999';
        container.style.display = 'flex';
        container.style.alignItems = 'center';
        container.style.gap = '6px';

        const badge = createRoundedButton();
        badge.id = 'apiDebugBadge';
        badge.textContent = `API endpoint: ${apiBaseUrl}`;
        badge.title = 'Click to copy API endpoint';
        badge.style.padding = '6px 10px';
        badge.style.fontSize = '11px';

        const hideButton = createRoundedButton();
        hideButton.id = 'apiDebugHideBtn';
        hideButton.textContent = 'x';
        hideButton.title = 'Hide API debug badge';
        hideButton.style.width = '24px';
        hideButton.style.height = '24px';
        hideButton.style.fontSize = '12px';

        const showButton = createRoundedButton();
        showButton.id = 'apiDebugShowBtn';
        showButton.textContent = 'API';
        showButton.title = 'Show API debug badge';
        showButton.style.position = 'fixed';
        showButton.style.right = '12px';
        showButton.style.bottom = '12px';
        showButton.style.zIndex = '9999';
        showButton.style.padding = '6px 10px';
        showButton.style.fontSize = '11px';

        function setBadgeVisibility(visible) {
            container.style.display = visible ? 'flex' : 'none';
            showButton.style.display = visible ? 'none' : 'block';
            localStorage.setItem(storageKeyVisible, visible ? 'true' : 'false');
        }

        function setBadgeState(state) {
            const picked = stateMap[state] || stateMap.idle;
            badge.style.borderColor = picked.border;
            badge.style.background = picked.bg;
            badge.style.color = picked.color;
            badge.textContent = `${picked.label}: ${apiBaseUrl}`;
        }

        global.__setApiDebugBadgeState = setBadgeState;

        badge.addEventListener('click', async () => {
            try {
                await copyText(apiBaseUrl);
                const originalText = badge.textContent;
                badge.textContent = 'API endpoint copied';
                setTimeout(() => {
                    badge.textContent = originalText;
                }, 1200);
            } catch (error) {
                console.warn('Failed to copy API endpoint:', error);
            }
        });

        hideButton.addEventListener('click', () => setBadgeVisibility(false));
        showButton.addEventListener('click', () => setBadgeVisibility(true));

        container.appendChild(badge);
        container.appendChild(hideButton);
        document.body.appendChild(container);
        document.body.appendChild(showButton);

        const isVisible = localStorage.getItem(storageKeyVisible) !== 'false';
        setBadgeVisibility(isVisible);
        setBadgeState('idle');

        if (!global.__apiDebugFetchWrapped) {
            const originalFetch = global.fetch.bind(global);
            global.fetch = async (...args) => {
                try {
                    const response = await originalFetch(...args);
                    global.__setApiDebugBadgeState(response.ok ? 'ok' : 'warn');
                    return response;
                } catch (error) {
                    global.__setApiDebugBadgeState('error');
                    throw error;
                }
            };
            global.__apiDebugFetchWrapped = true;
        }
    }

    global.ApiDebugBadge = {
        init,
    };
})(window);
