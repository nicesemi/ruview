// Quick Settings Panel - Centralized configuration for all UI features
// Accessible via gear icon in header

import { apiService, API_TOKEN_STORAGE_KEY } from '../services/api.service.js';

export class QuickSettings {
  constructor(app) {
    this.app = app;
    this.button = null;
    this.panel = null;
    this.isOpen = false;
  }

  // A stored token is applied at api.service.js module load (before any
  // request fires) — this panel only saves/clears it.
  init() {
    this.createButton();
    this.createPanel();
  }

  createButton() {
    this.button = document.createElement('button');
    this.button.className = 'settings-gear';
    this.button.setAttribute('aria-label', '设置');
    this.button.setAttribute('title', '快捷设置');
    this.button.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`;

    this.button.addEventListener('click', () => this.toggle());

    const headerInfo = document.querySelector('.header-info');
    if (headerInfo) headerInfo.appendChild(this.button);
  }

  createPanel() {
    this.panel = document.createElement('div');
    this.panel.className = 'quick-settings-panel';
    this.panel.setAttribute('role', 'dialog');
    this.panel.setAttribute('aria-label', '快捷设置');

    this.panel.innerHTML = `
      <div class="qs-header">
        <h3>设置</h3>
        <button class="qs-close" aria-label="Close">&times;</button>
      </div>
      <div class="qs-body">
        <div class="qs-section">
          <div class="qs-section-title">显示</div>
          <label class="qs-toggle">
            <span>减少动效</span>
            <input type="checkbox" id="qs-reduced-motion" ${this.prefersReducedMotion() ? 'checked' : ''}>
            <span class="qs-switch"></span>
          </label>
          <label class="qs-toggle">
            <span>高对比度</span>
            <input type="checkbox" id="qs-high-contrast">
            <span class="qs-switch"></span>
          </label>
          <label class="qs-toggle">
            <span>紧凑模式</span>
            <input type="checkbox" id="qs-compact" ${this.getSetting('compact') ? 'checked' : ''}>
            <span class="qs-switch"></span>
          </label>
        </div>
        <div class="qs-section">
          <div class="qs-section-title">监控</div>
          <label class="qs-toggle">
            <span>健康状态轮询</span>
            <input type="checkbox" id="qs-health-polling" checked>
            <span class="qs-switch"></span>
          </label>
          <label class="qs-toggle">
            <span>自动重连</span>
            <input type="checkbox" id="qs-auto-reconnect" checked>
            <span class="qs-switch"></span>
          </label>
        </div>
        <div class="qs-section">
          <div class="qs-section-title">Cognitum 账户</div>
          <div class="qs-row" style="flex-direction: column; align-items: stretch; gap: 6px;">
            <span id="qs-signin-status" style="font-size: 0.9em; opacity: 0.85;">检查中…</span>
            <div style="display: flex; gap: 8px;">
              <button class="qs-btn" id="qs-signin" hidden>使用 Cognitum 登录</button>
              <button class="qs-btn-danger" id="qs-signout" hidden>退出登录</button>
            </div>
          </div>
        </div>
        <div class="qs-section">
          <div class="qs-section-title">API 访问</div>
          <div class="qs-row" style="flex-direction: column; align-items: stretch; gap: 6px;">
            <span>Bearer 令牌（仅当服务端启用 RUVIEW_API_TOKEN 时填写）</span>
            <input type="password" id="qs-api-token" class="qs-text-input" placeholder="粘贴令牌…" autocomplete="off" style="width: 100%; box-sizing: border-box;">
            <div style="display: flex; gap: 8px;">
              <button class="qs-btn" id="qs-api-token-save">保存并应用</button>
              <button class="qs-btn-danger" id="qs-api-token-clear">清空</button>
            </div>
            <span id="qs-api-token-status" style="font-size: 0.85em; opacity: 0.75;"></span>
          </div>
        </div>
        <div class="qs-section">
          <div class="qs-section-title">数据</div>
          <div class="qs-row">
            <span>清除本地数据</span>
            <button class="qs-btn-danger" id="qs-clear-data">清空</button>
          </div>
          <div class="qs-row">
            <span>重置引导</span>
            <button class="qs-btn" id="qs-reset-tour">重置</button>
          </div>
        </div>
      </div>
    `;

    // Bind events
    this.panel.querySelector('.qs-close').addEventListener('click', () => this.close());

    // Re-check sign-in state whenever the page could be showing a stale view:
    // `pageshow` fires on a back/forward-cache restore (where no script re-runs
    // and no fetch would otherwise happen), and `visibilitychange` covers
    // signing in or out in another tab. Opening the panel alone is not enough —
    // the panel may already be open, or the page may be restored wholesale.
    window.addEventListener('pageshow', () => { void refreshSignInPanel(this.panel); });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) void refreshSignInPanel(this.panel);
    });

    // ADR-271 sign-in. Bound here as well as in refreshSignInPanel so a click
    // works even if the status fetch has not resolved yet.
    this.panel.querySelector('#qs-signin')
      .addEventListener('click', () => { window.location.href = '/oauth/start'; });
    this.panel.querySelector('#qs-signout')
      .addEventListener('click', () => { window.location.href = '/oauth/logout'; });

    this.panel.querySelector('#qs-reduced-motion').addEventListener('change', (e) => {
      document.body.classList.toggle('reduced-motion', e.target.checked);
      this.saveSetting('reduced-motion', e.target.checked);
    });

    this.panel.querySelector('#qs-high-contrast').addEventListener('change', (e) => {
      document.body.classList.toggle('high-contrast', e.target.checked);
      this.saveSetting('high-contrast', e.target.checked);
    });

    this.panel.querySelector('#qs-compact').addEventListener('change', (e) => {
      document.body.classList.toggle('compact-mode', e.target.checked);
      this.saveSetting('compact', e.target.checked);
    });

    this.panel.querySelector('#qs-health-polling').addEventListener('change', (e) => {
      const healthService = this.app?.components?.dashboard?.healthSubscription;
      if (e.target.checked) {
        // Resume would need import - just dispatch event
        document.dispatchEvent(new CustomEvent('health-polling-toggle', { detail: true }));
      } else {
        document.dispatchEvent(new CustomEvent('health-polling-toggle', { detail: false }));
      }
    });

    this.panel.querySelector('#qs-api-token-save').addEventListener('click', () => {
      const input = this.panel.querySelector('#qs-api-token');
      const status = this.panel.querySelector('#qs-api-token-status');
      const token = input.value.trim();
      if (!token) {
        status.textContent = '请先输入令牌，或点击清除以移除。';
        return;
      }
      try { localStorage.setItem(API_TOKEN_STORAGE_KEY, token); } catch { /* noop */ }
      apiService.setAuthToken(token);
      status.textContent = '令牌已保存并生效，正在刷新…';
      setTimeout(() => window.location.reload(), 600);
    });

    this.panel.querySelector('#qs-api-token-clear').addEventListener('click', () => {
      const input = this.panel.querySelector('#qs-api-token');
      const status = this.panel.querySelector('#qs-api-token-status');
      try { localStorage.removeItem(API_TOKEN_STORAGE_KEY); } catch { /* noop */ }
      apiService.setAuthToken(null);
      input.value = '';
      status.textContent = '令牌已清除，正在刷新…';
      setTimeout(() => window.location.reload(), 600);
    });

    this.panel.querySelector('#qs-clear-data').addEventListener('click', () => {
      try {
        localStorage.clear();
        sessionStorage.clear();
      } catch { /* noop */ }
      this.close();
      window.location.reload();
    });

    this.panel.querySelector('#qs-reset-tour').addEventListener('click', () => {
      try { localStorage.removeItem('ruview-onboarding-done'); } catch { /* noop */ }
      this.close();
      document.dispatchEvent(new CustomEvent('start-onboarding'));
    });

    document.body.appendChild(this.panel);

    // #2099: the dashboard asks for this when the server refused the stream
    // for lack of a valid token.
    document.addEventListener('ruview-open-api-access', (e) => this.openApiAccess(e.detail?.reason));

    // Close on outside click
    document.addEventListener('click', (e) => {
      if (this.isOpen && !this.panel.contains(e.target) && !this.button.contains(e.target)) {
        this.close();
      }
    });

    // Apply saved settings on init
    this.applySavedSettings();
  }

  applySavedSettings() {
    if (this.getSetting('reduced-motion') || this.prefersReducedMotion()) {
      document.body.classList.add('reduced-motion');
      const cb = this.panel.querySelector('#qs-reduced-motion');
      if (cb) cb.checked = true;
    }
    if (this.getSetting('high-contrast')) {
      document.body.classList.add('high-contrast');
      const cb = this.panel.querySelector('#qs-high-contrast');
      if (cb) cb.checked = true;
    }
    if (this.getSetting('compact')) {
      document.body.classList.add('compact-mode');
    }
    const status = this.panel.querySelector('#qs-api-token-status');
    let hasToken = false;
    try { hasToken = !!localStorage.getItem(API_TOKEN_STORAGE_KEY); } catch { /* noop */ }
    if (status) status.textContent = hasToken ? '当前已设置令牌。' : '未设置令牌（未启用鉴权或无需鉴权）。';
  }

  prefersReducedMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  toggle() {
    this.isOpen ? this.close() : this.open();
  }

  open() {
    this.isOpen = true;
    this.panel.classList.add('open');
    // Refresh on every open, not once at construction: the session may have
    // been established in another tab, or expired since the page loaded.
    // Fire-and-forget — a failure renders as a message in the panel, and must
    // not stop the panel opening.
    void refreshSignInPanel(this.panel);
  }

  /** Open on the API Access section and say why (#2099). */
  openApiAccess(reason) {
    this.open();
    const status = this.panel.querySelector('#qs-api-token-status');
    if (status) {
      status.textContent = reason === 'rejected'
        ? '服务器拒绝了已保存的令牌。请粘贴当前 RUVIEW_API_TOKEN 后点击保存并应用。'
        : '该服务器需要 API 令牌。请粘贴 RUVIEW_API_TOKEN 后点击保存并应用。';
    }
    const input = this.panel.querySelector('#qs-api-token');
    if (input) {
      input.scrollIntoView?.({ block: 'center' });
      input.focus();
    }
  }

  close() {
    this.isOpen = false;
    this.panel.classList.remove('open');
  }

  getSetting(key) {
    try { return JSON.parse(localStorage.getItem(`ruview-setting-${key}`)); }
    catch { return null; }
  }

  saveSetting(key, value) {
    try { localStorage.setItem(`ruview-setting-${key}`, JSON.stringify(value)); }
    catch { /* noop */ }
  }

  dispose() {
    this.button?.remove();
    this.panel?.remove();
  }
}

// ---- Cognitum browser sign-in (ADR-271) -------------------------------------
//
// `/oauth/status` is intentionally UNGATED: a signed-out browser cannot ask a
// gated endpoint whether sign-in is available. It returns capability flags and,
// when a session exists, who it belongs to — never a credential.
//
// Sign-in is a full-page navigation, not fetch(): the server replies 302 to
// auth.cognitum.one, and the browser must follow it and carry the transaction
// cookie. An XHR would follow the redirect invisibly and land nowhere useful.
export async function refreshSignInPanel(root = document) {
  const status = root.querySelector('#qs-signin-status');
  const signIn = root.querySelector('#qs-signin');
  const signOut = root.querySelector('#qs-signout');
  if (!status || !signIn || !signOut) return null;

  let info;
  try {
    const resp = await fetch('/oauth/status', { credentials: 'same-origin' });
    // 404 = a server predating ADR-271. Say so plainly rather than offering a
    // button that will 404.
    if (resp.status === 404) {
      status.textContent = '该服务器不支持 Cognitum 登录。';
      signIn.hidden = true;
      signOut.hidden = true;
      return null;
    }
    if (!resp.ok) throw new Error(`status ${resp.status}`);
    info = await resp.json();
  } catch (err) {
    status.textContent = `Could not reach the server (${err.message}).`;
    signIn.hidden = true;
    signOut.hidden = true;
    return null;
  }

  if (info.signed_in) {
    status.textContent = `Signed in${info.account ? ` as ${info.account}` : ''}${
      info.scope ? ` - ${info.scope}` : ''
    }`;
    signIn.hidden = true;
    signOut.hidden = false;
  } else if (info.browser_signin) {
    status.textContent = info.auth_required
      ? '该服务器要求登录。'
      : '可选：登录后可使用 Cognitum 账号。';
    signIn.hidden = false;
    signOut.hidden = true;
  } else if (info.auth_required) {
    // Auth is on but OAuth is not — the static-token panel below is the path.
    status.textContent = '该服务器使用共享 API 令牌（见下方“API 访问”）。';
    signIn.hidden = true;
    signOut.hidden = true;
  } else {
    status.textContent = '该服务器无需登录。';
    signIn.hidden = true;
    signOut.hidden = true;
  }

  signIn.onclick = () => { window.location.href = '/oauth/start'; };
  signOut.onclick = () => { window.location.href = '/oauth/logout'; };
  return info;
}
