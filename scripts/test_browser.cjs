// Optional browser regression test: npm install --no-save playwright
// DASHBOARD_TEST_URL points to an existing dashboard; only GET requests reach it.
// Heating POST tests are intercepted and never reach hardware.
// The candidate HTML is intercepted locally, so the remote installation is not changed.
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

async function main() {
  const base = process.env.DASHBOARD_TEST_URL;
  if (!base) throw new Error('Set DASHBOARD_TEST_URL to a running dashboard URL.');
  const url = new URL('/', base).href;
  const output = process.env.BROWSER_TEST_OUTPUT || '/tmp/wattcycle-browser-check';
  fs.mkdirSync(output, {recursive:true});
  const candidate = fs.readFileSync(path.join(__dirname,'../dashboard/index.html'),'utf8');
  const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  try {
    const context = await browser.newContext({viewport:{width:1280,height:1000},timezoneId:'Europe/Bucharest'});
    const page = await context.newPage();
    page.setDefaultTimeout(20000);
    const errors = [];
    let expectedHistoryFailure = false;
    const checkConsole = message => {
      if (message.type() !== 'error') return;
      if (expectedHistoryFailure && message.location().url.includes('/api/history/range') && message.text().includes('503')) return;
      errors.push(message.text());
    };
    // Browsers may request an icon even though the dashboard declares none.
    await context.route('**/favicon.ico', route => route.fulfill({status:204}));
    page.on('pageerror',error=>{errors.push(error.message); console.error('Page error:',error.message);});
    page.on('requestfailed',request=>console.error('Request failed:',request.url(),request.failure()));
    page.on('console', checkConsole);
    await page.goto(url);
    await page.waitForFunction(()=>document.querySelectorAll('.metric-chart svg').length===3);
    await page.screenshot({path:path.join(output,'before-desktop.png'),fullPage:true});
    console.log('Live baseline captured.');
    if (!process.env.TEST_DEPLOYED) {
      await page.route(url,route=>route.fulfill({contentType:'text/html',body:candidate}));
      // Forward API responses too: intercepted navigations have no resolved IP
      // address, which can affect Chromium's local-network request classification.
      await page.route('**/api/**',async route=>route.fulfill({response:await route.fetch()}));
    }
    await page.reload();
    console.log('Candidate loaded:',await page.locator('#history-message').textContent());
    try {
      await page.waitForFunction(()=>typeof historyLoading!=='undefined' && !historyLoading && !!historyData);
    } catch (error) {
      await page.screenshot({path:path.join(output,'candidate-failure.png'),fullPage:true});
      console.error(await page.evaluate(()=>({status,historyLoading,historyData,historyRequest,rangeDraft,message:document.getElementById('history-message').textContent})));
      throw error;
    }
    assert.equal(await page.locator('#range').inputValue(),'86400');
    assert.equal(await page.locator('.metric-chart svg').count(),3);
    await page.waitForFunction(()=>!!systemData && systemConnected);
    assert.equal(await page.locator('#system-model').textContent(), await page.evaluate(()=>systemData.model || 'Monitor computer'));
    assert.equal(await page.locator('.system-details, #system-flags').count(),0);
    assert(!(await page.locator('#system-temperature').textContent()).includes('NaN'));
    await page.locator('.system-panel').screenshot({path:path.join(output,'system-desktop.png')});
    assert(await page.locator('#later').isDisabled());
    await page.screenshot({path:path.join(output,'after-desktop.png'),fullPage:true});
    const settled = async()=>page.waitForFunction(()=>!historyLoading && !!historyData && document.getElementById('history-message').textContent!=='Loading history…');
    const data = ()=>page.evaluate(()=>({start:historyData.start,end:historyData.end,rows:historyData.rows.length,bucket:historyData.bucket_seconds}));
    for (const duration of [900,3600,21600]) {
      await page.selectOption('#range',String(duration));
      await settled();
      const view = await data();
      assert.equal(view.end-view.start,duration);
      assert.equal(view.bucket,60);
      assert(view.rows>0 && view.rows<=duration/60+1);
      assert(await page.locator('.metric-chart .time-grid').count()>=3);
    }
    await page.selectOption('#range','3600'); await settled();
    await page.selectOption('#metric','charge_W');
    assert.equal(await page.locator('.metric-chart svg').count(),1);
    assert(await page.locator('#charts').evaluate(el=>el.classList.contains('single')));
    await page.locator('#zoom-in').click(); await settled();
    assert.equal((await data()).end-(await data()).start,1800);
    const zoom = await data();
    await page.locator('#earlier').click(); await settled();
    assert.equal((await data()).end,zoom.start);
    await page.locator('#later').click(); await settled();
    assert.equal((await data()).start,zoom.start);
    await page.locator('#zoom-out').click(); await settled();
    assert.equal((await data()).end-(await data()).start,3600);
    await page.locator('#latest').click(); await settled();
    assert.equal(await page.locator('#range').inputValue(),'follow');
    assert((await page.locator('#history-window').textContent()).includes('following latest'));
    const svg = page.locator('.metric-chart svg');
    await svg.click({position:{x:300,y:90}});
    assert.equal(await page.locator('.crosshair').getAttribute('visibility'),'visible');
    assert(!(await page.locator('.point-detail').textContent()).includes('Latest displayed'));
    await page.screenshot({path:path.join(output,'after-hour-detail.png'),fullPage:true});
    await page.selectOption('#range','900'); await settled();
    while (await page.locator('#zoom-in').isEnabled()) {
      await page.locator('#zoom-in').click(); await settled();
    }
    assert.equal((await data()).end-(await data()).start,300);
    await page.selectOption('#range','custom');
    const customDates = await page.evaluate(()=>[localInput(serverNow()-7200),localInput(serverNow()-3600),localInput(serverNow()-10800)]);
    await page.locator('#start').fill(customDates[0]);
    await page.locator('#end').fill(customDates[1]);
    const draftURL = await page.evaluate(()=>historyRequest);
    await page.evaluate(()=>history());
    assert.equal(await page.evaluate(()=>historyRequest),draftURL);
    await page.getByRole('button',{name:'Apply dates',exact:true}).click(); await settled();
    assert.equal((await data()).end-(await data()).start,3600);
    await page.locator('#end').fill(customDates[2]);
    await page.getByRole('button',{name:'Apply dates',exact:true}).click();
    assert((await page.locator('#history-message').textContent()).includes('valid start'));
    await page.selectOption('#range','900'); await settled();
    await page.selectOption('#metric','all');
    assert.equal(await page.locator('.metric-chart').count(),await page.evaluate(()=>historyData.metrics.length));
    await page.selectOption('#range','all'); await settled();
    assert((await data()).rows<=1000);
    await page.selectOption('#range','900'); await settled();
    await page.selectOption('#metric','overview');
    // Check real touch input and overflow at small phone sizes.
    const phone = await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,deviceScaleFactor:1,timezoneId:'Europe/Bucharest'});
    const mobile = await phone.newPage();
    await phone.route('**/favicon.ico', route => route.fulfill({status:204}));
    mobile.on('pageerror',error=>errors.push(error.message));
    mobile.on('requestfailed',request=>console.error('Mobile request failed:',request.url(),request.failure()));
    mobile.on('console', checkConsole);
    if (!process.env.TEST_DEPLOYED) {
      await mobile.route(url,route=>route.fulfill({contentType:'text/html',body:candidate}));
      await mobile.route('**/api/**',async route=>route.fulfill({response:await route.fetch()}));
    }
    await mobile.goto(url);
    try {
      await mobile.waitForFunction(()=>!historyLoading && !!historyData);
    } catch (error) {
      await mobile.screenshot({path:path.join(output,'mobile-failure.png'),fullPage:true});
      console.error(await mobile.evaluate(()=>({status,historyLoading,historyRequest,message:document.getElementById('history-message').textContent})));
      throw error;
    }
    await mobile.selectOption('#range','900');
    await mobile.waitForFunction(()=>!historyLoading && historyData.end-historyData.start===900);
    assert(await mobile.locator('.metric-chart').first().locator('.time-grid').count()>=3);
    await mobile.locator('.metric-chart svg').first().tap();
    assert.equal(await mobile.locator('.crosshair').first().getAttribute('visibility'),'visible');
    assert(await mobile.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await mobile.screenshot({path:path.join(output,'after-mobile-390.png'),fullPage:true});
    await mobile.setViewportSize({width:320,height:740});
    await mobile.waitForFunction(()=>!!systemData && systemConnected);
    assert(await mobile.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await mobile.locator('.system-panel').screenshot({path:path.join(output,'system-mobile-320.png')});
    await mobile.screenshot({path:path.join(output,'after-mobile-320.png'),fullPage:true});
    // Failed history requests disable navigation, then recover on the next refresh.
    expectedHistoryFailure = true;
    await page.route('**/api/history/range?*',route=>route.fulfill({status:503,body:'Test outage'}));
    await page.getByRole('button',{name:'Refresh history',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('history-message').textContent.includes('unavailable'));
    assert(await page.locator('#zoom-in').isDisabled());
    await page.unroute('**/api/history/range?*');
    await page.getByRole('button',{name:'Refresh history',exact:true}).click(); await settled();
    expectedHistoryFailure = false;
    assert(await page.locator('#zoom-in').isEnabled());
    // Test real button events and alarming with fully intercepted control traffic.
    const heatingFixture = await page.evaluate(()=>JSON.parse(JSON.stringify(status)));
    heatingFixture.heating = {available:true, installation_ready:true, control_configured:true,
      armed:false, pending:false, phase:'disabled', message:'Heating disabled.', alarm:null, output_on:false,
      on_C:5, off_C:10, max_sensor_age_seconds:90};
    heatingFixture.sensor.age_seconds = 0;
    heatingFixture.sensor.error = null;
    const commands = [];
    await page.route('**/api/status', route=>route.fulfill({json:heatingFixture}));
    await page.route('**/api/heating', route=>{
      assert.equal(route.request().method(),'POST');
      assert.equal(route.request().headers().authorization,'Bearer fixture-only-key');
      const command=route.request().postDataJSON(); commands.push(command);
      Object.assign(heatingFixture.heating, {armed:command.armed, phase:command.armed?'heating':'disabled',
        output_on:command.armed, message:command.armed?'Heating enclosure.':'Heating disabled; LOAD OFF verified.'});
      return route.fulfill({status:202,json:{heating:heatingFixture.heating}});
    });
    await page.waitForFunction(()=>status?.heating?.installation_ready);
    await page.locator('#heating-key').fill('fixture-only-key');
    await page.locator('#heating-arm').click();
    await page.waitForFunction(()=>document.getElementById('heating-output').textContent==='ON');
    assert(await page.locator('#heating-panel').evaluate(el=>el.classList.contains('heating-active')));
    await page.locator('#heating-disable').click();
    await page.waitForFunction(()=>document.getElementById('heating-output').textContent==='OFF');
    assert.deepEqual(commands,[{armed:true},{armed:false}]);
    Object.assign(heatingFixture.heating, {phase:'alarm',alarm:'Sensor unavailable',message:'LOAD OFF unconfirmed; inspect locally.',output_on:null});
    await page.waitForFunction(()=>document.getElementById('heating-panel').classList.contains('heating-alarm'));
    assert.equal(await page.locator('#heating-output').textContent(),'Unconfirmed');
    await page.locator('#heating-panel').screenshot({path:path.join(output,'heating-alarm.png')});
    await page.setViewportSize({width:320,height:740});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.locator('#heating-panel').screenshot({path:path.join(output,'heating-mobile-320.png')});
    assert.deepEqual(errors,[]);
    console.log('PASS: live API, presets, zoom/pan/latest, minute limit, hover/tap, all metrics, custom dates, draft refresh, error recovery, desktop and mobile overflow; no page errors.');
    console.log('Screenshots: '+output);
  } finally {
    await browser.close();
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
