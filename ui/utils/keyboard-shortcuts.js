// Keyboard Shortcuts System
// Press '?' to show help overlay, number keys to switch tabs, etc.

export class KeyboardShortcuts {
  constructor(app) {
    this.app = app;
    this.shortcuts = new Map();
    this.helpVisible = false;
    this.enabled = true;
    this.overlay = null;
    this.registerDefaults();
  }

  registerDefaults() {
    this.register('?', '查看键盘快捷键', () => this.toggleHelp());
    this.register('Escape', '关闭浮层/对话框', () => this.closeAll());
    this.register('1', '切换到仪表盘', () => this.switchTab('dashboard'));
    this.register('2', '切换到硬件', () => this.switchTab('hardware'));
    this.register('3', '切换到实时演示', () => this.switchTab('demo'));
    this.register('4', '切换到系统架构', () => this.switchTab('architecture'));
    this.register('5', '切换到性能分析', () => this.switchTab('performance'));
    this.register('6', '切换到应用场景', () => this.switchTab('applications'));
    this.register('7', '切换到感知', () => this.switchTab('sensing'));
    this.register('8', '切换到模型训练', () => this.switchTab('training'));
    this.register('p', '切换性能监视器', () => this.togglePerfMonitor());
    this.register('t', '切换深色/浅色主题', () => this.toggleTheme());
  }

  register(key, description, handler) {
    this.shortcuts.set(key, { description, handler });
  }

  init() {
    document.addEventListener('keydown', (e) => this.handleKeydown(e));
    this.createOverlay();
  }

  handleKeydown(e) {
    if (!this.enabled) return;

    // Ignore when typing in inputs
    const tag = e.target.tagName.toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) {
      if (e.key === 'Escape') {
        e.target.blur();
      }
      return;
    }

    // Ignore modified keys (except shift for '?')
    if (e.ctrlKey || e.altKey || e.metaKey) return;

    const shortcut = this.shortcuts.get(e.key);
    if (shortcut) {
      e.preventDefault();
      shortcut.handler();
    }
  }

  switchTab(tabId) {
    const tabManager = this.app?.getComponent?.('tabManager');
    if (tabManager) {
      tabManager.switchToTab(tabId);
    }
  }

  togglePerfMonitor() {
    const event = new CustomEvent('toggle-perf-monitor');
    document.dispatchEvent(event);
  }

  toggleTheme() {
    const event = new CustomEvent('toggle-theme');
    document.dispatchEvent(event);
  }

  closeAll() {
    if (this.helpVisible) {
      this.hideHelp();
    }
  }

  createOverlay() {
    this.overlay = document.createElement('div');
    this.overlay.className = 'shortcuts-overlay';
    this.overlay.setAttribute('role', 'dialog');
    this.overlay.setAttribute('aria-label', '键盘快捷键');
    this.overlay.setAttribute('aria-modal', 'true');
    this.overlay.innerHTML = this.buildHelpHTML();
    this.overlay.addEventListener('click', (e) => {
      if (e.target === this.overlay) this.hideHelp();
    });
    document.body.appendChild(this.overlay);
  }

  buildHelpHTML() {
    const groups = [
      {
        title: '导航',
        items: Array.from(this.shortcuts.entries())
          .filter(([key]) => /^[1-8]$/.test(key))
      },
      {
        title: '操作',
        items: Array.from(this.shortcuts.entries())
          .filter(([key]) => /^[a-z]$/.test(key))
      },
      {
        title: '通用',
        items: Array.from(this.shortcuts.entries())
          .filter(([key]) => !/^[1-8a-z]$/.test(key))
      }
    ];

    return `
      <div class="shortcuts-panel">
        <div class="shortcuts-header">
          <h2>键盘快捷键</h2>
          <button class="shortcuts-close" aria-label="Close">&times;</button>
        </div>
        <div class="shortcuts-body">
          ${groups.map(group => `
            <div class="shortcuts-group">
              <h3>${group.title}</h3>
              ${group.items.map(([key, { description }]) => `
                <div class="shortcut-row">
                  <kbd>${this.formatKey(key)}</kbd>
                  <span>${description}</span>
                </div>
              `).join('')}
            </div>
          `).join('')}
        </div>
      </div>
    `;
  }

  formatKey(key) {
    const map = { Escape: 'Esc', '?': '?' };
    return map[key] || key.toUpperCase();
  }

  toggleHelp() {
    this.helpVisible ? this.hideHelp() : this.showHelp();
  }

  showHelp() {
    this.overlay.classList.add('visible');
    this.helpVisible = true;
    // Focus close button
    const closeBtn = this.overlay.querySelector('.shortcuts-close');
    if (closeBtn) {
      closeBtn.onclick = () => this.hideHelp();
      closeBtn.focus();
    }
  }

  hideHelp() {
    this.overlay.classList.remove('visible');
    this.helpVisible = false;
  }

  dispose() {
    if (this.overlay?.parentNode) {
      this.overlay.parentNode.removeChild(this.overlay);
    }
  }
}
