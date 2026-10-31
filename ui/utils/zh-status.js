/**
 * 后端返回的英文状态文案 -> 中文
 * 仅用于界面显示，不改变后端数据。
 */

const STATUS_MAP = {
  healthy: '正常',
  degraded: '降级',
  unhealthy: '异常',
  down: '离线',
  unknown: '未知',
  live: '实时',
  simulated: '模拟',
  offline: '离线',
  connecting: '连接中',
  connected: '已连接',
  error: '错误',
  warning: '警告',
  active: '活动',
  idle: '空闲'
};

const PHRASES = [
  [/^API server is running normally\.?$/i, 'API 服务运行正常'],
  [/^Server running without hardware\.?$/i, '服务运行中，未检测到硬件'],
  [/^Real hardware connected\.?$/i, '已连接真实硬件'],
  [/^sensing pipeline running\.?$/i, '感知流水线运行中'],
  [/^ESP32\s*(\w+)?\s*stream,?\s*(\d+)\s*node\(s\)\.?$/i, 'ESP32 数据流，$2 个节点'],
  [/^(\d+)\s*frames? received\.?$/i, '已接收 $1 帧'],
  [/^No hardware detected\.?$/i, '未检测到硬件'],
  [/^Hardware connected\.?$/i, '硬件已连接'],
  [/^Streaming started\.?$/i, '数据流已启动'],
  [/^Streaming stopped\.?$/i, '数据流已停止'],
  [/^API server is not responding\.?$/i, 'API 服务无响应'],
  [/^Waiting for data\.?$/i, '等待数据'],
  [/^No data\.?$/i, '暂无数据'],
  [/^Connection lost\.?$/i, '连接已断开'],
  [/^Reconnecting\.\.\.$/i, '正在重连…'],
  [/^Weak$/i, '弱'],
  [/^Good$/i, '良好'],
  [/^Strong$/i, '强'],
  [/^Moderate$/i, '中等']
];

export function zhStatus(value) {
  if (value === null || value === undefined) return value;
  const s = String(value).trim();
  if (!s) return value;
  const lower = s.toLowerCase();
  if (STATUS_MAP[lower]) return STATUS_MAP[lower];
  for (const [re, rep] of PHRASES) {
    if (re.test(s)) return s.replace(re, rep);
  }
  return value;
}

export default zhStatus;
