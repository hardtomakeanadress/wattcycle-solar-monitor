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
let systemFetchFailed = false;
let systemFixture = {timestamp_utc:'2026-10-03T12:00:00Z',age_seconds:0,
  model:'Raspberry Pi 2 Model B Rev 1.1',cpu_cores:4,cpu_model:'ARMv7 Processor',
  os:'Raspbian 13',kernel:'6.12',architecture:'armv7l',python_version:'3.13.5',
  temperature_C:34.7,memory_total_bytes:1073741824,memory_available_bytes:0,
  storage_total_bytes:32*1073741824,storage_free_bytes:24*1073741824,
  uptime_seconds:172861,throttle_current:[],throttle_since_boot:[]};

async function fetch(url) {
  requestedURL = url;
  if (pendingFetch) await pendingFetch;
  if (failedFetch) throw new Error('Offline');
  if (url === '/api/system') {
    if (systemFetchFailed) throw new Error('System unavailable');
    return {ok:true,json:async()=>systemFixture};
  }
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

  await refresh();
  await refreshSystem();
  assert($('system-model').textContent.includes('Raspberry Pi 2'), 'Pi model omitted');
  assert($('system-memory').textContent === '0 MiB', 'Zero available RAM discarded');
  assert($('system-storage').textContent === '24.0 GiB', 'Disk units incorrect');
  assert($('system-uptime').textContent === '2d 0h', 'Uptime formatting incorrect');
  assert($('system-temperature').textContent === '34.7 °C', 'Pi temperature incorrect');
  assert(!$('system-stats').classList.contains('stale'), 'Fresh host data marked stale');
  systemFetchFailed = true; await refreshSystem();
  assert($('system-updated').textContent.includes('Last known'), 'System failure not marked');
  assert($('system-temperature').textContent === '34.7 °C', 'System failure erased last value');
  assert($('connection').textContent === '● Live', 'System endpoint failure affected solar connection');
  systemFetchFailed = false; await refreshSystem();
  monotonicNow += 91000; systemFreshness();
  assert($('system-stats').classList.contains('stale'), 'System age does not advance');
  systemFixture.temperature_C = null; systemFixture.throttle_current = null; systemFixture.throttle_since_boot = null;
  await refreshSystem();
  assert($('system-temperature').textContent === '—', 'Unavailable Pi temperature shown as zero');
  assert(!$('system-stats').classList.contains('stale'), 'System recovery failed');
  assert(systemBytes(null) === '—' && systemUptime(null) === '—', 'Unknown system values invented');
  await refresh();

  // Sensor freshness is independent of solar data and the browser's wall clock.
  fixture.sensor = {enabled:true, name:'Cabana climate', reading:null, error:null,
    age_seconds:null, stale:true, stale_after_seconds:1800, poll_interval_seconds:600};
  await refresh();
  assert($('sensor-temperature').textContent === '—', 'Missing sensor shown as zero');
  assert($('climate-status').textContent === 'Connecting', 'Initial sensor state incorrect');
  assert($('connection').textContent === '● Live', 'Sensor affects solar status');
  fixture.sensor.reading = {temperature_C:0,humidity_percent:0,battery_V:3.136};
  fixture.sensor.age_seconds = 5; fixture.sensor.stale = false;
  await refresh();
  assert($('sensor-temperature').textContent === '0.0', 'Zero temperature omitted');
  assert($('sensor-humidity').textContent === 0, 'Zero humidity omitted');
  assert($('climate-status').textContent === 'Live', 'Fresh sensor not live');
  monotonicNow += 1796000; freshness();
  assert($('climate-status').textContent === 'Last known', 'Sensor age does not advance');
  fixture.sensor.error = 'Sensor unavailable. Retrying every 10 minutes.';
  await refresh();
  assert($('climate-message').textContent.includes('unavailable'), 'Sensor failure hidden');
  assert($('sensor-temperature').textContent === '0.0', 'Failure erased last reading');
  assert($('connection').textContent === '● Live', 'Sensor failure affects solar status');
  fixture.sensor.error = null; fixture.sensor.reading.temperature_C = -5;
  await refresh();
  assert($('sensor-temperature').textContent === '-5.0', 'Negative temperature corrupted');
  assert($('climate-status').textContent === 'Live', 'Sensor did not recover');
  failedFetch = true; await refresh();
  assert($('climate-status').textContent === 'Offline', 'Network failure leaves sensor live');
  failedFetch = false; delete fixture.sensor; await refresh();
  assert($('climate').classList.contains('hidden'), 'Unconfigured sensor visible');

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
  const climateMetric = {key:'sensor_temperature_C',label:'Climate temperature',unit:'°C',source:'sensor',poll_interval_seconds:600};
  const climateData = {...historyFixture,start:0,end:3000,bucket_seconds:60,sensor_bucket_seconds:600,
    rows:[{ts:120,charge_W:20}],sensor_rows:[{ts:100,sensor_temperature_C:-5},{ts:700,sensor_temperature_C:0},{ts:2900,sensor_temperature_C:4}]};
  const climateSVG = chart(climateMetric, climateData);
  assert((climateSVG.match(/M[0-9]/g)||[]).length === 2, 'Climate connects across a missed interval or breaks normal ten-minute readings');
  assert(climateSVG.includes('L') && climateSVG.includes('-5'), 'Climate series does not use sensor timestamps and values');
  assert(nearestReading(metricRows(climateMetric,climateData),climateMetric.key,710).ts === 700, 'Climate inspection uses solar rows');
  const savedHistory = historyData;
  historyData = {...climateData,metrics:[climateMetric]};
  $('metric').value = climateMetric.key; renderHistory();
  assert($('charts').innerHTML.includes('Climate temperature') && $('charts').innerHTML.includes('<circle'), 'Climate chart missing');
  historyData = savedHistory;
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

  // Short presets request the actual minute records using the Pi's clock.
  for (const seconds of [900,3600,21600]) {
    $('range').value = String(seconds);
    await loadHistory();
    assert(requestedURL.includes('start='+(fixture.server_time_seconds-seconds)), 'Short preset has the wrong start');
    assert(requestedURL.includes('end='+fixture.server_time_seconds), 'Short preset has the wrong end');
  }
  const oneHour = {start:1789308000,end:1789311600};
  const zoomed = navigationWindow(oneHour,'in',oneHour.end);
  assert(zoomed.end-zoomed.start === 1800, 'Zoom does not halve the visible hour');
  assert((zoomed.start+zoomed.end)/2 === (oneHour.start+oneHour.end)/2, 'Zoom loses its center');
  assert(navigationWindow(zoomed,'out',oneHour.end).start === oneHour.start, 'Zoom out does not restore the window');
  let smallest = oneHour;
  for (let i=0;i<12;i++) smallest = navigationWindow(smallest,'in',oneHour.end);
  assert(smallest.end-smallest.start === 300, 'Zoom passes the five-minute limit');
  const earlier = navigationWindow(oneHour,'earlier',oneHour.end);
  const later = navigationWindow(earlier,'later',oneHour.end);
  assert(earlier.end === oneHour.start && later.start === oneHour.start, 'Panning skips or distorts the window');
  assert(navigationWindow({start:0,end:900},'earlier',900).start === 0, 'Panning produces negative Unix dates');

  const tickStart = Math.floor(oneHour.start/3600)*3600;
  const ticks = timeTicks(tickStart,tickStart+900);
  assert(ticks.length >= 3 && ticks.every(t=>t%300===0), '15-minute view lacks useful five-minute ticks');
  assert(timeTicks(tickStart,tickStart+3600).every(t=>t%900===0), 'Hour view lacks quarter-hour ticks');
  assert(timeTicks(tickStart,tickStart+900,true).length <= 4, 'Mobile time labels are overcrowded');
  for (const offset of [0,1,61,421]) {
    assert(timeTicks(tickStart+offset,tickStart+offset+900,true).length >= 3, 'Unaligned mobile window has too few time labels');
    assert(timeTicks(tickStart+offset,tickStart+offset+60,true).length >= 2, 'One-minute custom window lacks boundary labels');
  }
  assert(timeTicks(tickStart,tickStart+30*86400).length >= 2, 'Month view has too few time labels');
  assert(/^\d\d:\d\d$/.test(timeLabel(tickStart,900)), 'Short view does not show clear hours and minutes');
  assert(chart(metric,{...historyFixture,...oneHour,rows:[{ts:oneHour.start+60,controller_C:0}]}).includes('time-grid'), 'Time grid is missing');
  const inspectRows = [{ts:100,charge_W:0},{ts:160,charge_W:null},{ts:220,charge_W:10}];
  assert(nearestReading(inspectRows,'charge_W',150).ts === 100, 'Nearest reading loses zero or selects missing value');
  assert(nearestReading(inspectRows,'charge_W',219).ts === 220, 'Nearest reading inspection misses data');
  assert(nearestReading(inspectRows,'absent',150) === null, 'Missing metric invents a reading');
  const attributes = {}, detailElement = {};
  historyData = {...historyFixture,start:100,end:220,rows:inspectRows};
  inspectPoint({clientX:580,currentTarget:{
    getBoundingClientRect:()=>({left:0,width:600}), getAttribute:()=> 'charge_W',
    closest:()=>({querySelector:()=>detailElement}),
    querySelector:()=>({setAttribute:(k,v)=>{attributes[k]=v;}})
  }});
  assert(detailElement.textContent.includes('10 W') && attributes.x1 === 580 && attributes.visibility === 'visible', 'Tap/crosshair does not inspect the plotted sample');

  // Selecting a single measurement gives it the full chart width.
  $('metric').value = 'charge_W'; renderHistory();
  assert($('charts').classList.contains('single'), 'Single metric stays cramped');
  $('metric').value = 'overview'; renderHistory();
  assert(!$('charts').classList.contains('single'), 'Overview incorrectly retains single-chart layout');

  // Navigation writes minute inputs and fetches the selected window.
  historyData = {...historyFixture,...oneHour,rows};
  await navigateHistory('in');
  assert($('range').value === 'custom' && requestedURL.includes('start='+zoomed.start), 'Zoom did not load the selected dates');
  assert(!$('custom-range').classList.contains('hidden'), 'Zoom hides its editable dates');
  historyData = {...historyFixture,...oneHour,rows};
  await navigateHistory('latest');
  assert($('range').value === 'follow' && followSeconds === 3600, 'Latest did not preserve the current zoom');
  monotonicNow += 60000;
  await loadHistory();
  assert(requestedURL.includes('end='+(fixture.server_time_seconds+60)), 'Latest stops following incoming readings');
  monotonicNow -= 60000;

  // Fixed UTC windows must survive local daylight-saving clock repetition.
  const autumnStart = Date.parse('2026-10-25T00:00:00Z')/1000;
  historyData = {...historyFixture,start:autumnStart,end:autumnStart+7200,rows};
  await navigateHistory('in');
  assert(requestedURL.includes('start='+(autumnStart+1800)) && requestedURL.includes('end='+(autumnStart+5400)), 'DST clock repetition distorts zoom');

  // A refresh must never apply dates while the user is still editing them.
  editRange();
  const beforeDraftRefresh = requestedURL;
  $('start').value = '2026-09-12T01:00'; $('end').value = '2026-09-12T02:00';
  await history();
  assert(requestedURL === beforeDraftRefresh && $('zoom-in').disabled, 'Automatic refresh submitted a date draft');
  await loadHistory();
  assert(requestedURL.includes('start='+new Date('2026-09-12T01:00').getTime()/1000), 'Apply dates ignores edited times');
  assert(!rangeDraft && !historyLoading, 'Applied dates leave navigation blocked');

  failedFetch = true;
  await loadHistory();
  assert($('zoom-in').disabled && $('history-window').textContent === '', 'Failed request leaves misleading navigation');
  failedFetch = false;
  await loadHistory();
  assert(!$('zoom-in').disabled, 'Navigation does not recover after a failed request');

  // A slower response for the previous range must not replace the latest range.
  const originalFetch = fetch, responses = [];
  fetch = url => new Promise(resolve => responses.push(resolve));
  const olderRequest = loadHistory();
  assert($('zoom-in').disabled, 'Navigation is active while data is loading');
  const newerRequest = loadHistory();
  const newest = {...historyFixture, rows, sample_count: 22};
  responses[1]({ok:true,json:async()=>newest});
  await newerRequest;
  responses[0]({ok:true,json:async()=>({...newest,sample_count:11})});
  await olderRequest;
  assert(historyData.sample_count === 22, 'Old range response replaced the newer range');
  const draftRequest = loadHistory();
  editRange();
  responses[2]({ok:true,json:async()=>({...newest,sample_count:33})});
  await draftRequest;
  assert(historyData.sample_count === 22 && rangeDraft, 'Pending response overwrote an edited range');
  rangeDraft = false;
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
  return 'PASS: freshness, errors, metrics, short presets, zoom/pan/follow, DST, time ticks, point inspection, date drafts, slow responses, gaps, escaping, and scheduling';
}
