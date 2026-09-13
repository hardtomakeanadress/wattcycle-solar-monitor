// Run with a JavaScript engine and the page's inline script (without its startup calls).
// Network and DOM are simulated; this does not contact the Pi.
const elements = new Map();
const document = {getElementById(id) {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {textContent: '', innerHTML: '', classList: {
      toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); }
    }});
  }
  return elements.get(id);
}};
let monotonicNow = 0, failedFetch = false, pendingFetch = null;
const performance = {now: () => monotonicNow};
const scheduled = [];
function setTimeout(callback, delay) { scheduled.push({callback, delay}); return scheduled.length; }
function clearTimeout() {}
function setInterval() {}
class AbortController { constructor() { this.signal = {}; } abort() {} }
const fixture = {
  reading: {timestamp_utc: '2026-09-13T15:00:00Z', model: 'M-2430N', system_V: 24,
    charge_W: 20, battery_V: 27.4, PV_V: 44, charge_A: 0.7,
    today_generation_Wh: 373, today_peak_W: 231, controller_estimated_SOC_percent: 100,
    charge_status: 'float', controller_C: 20, battery_temperature_field_C: 25,
    load_on: false, fault_bits: 0},
  error: null, stale: false, age_seconds: 10, stale_after_seconds: 180,
  poll_interval_seconds: 60, server_time_seconds: 1789311610
};
let rows = [];
async function fetch(url) {
  if (pendingFetch) await pendingFetch;
  if (failedFetch) throw new Error('Offline');
  return {ok: true, json: async () => url === '/api/status' ? fixture : rows};
}
function assert(condition, message) { if (!condition) throw new Error(message); }

async function runFrontendTests() {
  // A wildly incorrect laptop clock must not mark a fresh Pi reading as stale.
  Date.now = () => 9999999999999;
  await refresh();
  assert($('connection').textContent === '● Live', 'Fresh reading marked stale due to browser clock');
  assert($('updated').textContent.includes('every minute'), 'Interval label incorrect');
  monotonicNow += 171000;
  freshness();
  assert(!$('warning').classList.contains('hidden'), 'Aging reading was not flagged');
  await refresh();
  assert($('warning').classList.contains('hidden'), 'Freshness did not recover');

  failedFetch = true;
  await refresh();
  assert($('warning').textContent.includes('Cannot reach'), 'Connection failure hidden');
  failedFetch = false;
  await refresh();
  assert($('connection').textContent === '● Live', 'Connection recovery failed');
  fixture.error = 'Live reading received, but could not save history.';
  await refresh();
  assert($('warning').textContent.includes('could not save'), 'Storage failure hidden');
  fixture.error = null;

  failedFetch = true;
  await history();
  assert($('empty').textContent.includes('unavailable'), 'History failure hidden');
  failedFetch = false;
  await history();
  assert($('empty').textContent.includes('will appear'), 'History error persists after recovery');
  rows = [{ts: fixture.server_time_seconds - 10, charge_W: 20, battery_V: 27.4}];
  await history();
  assert($('empty').classList.contains('hidden'), 'Single reading incorrectly called empty');
  assert($('chart').innerHTML.includes('<circle'), 'Single reading invisible');
  assert($('chart').innerHTML.includes('cx="584.9"'), 'Chart uses wrong browser clock');

  // No follow-up refresh should be scheduled until the slow request completes.
  let release;
  pendingFetch = new Promise(resolve => { release = resolve; });
  scheduled.length = 0;
  const request = refresh();
  assert(!scheduled.some(task => task.delay === 5000), 'Overlapping refresh scheduled');
  release();
  await request;
  pendingFetch = null;
  assert(scheduled.filter(task => task.delay === 5000).length === 1, 'Refresh did not reschedule');
  return 'PASS: freshness, interval, connection/storage errors, history recovery, single-point chart, non-overlapping requests';
}
