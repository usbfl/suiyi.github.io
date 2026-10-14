// Read-only smoke check for a newly extracted source package with no saved model keys.
const {chromium,expect}=require('@playwright/test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const base=process.env.ALPHA_PACKAGE_BASE||'http://127.0.0.1:8022';
const output=process.env.ALPHA_PACKAGE_PREVIEWS||'docs/previews';
assert(['127.0.0.1','localhost'].includes(new URL(base).hostname));
(async()=>{
  const get=async endpoint=>{const response=await fetch(base+'/api'+endpoint);assert(response.ok,endpoint);return response.json();};
  const connections=await get('/connections');assert(connections.every(c=>!c.has_key),'Use a clean source-package instance');
  const before=await get('/state');assert.equal(before.runtime.calls,0);
  const browser=await chromium.launch({channel:'chrome',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],writes=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',async route=>{const request=route.request();if(!['GET','HEAD'].includes(request.method())){writes.push(request.method());await route.abort();}else if(/^https?:/.test(request.url())&&new URL(request.url()).origin!==base){errors.push('external browser request');await route.abort();}else await route.continue();});
  fs.mkdirSync(output,{recursive:true});
  try{
    await page.goto(base+'/#overview');
    await expect(page.locator('#live-overview')).toBeVisible();
    await expect(page.locator('#live-stock-price')).not.toHaveText('—',{timeout:30000});
    await expect(page.locator('#live-chart-message')).toBeHidden({timeout:30000});
    await expect(page.locator('#dashboard-view')).toBeHidden();
    await page.screenshot({path:path.join(output,'01-live-market.png'),fullPage:true});
    await page.locator('#navigation [data-view="agents"]').click();
    await expect(page.locator('.profile-card')).toHaveCount(6);
    await page.screenshot({path:path.join(output,'02-agent-config.png'),fullPage:true});
    await page.locator('#navigation [data-view="tools"]').click();
    await expect(page.locator('#admin-view')).toContainText('研究查询');
    await page.screenshot({path:path.join(output,'03-research-tools.png'),fullPage:true});
    const after=await get('/state');assert.equal(after.id,before.id);assert.equal(after.runtime.calls,0);assert.equal(after.round,0);assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
    console.log(JSON.stringify({passed:true,clean_instance:true,has_model_key:false,model_calls:0,round:after.round,previews:3,errors,writes}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
