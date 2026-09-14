// Run with a JavaScript engine and the page's inline script (without its startup calls).
// Network and DOM are simulated; this does not contact the Pi.
const elements = new Map();
const document = {getElementById(id) {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {textContent: '', innerHTML: '', value: '', classList: {
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
let historyFixture = {start: fixture.server_time_seconds-86400, end: fixture.server_time_seconds,
  bucket_seconds: 87, sample_count: 0, available_start: null, available_end: null,
  metrics: [{key:'charge_W',label:'Charging power',unit:'W'}, {key:'battery_V',label:'Battery voltage',unit:'V'},
    {key:'PV_V',label:'Solar input voltage',unit:'V'}, {key:'controller_C',label:'Temperature',unit:'°C'}]};
let requestedURL = '';

async function fetch(url) {
  requestedURL = url;
  if (pendingFetch) await pendingFetch;
  if (failedFetch) throw new Error('Offline');
  return {ok: true, json: async () => url === '/api/status' ? fixture : {...historyFixture, rows}};
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
  assert($('history-message').textContent.includes('unavailable'), 'History failure hidden');
  failedFetch = false;
  await history();
  assert($('history-message').textContent.includes('No readings'), 'History error persists after recovery');
  rows = [{ts: fixture.server_time_seconds - 10, charge_W: 20, battery_V: 27.4, PV_V: 42}];
  historyFixture.sample_count = 1;
  await history();
  assert($('history-message').textContent === '', 'Single reading incorrectly called empty');
  assert($('charts').innerHTML.includes('<circle'), 'Single reading invisible');
  assert($('charts').innerHTML.includes('Battery voltage') && $('charts').innerHTML.includes('Solar input voltage'), 'Overview missing metrics');
  assert(requestedURL.includes('end='+fixture.server_time_seconds), 'Query uses wrong browser clock');
  $('metric').value = 'all';
  renderHistory();
  assert($('charts').innerHTML.includes('Temperature'), 'All metrics omitted temperature');
  assert($('charts').innerHTML.includes('No recorded values'), 'Missing fields presented as zero');
  $('metric').value = 'battery_V';
  renderHistory();
  assert(!$('charts').innerHTML.includes('Charging power'), 'Metric selection ignored');
  const metric = {key:'controller_C',label:'Temperature',unit:'°C'};
  const svg = chart(metric, {...historyFixture, rows:[{ts:historyFixture.start+10,controller_C:-4},{ts:historyFixture.end-10,controller_C:0}]});
  assert(svg.includes('-4') && !svg.includes('NaN'), 'Negative/zero metric invalid');
  assert((svg.match(/M[0-9]/g)||[]).length === 2, 'Outage joined by a line');
  assert(escapeHTML('<img onerror="x">').includes('&lt;'), 'Unsafe markup was not escaped');
  $('range').value = 'custom';
  $('start').value = '2026-09-15T00:00'; $('end').value = '2026-09-14T00:00';
  await loadHistory();
  assert($('history-message').textContent.includes('valid start'), 'Reversed range accepted');
  $('start').value = '2026-09-13T00:00';
  await loadHistory();
  assert(requestedURL.includes('start='+new Date('2026-09-13T00:00').getTime()/1000), 'Custom local dates parsed incorrectly');
  $('range').value = 'all';
  await loadHistory();
  assert(requestedURL.endsWith('range=all'), 'All-history query incorrect');

  // A slower response for the previous range must not replace the latest range.
  const originalFetch = fetch, responses = [];
  fetch = url => new Promise(resolve => responses.push(resolve));
  const olderRequest = loadHistory();
  const newerRequest = loadHistory();
  const newest = {...historyFixture, rows, sample_count: 22};
  responses[1]({ok:true,json:async()=>newest});
  await newerRequest;
  responses[0]({ok:true,json:async()=>({...newest,sample_count:11})});
  await olderRequest;
  assert(historyData.sample_count === 22, 'Old range response replaced the newer range');
  fetch = originalFetch;

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
  return 'PASS: freshness, errors, all metrics, date ranges, zero/negative/missing values, gaps, escaping, and scheduling';
}
