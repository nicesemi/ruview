// Internationalization - ZH/EN/PL language support
// Detects browser language, persists choice, translates UI strings

const translations = {
  en: {
    // Navigation
    'nav.dashboard': 'Dashboard',
    'nav.hardware': 'Hardware',
    'nav.demo': 'Live Demo',
    'nav.architecture': 'Architecture',
    'nav.performance': 'Performance',
    'nav.applications': 'Applications',
    'nav.sensing': 'Sensing',
    'nav.training': 'Training',

    // Dashboard
    'dashboard.title': 'Revolutionary WiFi-Based Human Pose Detection',
    'dashboard.subtitle': 'Human Tracking Through Walls Using WiFi Signals',
    'dashboard.description': 'AI can track your full-body movement through walls using just WiFi signals. Researchers at Carnegie Mellon have trained a neural network to turn basic WiFi signals into detailed wireframe models of human bodies.',
    'dashboard.status': 'System Status',
    'dashboard.metrics': 'System Metrics',
    'dashboard.features': 'Features',
    'dashboard.liveStats': 'Live Statistics',
    'dashboard.activePersons': 'Active Persons',
    'dashboard.avgConfidence': 'Avg Confidence',
    'dashboard.totalDetections': 'Total Detections',
    'dashboard.zoneOccupancy': 'Zone Occupancy',

    // Status
    'status.apiServer': 'API Server',
    'status.hardware': 'Hardware',
    'status.inference': 'Inference',
    'status.streaming': 'Streaming',
    'status.dataSource': 'Data Source',

    // Metrics
    'metrics.cpu': 'CPU Usage',
    'metrics.memory': 'Memory Usage',
    'metrics.disk': 'Disk Usage',

    // Benefits
    'benefit.throughWalls': 'Through Walls',
    'benefit.throughWallsDesc': 'Works through solid barriers with no line of sight required',
    'benefit.privacy': 'Privacy-Preserving',
    'benefit.privacyDesc': 'No cameras or visual recording - just WiFi signal analysis',
    'benefit.realtime': 'Real-Time',
    'benefit.realtimeDesc': 'Maps 24 body regions in real-time at 100Hz sampling rate',
    'benefit.lowCost': 'Low Cost',
    'benefit.lowCostDesc': 'Built using $30 commercial WiFi hardware',

    // Stats
    'stat.bodyRegions': 'Body Regions',
    'stat.samplingRate': 'Sampling Rate',
    'stat.accuracy': 'Accuracy (AP@50)',
    'stat.hardwareCost': 'Hardware Cost',

    // Actions
    'action.startDetection': 'Start Detection',
    'action.stopDetection': 'Stop Detection',
    'action.toggleTheme': 'Toggle theme',
    'action.exportData': 'Export data',
    'action.screenshot': 'Take screenshot',

    // Connection
    'conn.connected': 'Connected',
    'conn.connecting': 'Connecting...',
    'conn.offline': 'Offline',
    'conn.reconnecting': 'Reconnecting...',
    'conn.live': 'Live',
    'conn.simulated': 'Simulated',

    // Misc
    'misc.loading': 'Loading...',
    'misc.error': 'An error occurred',
    'misc.noData': 'No data available',
    'misc.close': 'Close',
    'misc.cancel': 'Cancel',
    'misc.confirm': 'Confirm',
    'misc.settings': 'Settings',
    'misc.language': 'Language'
  },

  zh: {
    // Navigation
    'nav.dashboard': '仪表盘',
    'nav.hardware': '硬件',
    'nav.demo': '实时演示',
    'nav.architecture': '系统架构',
    'nav.performance': '性能分析',
    'nav.applications': '应用场景',
    'nav.sensing': '感知',
    'nav.training': '模型训练',

    // Dashboard
    'dashboard.title': '基于 WiFi 的革命性人体姿态检测',
    'dashboard.subtitle': '利用 WiFi 信号隔墙追踪人体',
    'dashboard.description': 'AI 仅凭 WiFi 信号就能隔墙追踪你的全身动作。卡内基梅隆大学的研究者训练了一个神经网络，把基础 WiFi 信号转换为精细的人体骨架模型，NiceSemi基于此神经网络创造了WiFi DensePose系统π RuView，并且在Github上开源。',
    'dashboard.status': '系统状态',
    'dashboard.metrics': '系统指标',
    'dashboard.features': '功能特性',
    'dashboard.liveStats': '实时统计',
    'dashboard.activePersons': '在场人数',
    'dashboard.avgConfidence': '平均置信度',
    'dashboard.totalDetections': '累计检测数',
    'dashboard.zoneOccupancy': '区域占用',

    // Status
    'status.apiServer': 'API 服务',
    'status.hardware': '硬件',
    'status.inference': '推理引擎',
    'status.streaming': '数据流',
    'status.dataSource': '数据源',

    // Metrics
    'metrics.cpu': 'CPU 占用',
    'metrics.memory': '内存占用',
    'metrics.disk': '磁盘占用',

    // Benefits
    'benefit.throughWalls': '隔墙感知',
    'benefit.throughWallsDesc': '可穿透实体遮挡，无需视线直连',
    'benefit.privacy': '隐私友好',
    'benefit.privacyDesc': '不使用摄像头或录像，仅分析 WiFi 信号',
    'benefit.realtime': '实时',
    'benefit.realtimeDesc': '以 100Hz 采样率实时映射 24 个人体部位',
    'benefit.lowCost': '低成本',
    'benefit.lowCostDesc': '基于 30 美元的商用 WiFi 硬件搭建',

    // Stats
    'stat.bodyRegions': '人体部位',
    'stat.samplingRate': '采样率',
    'stat.accuracy': '准确率（AP@50）',
    'stat.hardwareCost': '硬件成本',

    // Actions
    'action.startDetection': '开始检测',
    'action.stopDetection': '停止检测',
    'action.toggleTheme': '切换主题',
    'action.exportData': '导出数据',
    'action.screenshot': '截图',

    // Connection
    'conn.connected': '已连接',
    'conn.connecting': '正在连接…',
    'conn.offline': '离线',
    'conn.reconnecting': '正在重连…',
    'conn.live': '实时',
    'conn.simulated': '模拟',

    // Misc
    'misc.loading': '加载中…',
    'misc.error': '发生错误',
    'misc.noData': '暂无数据',
    'misc.close': '关闭',
    'misc.cancel': '取消',
    'misc.confirm': '确认',
    'misc.settings': '设置',
    'misc.language': '语言'
  },

  pl: {
    // Navigation
    'nav.dashboard': 'Panel',
    'nav.hardware': 'Sprzet',
    'nav.demo': 'Demo na zywo',
    'nav.architecture': 'Architektura',
    'nav.performance': 'Wydajnosc',
    'nav.applications': 'Aplikacje',
    'nav.sensing': 'Czujniki',
    'nav.training': 'Trening',

    // Dashboard
    'dashboard.title': 'Rewolucyjne wykrywanie pozy czlowieka przez WiFi',
    'dashboard.subtitle': 'Sledzenie ludzi przez sciany za pomoca sygnalow WiFi',
    'dashboard.description': 'AI moze sledzic ruchy calego ciala przez sciany uzywajac jedynie sygnalow WiFi. Badacze z Carnegie Mellon wytrenowali siec neuronowa do zamiany sygnalow WiFi w szczegolowe modele szkieletowe.',
    'dashboard.status': 'Status systemu',
    'dashboard.metrics': 'Metryki systemu',
    'dashboard.features': 'Funkcje',
    'dashboard.liveStats': 'Statystyki na zywo',
    'dashboard.activePersons': 'Aktywne osoby',
    'dashboard.avgConfidence': 'Srednia pewnosc',
    'dashboard.totalDetections': 'Laczne detekcje',
    'dashboard.zoneOccupancy': 'Zajecie stref',

    // Status
    'status.apiServer': 'Serwer API',
    'status.hardware': 'Sprzet',
    'status.inference': 'Wnioskowanie',
    'status.streaming': 'Streaming',
    'status.dataSource': 'Zrodlo danych',

    // Metrics
    'metrics.cpu': 'Uzycie CPU',
    'metrics.memory': 'Uzycie pamieci',
    'metrics.disk': 'Uzycie dysku',

    // Benefits
    'benefit.throughWalls': 'Przez sciany',
    'benefit.throughWallsDesc': 'Dziala przez przeszkody stale bez linii wzroku',
    'benefit.privacy': 'Ochrona prywatnosci',
    'benefit.privacyDesc': 'Brak kamer i nagrywania - tylko analiza sygnalow WiFi',
    'benefit.realtime': 'Czas rzeczywisty',
    'benefit.realtimeDesc': 'Mapuje 24 regiony ciala w czasie rzeczywistym przy 100Hz',
    'benefit.lowCost': 'Niski koszt',
    'benefit.lowCostDesc': 'Zbudowany z komercyjnego sprzetu WiFi za $30',

    // Stats
    'stat.bodyRegions': 'Regiony ciala',
    'stat.samplingRate': 'Czestotliwosc',
    'stat.accuracy': 'Dokladnosc (AP@50)',
    'stat.hardwareCost': 'Koszt sprzetu',

    // Actions
    'action.startDetection': 'Rozpocznij detekcje',
    'action.stopDetection': 'Zatrzymaj detekcje',
    'action.toggleTheme': 'Zmien motyw',
    'action.exportData': 'Eksportuj dane',
    'action.screenshot': 'Zrob zrzut ekranu',

    // Connection
    'conn.connected': 'Polaczono',
    'conn.connecting': 'Laczenie...',
    'conn.offline': 'Offline',
    'conn.reconnecting': 'Ponowne laczenie...',
    'conn.live': 'Na zywo',
    'conn.simulated': 'Symulacja',

    // Misc
    'misc.loading': 'Ladowanie...',
    'misc.error': 'Wystapil blad',
    'misc.noData': 'Brak danych',
    'misc.close': 'Zamknij',
    'misc.cancel': 'Anuluj',
    'misc.confirm': 'Potwierdz',
    'misc.settings': 'Ustawienia',
    'misc.language': 'Jezyk'
  }
};

export class I18n {
  constructor() {
    this.locale = this.getSavedLocale() || this.detectLocale();
    this.listeners = [];
  }

  init() {
    this.createSelector();
    this.applyTranslations();
  }

  detectLocale() {
    const lang = navigator.language?.toLowerCase() || 'zh';
    if (lang.startsWith('zh')) return 'zh';
    if (lang.startsWith('pl')) return 'pl';
    return 'zh';
  }

  getSavedLocale() {
    try {
      // 仅当用户在下拉框中主动切换过语言时才使用保存值，否则按浏览器语言自动判定
      if (localStorage.getItem('ruview-locale-explicit') !== 'true') return null;
      return localStorage.getItem('ruview-locale');
    } catch { return null; }
  }

  saveLocale(locale) {
    try {
      localStorage.setItem('ruview-locale', locale);
      localStorage.setItem('ruview-locale-explicit', 'true');
    } catch { /* noop */ }
  }

  t(key) {
    const dict = translations[this.locale] || translations.en;
    return dict[key] || translations.en[key] || key;
  }

  setLocale(locale) {
    if (!translations[locale]) return;
    this.locale = locale;
    this.saveLocale(locale);
    document.documentElement.setAttribute('lang', locale);
    this.applyTranslations();
    this.listeners.forEach(cb => { try { cb(locale); } catch { /* noop */ } });
  }

  onLocaleChange(callback) {
    this.listeners.push(callback);
    return () => {
      const i = this.listeners.indexOf(callback);
      if (i > -1) this.listeners.splice(i, 1);
    };
  }

  applyTranslations() {
    // Translate elements with data-i18n attribute
    document.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.getAttribute('data-i18n');
      el.textContent = this.t(key);
    });

    // Translate placeholders
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      const key = el.getAttribute('data-i18n-placeholder');
      el.placeholder = this.t(key);
    });

    // Translate aria-labels
    document.querySelectorAll('[data-i18n-aria]').forEach(el => {
      const key = el.getAttribute('data-i18n-aria');
      el.setAttribute('aria-label', this.t(key));
    });

    // Update language selector
    const selector = document.getElementById('lang-selector');
    if (selector) selector.value = this.locale;
  }

  createSelector() {
    const wrapper = document.createElement('div');
    wrapper.className = 'lang-selector-wrap';
    wrapper.innerHTML = `
      <select id="lang-selector" class="lang-selector" aria-label="语言">
        <option value="zh">中文</option>
        <option value="en">EN</option>
        <option value="pl">PL</option>
      </select>
    `;

    const select = wrapper.querySelector('select');
    select.value = this.locale;
    select.addEventListener('change', () => this.setLocale(select.value));

    const headerInfo = document.querySelector('.header-info');
    if (headerInfo) {
      headerInfo.appendChild(wrapper);
    }
  }

  getAvailableLocales() {
    return Object.keys(translations);
  }

  dispose() {
    this.listeners = [];
  }
}

export const i18n = new I18n();
