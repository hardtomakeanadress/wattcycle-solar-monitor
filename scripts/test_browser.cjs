// Optional browser regression test: npm install --no-save playwright
// DASHBOARD_TEST_URL points to an existing dashboard; only GET requests are made.
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
    page.on('pageerror',error=>{errors.push(error.message); console.error('Page error:',error.message);});
    page.on('requestfailed',request=>console.error('Request failed:',request.url(),request.failure()));
    page.on('console',message=>{if(message.type()==='error') console.error('Browser console:',message.text());});
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
    mobile.on('pageerror',error=>errors.push(error.message));
    mobile.on('requestfailed',request=>console.error('Mobile request failed:',request.url(),request.failure()));
    mobile.on('console',message=>{if(message.type()==='error') console.error('Mobile console:',message.text());});
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
    assert(await mobile.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await mobile.screenshot({path:path.join(output,'after-mobile-320.png'),fullPage:true});
    // Failed history requests disable navigation, then recover on the next refresh.
    await page.route('**/api/history/range?*',route=>route.fulfill({status:503,body:'Test outage'}));
    await page.getByRole('button',{name:'Refresh history',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('history-message').textContent.includes('unavailable'));
    assert(await page.locator('#zoom-in').isDisabled());
    await page.unroute('**/api/history/range?*');
    await page.getByRole('button',{name:'Refresh history',exact:true}).click(); await settled();
    assert(await page.locator('#zoom-in').isEnabled());
    assert.deepEqual(errors,[]);
    console.log('PASS: live API, presets, zoom/pan/latest, minute limit, hover/tap, all metrics, custom dates, draft refresh, error recovery, desktop and mobile overflow; no page errors.');
    console.log('Screenshots: '+output);
  } finally {
    await browser.close();
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
