// Onboarding Tour - Interactive first-run walkthrough
// Shows on first visit, can be re-triggered from command palette or help

const STORAGE_KEY = 'ruview-onboarding-done';

export class Onboarding {
  constructor(app) {
    this.app = app;
    this.overlay = null;
    this.currentStep = 0;
    this.steps = [];
    this.active = false;
  }

  init() {
    this.defineSteps();
    document.addEventListener('start-onboarding', () => this.start());

    // Auto-start on first visit
    if (!this.isDone()) {
      // Delay to let the app render first
      setTimeout(() => this.start(), 800);
    }
  }

  defineSteps() {
    this.steps = [
      {
        title: '欢迎使用 RuView',
        text: '基于 WiFi 的隔墙人体姿态估计。先带你快速过一遍仪表盘。',
        target: null, // No highlight, centered
        position: 'center'
      },
      {
        title: '系统状态',
        text: '实时监控 WiFi 感知硬件与 API 服务状态，显示绿色即代表一切正常。',
        target: '.live-status-panel',
        position: 'bottom'
      },
      {
        title: '实时演示',
        text: '切换到“实时演示”页即可查看实时姿态检测，可接入 ESP32 传感器或使用内置模拟。',
        target: '[data-tab="demo"]',
        position: 'bottom'
      },
      {
        title: '感知可视化',
        text: '“感知”页以三维高斯泼溅方式展示 WiFi 信号场，并附带实时指标。',
        target: '[data-tab="sensing"]',
        position: 'bottom'
      },
      {
        title: '键盘快捷键',
        text: '按 ? 查看快捷键，按 Ctrl+K 打开命令面板，数字键 1-8 可快速切换标签页。',
        target: null,
        position: 'center'
      },
      {
        title: '全部就绪！',
        text: '去浏览仪表盘、连接硬件或启动演示吧。随时可从命令面板重播本引导。',
        target: null,
        position: 'center'
      }
    ];
  }

  isDone() {
    try { return localStorage.getItem(STORAGE_KEY) === 'true'; }
    catch { return false; }
  }

  markDone() {
    try { localStorage.setItem(STORAGE_KEY, 'true'); }
    catch { /* noop */ }
  }

  start() {
    this.currentStep = 0;
    this.active = true;
    this.createOverlay();
    this.showStep();
  }

  createOverlay() {
    // Remove existing if any
    this.removeOverlay();

    this.overlay = document.createElement('div');
    this.overlay.className = 'onboarding-overlay';
    this.overlay.setAttribute('role', 'dialog');
    this.overlay.setAttribute('aria-label', '新手引导');
    this.overlay.setAttribute('aria-modal', 'true');
    document.body.appendChild(this.overlay);
  }

  showStep() {
    if (this.currentStep >= this.steps.length) {
      this.finish();
      return;
    }

    const step = this.steps[this.currentStep];
    const total = this.steps.length;
    const isFirst = this.currentStep === 0;
    const isLast = this.currentStep === total - 1;

    // Clear highlight
    document.querySelectorAll('.onboarding-highlight').forEach(el => el.classList.remove('onboarding-highlight'));

    // Highlight target
    let targetRect = null;
    if (step.target) {
      const targetEl = document.querySelector(step.target);
      if (targetEl) {
        targetEl.classList.add('onboarding-highlight');
        targetRect = targetEl.getBoundingClientRect();
      }
    }

    this.overlay.innerHTML = `
      <div class="onboarding-backdrop"></div>
      <div class="onboarding-tooltip ${step.position}" ${targetRect ? `style="${this.positionTooltip(targetRect, step.position)}"` : ''}>
        <div class="onboarding-progress">
          ${Array.from({ length: total }, (_, i) =>
            `<span class="onboarding-dot ${i === this.currentStep ? 'active' : i < this.currentStep ? 'done' : ''}"></span>`
          ).join('')}
        </div>
        <h3 class="onboarding-title">${step.title}</h3>
        <p class="onboarding-text">${step.text}</p>
        <div class="onboarding-actions">
          <button class="onboarding-skip">跳过引导</button>
          <div class="onboarding-nav">
            ${!isFirst ? '<button class="onboarding-prev">上一步</button>' : ''}
            <button class="onboarding-next">${isLast ? '开始使用' : '下一步'}</button>
          </div>
        </div>
      </div>
    `;

    // Bind events
    this.overlay.querySelector('.onboarding-skip').addEventListener('click', () => this.finish());
    this.overlay.querySelector('.onboarding-next').addEventListener('click', () => {
      this.currentStep++;
      this.showStep();
    });
    const prevBtn = this.overlay.querySelector('.onboarding-prev');
    if (prevBtn) {
      prevBtn.addEventListener('click', () => {
        this.currentStep--;
        this.showStep();
      });
    }
    this.overlay.querySelector('.onboarding-backdrop').addEventListener('click', () => this.finish());

    // Focus next button
    this.overlay.querySelector('.onboarding-next').focus();

    // Escape to close
    this._escHandler = (e) => { if (e.key === 'Escape') this.finish(); };
    document.addEventListener('keydown', this._escHandler);
  }

  positionTooltip(rect, position) {
    const margin = 12;
    if (position === 'bottom') {
      return `left: ${Math.max(16, rect.left + rect.width / 2 - 180)}px; top: ${rect.bottom + margin}px;`;
    }
    if (position === 'top') {
      return `left: ${Math.max(16, rect.left + rect.width / 2 - 180)}px; bottom: ${window.innerHeight - rect.top + margin}px;`;
    }
    return '';
  }

  finish() {
    this.active = false;
    this.markDone();
    this.removeOverlay();
    document.querySelectorAll('.onboarding-highlight').forEach(el => el.classList.remove('onboarding-highlight'));
    if (this._escHandler) document.removeEventListener('keydown', this._escHandler);
  }

  removeOverlay() {
    if (this.overlay?.parentNode) {
      this.overlay.parentNode.removeChild(this.overlay);
      this.overlay = null;
    }
  }

  dispose() {
    this.finish();
  }
}
