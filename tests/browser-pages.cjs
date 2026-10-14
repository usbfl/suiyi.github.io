// Start tests/browser_server.py on :8011 and serve the Pages build at
// http://127.0.0.1:8080/suiyi.github.io/ before running this isolated smoke test.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const base='http://127.0.0.1:8080/suiyi.github.io/';
const fixture='http://127.0.0.1:8011';
const token='BROWSER_TEST_ONLY_ACCESS_TOKEN_123456789';
(async()=>{
  const identity=await fetch(fixture+'/__test__/identity').then(r=>r.json());
  assert.equal(identity.fixture,true);
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[],badResponses=[],calls=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('response',r=>{if(r.status()>=400)badResponses.push([r.url(),r.status()]);});
    await page.goto(base);
    await page.getByRole('button',{name:'连接后端服务',exact:true}).waitFor();
    assert.match(await page.locator('#initial-loading').innerText(),/Python/);
    assert.equal(await page.locator('#new-button').isEnabled(),false);
    // Proxy fake HTTPS transport to the project's isolated fixture; never use real market/model services.
    await page.route('https://backend.example.test/**',async route=>{
      const request=route.request(),path=new URL(request.url()).pathname;
      const headers={'access-control-allow-origin':'http://127.0.0.1:8080','access-control-allow-headers':'authorization,content-type','access-control-allow-methods':'GET,POST,PUT,DELETE,OPTIONS'};
      if(request.method()==='OPTIONS')return route.fulfill({status:204,headers});
      calls.push({path,auth:request.headers().authorization,url:request.url()});
      assert.equal(request.headers().authorization,'Bearer '+token);
      if(path==='/api/stream') {
        const state=await fetch(fixture+'/api/state').then(r=>r.json());
        return route.fulfill({status:200,headers:{...headers,'content-type':'text/event-stream'},body:'event: state\ndata: '+JSON.stringify(state)+'\n\n'});
      }
      const response=await fetch(fixture+path+new URL(request.url()).search,{method:request.method(),headers:{'Content-Type':'application/json'},body:request.postData()||undefined});
      return route.fulfill({status:response.status,headers:{...headers,'content-type':response.headers.get('content-type')||'application/json'},body:await response.text()});
    });
    await page.getByRole('button',{name:'连接后端服务',exact:true}).click();
    await page.locator('#backend-url').fill('https://backend.example.test');
    await page.locator('#backend-token').fill(token);
    await page.getByRole('button',{name:'验证并连接 →'}).click();
    await page.locator('#live-market-workspace, #market-workspace, .live-workspace').first().waitFor({timeout:10000}).catch(async()=>{await page.locator('#app-content').waitFor({state:'visible'});});
    await page.locator('#navigation [data-view=agents]').click();
    await page.locator('#admin-view .admin-toolbar h2').waitFor();
    assert.match(await page.locator('#admin-view').innerText(),/deepseek-1/);
    assert.ok(calls.some(c=>c.path==='/api/stream'));
    assert.ok(calls.every(c=>!c.url.includes(token)));
    assert.equal(await page.evaluate(t=>JSON.stringify(localStorage).includes(t),token),false);
    await page.reload();
    await page.locator('#admin-view .admin-toolbar h2').waitFor();
    assert.ok(calls.filter(c=>c.path==='/api/config').length>=2);
    await page.screenshot({path:'test-results/pages-connected.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.locator('#backend-button').click();
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=390));
    assert.ok(await page.locator('#backend-dialog').evaluate(el=>el.scrollWidth<=el.clientWidth));
    await page.screenshot({path:'test-results/pages-mobile-connection.png'});
    assert.deepEqual(errors,[]);assert.deepEqual(badResponses,[]);
    console.log(JSON.stringify({passed:true,checks:['repository subpath assets','disconnected state','HTTPS backend connection','Authorization header','SSE','Agent registry','reload persistence','mobile layout'],requests:calls.length}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
