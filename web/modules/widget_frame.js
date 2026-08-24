/* Framed widget bootstrap scripts. The parent remains the route and lifecycle owner. */

export function moduleBridgeScript(nonce) {
    return `
        (() => {
            const nonce = ${JSON.stringify(nonce)};
            let seq = 0;
            let disposed = false;
            const pending = new Map();
            const cleanup = new Set();
            const onDispose = (fn) => {
                if (typeof fn !== 'function') return;
                if (disposed) fn();
                else cleanup.add(fn);
            };
            const dispose = () => {
                if (disposed) return;
                disposed = true;
                pending.forEach(({ reject }) => reject(new Error('widget disposed')));
                pending.clear();
                cleanup.forEach((fn) => { try { fn(); } catch {} });
                cleanup.clear();
                window.removeEventListener('message', onMessage);
            };
            const onMessage = (event) => {
                if (event.source !== window.parent) return;
                const msg = event.data || {};
                if (msg.nonce !== nonce) return;
                if (msg.type === 'ouro-widget-dispose') {
                    dispose();
                    return;
                }
                if (msg.type !== 'ouro-widget-fetch-result' || disposed) return;
                const item = pending.get(msg.id);
                if (!item) return;
                pending.delete(msg.id);
                if (msg.error) {
                    item.reject(new Error(msg.error));
                    return;
                }
                item.resolve(new Response(msg.body || '', {
                    status: msg.status || 200,
                    headers: msg.headers || {},
                }));
            };
            window.addEventListener('message', onMessage);
            window.__ouroWidgetOnDispose = onDispose;
            window.fetch = (url, init = {}) => {
                const id = ++seq;
                return new Promise((resolve, reject) => {
                    if (disposed) {
                        reject(new Error('widget disposed'));
                        return;
                    }
                    pending.set(id, { resolve, reject });
                    window.parent.postMessage({
                        type: 'ouro-widget-fetch',
                        nonce,
                        id,
                        url: String(url || ''),
                        init: {
                            method: init.method || 'GET',
                            headers: init.headers || {},
                            body: init.body || null,
                        },
                    }, '*');
                });
            };
            window.OuroborosWidget = { fetch: window.fetch };
        })();
    `;
}

export function moduleResizeScript(nonce, frameFloor, maxHeight, borderReserve) {
    return `
        (() => {
            const root = document.getElementById('root');
            const verticalOverflowStyle = document.createElement('style');
            verticalOverflowStyle.textContent = 'html, body { overflow-y: hidden !important; }';
            let suppressingVerticalOverflow = false;
            let lastHeight = 0;
            const setVerticalOverflowSuppressed = (suppressed) => {
                if (suppressed === suppressingVerticalOverflow) return;
                suppressingVerticalOverflow = suppressed;
                if (suppressed) document.head.appendChild(verticalOverflowStyle);
                else verticalOverflowStyle.remove();
            };
            setVerticalOverflowSuppressed(true);
            const report = () => {
                if (!root) return;
                const box = root.getBoundingClientRect();
                const body = document.body;
                const bodyTop = body?.getBoundingClientRect().top || 0;
                // The root's bottom edge captures collapsed child margins; body
                // bottom padding and border complete the measured body box. This
                // also avoids treating a fixed 100vh body as small-module content.
                const bodyStyle = body ? getComputedStyle(body) : null;
                const paddingBottom = Number.parseFloat(bodyStyle?.paddingBottom);
                const borderBottom = Number.parseFloat(bodyStyle?.borderBottomWidth);
                const bodyBottomSpacing = Math.max(0,
                    (Number.isFinite(paddingBottom) ? paddingBottom : 0)
                    + (Number.isFinite(borderBottom) ? borderBottom : 0));
                const bodyHeight = body?.scrollHeight || 0;
                const bodyClientHeight = body?.clientHeight || 0;
                const fixedViewportBody = bodyStyle
                    && Math.abs((parseFloat(bodyStyle.height) || 0) - window.innerHeight) <= 1;
                const bodyContentHeight = !fixedViewportBody || bodyHeight > bodyClientHeight + 1
                    ? bodyHeight
                    : 0;
                const contentHeight = Math.max(
                    root.scrollHeight,
                    box.height,
                    box.bottom - bodyTop + bodyBottomSpacing,
                    bodyContentHeight,
                );
                const height = Math.ceil(contentHeight);
                const outerHeight = Math.min(
                    ${JSON.stringify(maxHeight)},
                    Math.max(
                        ${JSON.stringify(frameFloor)},
                        height + ${JSON.stringify(borderReserve)},
                    ),
                );
                setVerticalOverflowSuppressed(outerHeight < ${JSON.stringify(maxHeight)});
                if (!height || height === lastHeight) return;
                lastHeight = height;
                window.parent.postMessage({
                    type: 'ouro-widget-resize',
                    nonce: ${JSON.stringify(nonce)},
                    height,
                }, '*');
            };
            const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(report) : null;
            if (observer && root) observer.observe(root);
            const onLoad = () => report();
            window.addEventListener('load', onLoad, { once: true });
            window.__ouroWidgetOnDispose?.(() => {
                observer?.disconnect();
                window.removeEventListener('load', onLoad);
                setVerticalOverflowSuppressed(false);
            });
            report();
        })();
    `;
}
