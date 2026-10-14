import {requestJSON} from './api-client.js';
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(value,digits=2)=>Number.isFinite(Number(value))?Number(value).toLocaleString('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits}):'—';
const change=value=>(value>0?'+':'')+num(value)+'%';
const sign=value=>value>0?'positive':value<0?'negative':'neutral';
const stamp=value=>value?value.slice(0,19).replace('T',' '):'等待报价';
async function request(path,method='GET',body){return requestJSON('/market'+path,method,body);}

export function mountLiveWorkspace({getState,isOverview,toast}){
  const root=document.createElement('section');root.id='live-overview';root.hidden=true;
  root.innerHTML=`<div class="live-market-toolbar"><div><span class="live-session" id="live-session">正在连接行情</span><span class="live-check" id="live-check">独立于历史回测持续更新</span></div><div><span id="live-countdown" class="live-countdown"></span><button class="secondary" id="live-refresh">刷新行情</button></div></div><div id="live-market-error" class="market-inline-error" role="status" hidden></div>
    <div class="live-stock-layout"><section class="panel live-price-panel"><div class="live-price-heading"><div><div class="live-stock-label"><h2 id="live-stock-name">正在读取股票详情</h2><span id="live-stock-symbol"></span></div><div class="live-price-values"><b id="live-stock-price">—</b><span id="live-stock-change"></span><small id="live-stock-difference"></small></div></div><div class="live-chart-tabs" role="group" aria-label="价格图表类型"><button data-market-chart="daily" aria-pressed="true">近期日线</button><button data-market-chart="samples" aria-pressed="false">实时采样</button></div></div><div class="live-chart-wrap"><canvas id="live-price-chart" aria-label="所选股票实际价格曲线"></canvas><div class="live-chart-message" id="live-chart-message">正在读取行情数据…</div><div class="live-price-tooltip" id="live-price-tooltip" hidden></div></div><div class="live-chart-footer"><span id="live-chart-range">—</span><span id="live-chart-description">实际日线 · 不复权</span></div></section>
    <aside class="panel live-stock-detail"><div class="panel-heading"><h2 id="live-detail-title">行情详情</h2><span class="chip" id="live-quote-kind">最新报价</span></div><dl id="live-stock-facts" class="live-stock-facts"></dl><div class="live-quote-time"><span>行情源报价时间</span><b id="live-quote-time">—</b><p id="live-quote-note">价格更新取决于市场交易和行情源。</p></div></aside></div>
    <div class="live-bottom-layout"><section class="panel live-watch-panel"><div class="live-watch-heading"><div><h2>自选股票 <span id="live-watch-count" class="muted"></span></h2><p>点击股票查看详情，可按名称或沪深北代码添加。</p></div><form id="live-stock-search"><input id="live-stock-query" aria-label="搜索股票" placeholder="名称或代码，如 600519" maxlength="80"><button class="secondary" type="submit">搜索</button></form></div><div id="live-search-results" class="live-search-results" hidden></div><div class="live-watch-scroll"><table class="live-watch-table"><thead><tr><th>股票 / 代码</th><th>最新价</th><th>涨跌幅</th><th>最高 / 最低</th><th>报价时间</th><th></th></tr></thead><tbody id="live-watch-rows"><tr><td colspan="6">正在读取自选行情…</td></tr></tbody></table></div></section><aside class="panel live-experiment-context"><div class="panel-heading"><h2>Agent 实验</h2><span id="live-experiment-kind" class="chip"></span></div><div id="live-experiment-summary"></div></aside></div>`;
  document.querySelector('#app-content').insertBefore(root,document.querySelector('#dashboard-view'));
  const $=selector=>root.querySelector(selector);
  let snapshot=null,selected='sh000300',detail=null,detailRequest=0,loading=null,mode='daily',hover=null;
  try{selected=localStorage.getItem('alpha-selected-stock')||selected;}catch{}
  let lastReceived=Date.now();

  function updateExperiment(){
    const state=getState();if(!state?.ready)return;
    $('#live-experiment-kind').textContent=state.mode==='live'?'实时账户':'历史账户';
    const historical=state.mode==='replay',completed=state.learning?.stage==='complete';
    $('#live-experiment-summary').innerHTML=`<h3>${esc(state.title)}</h3><p>${historical?(completed?'历史回测已完成。实时行情独立更新，历史结果保留在回测页。':'历史回测进度和验证结果请在回测页查看。'):(state.runtime.running?'Agent 正在跟盘；首页行情持续更新。':'实时账户已就绪；Agent 决策尚未开启。')}</p><div class="live-agent-summary">${state.players.map(p=>`<button data-player="${esc(p.id)}"><span>${esc(p.name)}</span><b class="${sign(p.return_pct)}">${change(p.return_pct)}</b></button>`).join('')}</div><div class="live-experiment-actions"><button class="secondary" data-view="${historical?'history':'live'}">${historical?'查看历史回测':'打开实时模拟'} ↗</button>${historical&&state.learning?.can_promote?'<button class="text-button" data-promote>部署策略至实时模拟 ↗</button>':''}</div>`;
  }

  function renderStrip(){
    if(!isOverview())return;
    const target=document.querySelector('#market-strip'),quotes=snapshot?.quotes||[];
    const source=document.querySelector('#source');if(source&&quotes.length)source.textContent='行情：'+stamp(quotes.find(q=>q.symbol===selected)?.time||quotes[0].time)+' · 腾讯财经';
    target.innerHTML=`<div class="market-context"><b>实时行情</b><small>${snapshot?.checked_at?'检查 '+snapshot.checked_at.slice(11,19):'正在获取最新报价'}</small></div><div class="market-quotes">${quotes.slice(0,6).map(q=>`<button class="market-quote live-strip-quote" data-live-stock="${esc(q.symbol)}" title="报价 ${esc(stamp(q.time))}"><span class="market-name">${esc(q.symbol==='sh000300'?'沪深300':q.name)}</span><b class="market-price">${num(q.price)}</b><span class="market-change ${sign(q.change)}">${change(q.change)}</span></button>`).join('')}</div>`;
  }

  function render(){
    if(!snapshot){updateExperiment();return;}
    const quotes=snapshot.quotes||[],quote=quotes.find(q=>q.symbol===selected);
    $('#live-session').textContent=snapshot.session_label;
    $('#live-session').classList.toggle('trading',snapshot.session==='trading');
    $('#live-check').textContent=snapshot.checked_at?'最近检查 '+snapshot.checked_at.slice(11,19)+' · '+snapshot.refresh_seconds+' 秒自动刷新':'正在获取最新报价';
    $('#live-market-error').hidden=!snapshot.error;$('#live-market-error').textContent=snapshot.error;
    $('#live-watch-count').textContent=snapshot.watchlist.length+' 只';
    $('#live-watch-rows').innerHTML=quotes.filter(q=>q.symbol!=='sh000300').map(q=>`<tr data-stock-row="${esc(q.symbol)}" tabindex="0" role="button" aria-label="查看${esc(q.name)}详情" aria-selected="${q.symbol===selected}" class="${q.symbol===selected?'selected':''}"><td><b>${esc(q.name)}</b><small>${esc(q.symbol)}</small></td><td>${num(q.price)}</td><td class="${sign(q.change)}">${change(q.change)}</td><td class="watch-range">${num(q.high)}<small>${num(q.low)}</small></td><td class="watch-timestamp">${esc(q.time.slice(5,10))}<small>${esc(q.time.slice(11,19))}</small></td><td><button class="watch-remove" data-unwatch="${esc(q.symbol)}" aria-label="移除${esc(q.name)}自选">×</button></td></tr>`).join('')||'<tr><td colspan="6" class="empty">暂无自选报价，可通过上方搜索添加股票。</td></tr>';
    if(quote){
      $('#live-stock-name').textContent=selected==='sh000300'?'沪深300':quote.name;
      $('#live-detail-title').textContent=selected==='sh000300'?'指数详情':'股票详情';
      $('#live-stock-symbol').textContent=quote.symbol;
      $('#live-stock-price').textContent=num(quote.price);
      $('#live-stock-change').textContent=change(quote.change);$('#live-stock-change').className=sign(quote.change);
      $('#live-stock-difference').textContent=(quote.price>=quote.previous?'+':'')+num(quote.price-quote.previous)+' 较昨收';
      const facts=[['今开',num(quote.open)],['昨收',num(quote.previous)],['最高',num(quote.high)],['最低',num(quote.low)],['成交量',num(quote.volume,0)+(selected==='sh000300'?'':' 手')],['振幅',quote.previous?num((quote.high-quote.low)/quote.previous*100)+'%':'—']];
      $('#live-stock-facts').innerHTML=facts.map(([label,value])=>`<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join('');
      $('#live-quote-time').textContent=stamp(quote.time);
      $('#live-quote-kind').textContent=snapshot.session==='trading'?'最新报价':'最近有效报价';
      $('#live-quote-note').textContent=snapshot.unavailable_symbols.includes(selected)?'该股票本次刷新未返回有效报价，保留旧值。':snapshot.session==='closed'?'当前为非交易时段。自动检查继续，价格可能保持最近收盘值。':snapshot.session==='trading'?'行情源持续更新，非交易所逐笔推送。':'请以原始报价时间判断行情是否更新。';
    }
    renderStrip();updateExperiment();requestAnimationFrame(drawChart);
  }

  async function loadDetail(){
    const serial=++detailRequest,symbol=selected;
    try{const result=await request('/stocks/'+encodeURIComponent(symbol));if(serial!==detailRequest||selected!==symbol)return;detail=result;requestAnimationFrame(drawChart);}
    catch(error){if(serial!==detailRequest)return;detail={symbol,bars:[],samples:[],error:error.message};requestAnimationFrame(drawChart);}
  }
  function select(symbol){
    selected=symbol;detail=null;hover=null;try{localStorage.setItem('alpha-selected-stock',symbol);}catch{}
    render();loadDetail();
  }
  function accept(data){
    if(snapshot&&snapshot.monitor_id===data.monitor_id&&snapshot.revision>data.revision)return;
    snapshot=data;lastReceived=Date.now();
    if(data.quotes?.length&&!data.quotes.some(q=>q.symbol===selected))selected=data.quotes[0].symbol;
    render();if(data.quotes?.some(q=>q.symbol===selected))loadDetail();
  }
  async function refresh(force=false){
    if(loading)return loading;
    $('#live-refresh').disabled=true;
    loading=request('/live'+(force?'?refresh=true':'')).then(accept).catch(error=>{ $('#live-market-error').hidden=false;$('#live-market-error').textContent=error.message; }).finally(()=>{loading=null;$('#live-refresh').disabled=false;});
    return loading;
  }

  function chartPoints(){
    if(!detail||detail.symbol!==selected)return [];
    if(mode==='samples')return detail.samples.map(p=>({label:stamp(p.time),price:p.price}));
    const bars=detail.bars.slice(-60).map(b=>({label:b.date,price:b.close}));
    const quote=snapshot?.quotes.find(q=>q.symbol===selected);
    if(quote&&bars.length&&quote.date>=bars.at(-1).label){
      const point={label:quote.date,price:quote.price};
      if(quote.date===bars.at(-1).label)bars[bars.length-1]=point;else bars.push(point);
    }
    return bars;
  }
  function drawChart(){
    if(root.hidden)return;
    const canvas=$('#live-price-chart'),rect=canvas.getBoundingClientRect();if(!rect.width)return;
    const ratio=Math.min(devicePixelRatio,2);canvas.width=rect.width*ratio;canvas.height=rect.height*ratio;
    const context=canvas.getContext('2d');context.scale(ratio,ratio);
    const points=chartPoints().filter(p=>Number.isFinite(p.price)&&p.price>0),message=$('#live-chart-message'),tooltip=$('#live-price-tooltip');
    message.hidden=points.length>0;message.textContent=detail?.error||(!detail?'正在读取价格数据…':'暂无有效价格序列。');
    $('#live-chart-description').textContent=detail?.error|| (mode==='daily'?'实际日线 · 最新报价更新末点 · 不复权':points.length<2?'等待新的行情源报价 · 不生成模拟波动':'仅记录行情源返回的新报价');
    $('#live-chart-range').textContent=points.length?points[0].label+' — '+points.at(-1).label:'—';
    tooltip.hidden=true;if(!points.length)return;
    const w=rect.width,h=rect.height,left=12,right=65,top=15,bottom=29,span=w-left-right;
    let low=Math.min(...points.map(p=>p.price)),high=Math.max(...points.map(p=>p.price)),pad=Math.max((high-low)*.14,high*.002);low-=pad;high+=pad;
    const x=i=>left+span*(points.length===1?.5:i/(points.length-1)),y=value=>top+(h-top-bottom)*(1-(value-low)/(high-low));
    context.font='10px Consolas, monospace';context.lineWidth=1;
    for(let i=0;i<5;i++){const value=low+(high-low)*i/4,py=y(value);context.strokeStyle='#e3e9f1';context.setLineDash([3,5]);context.beginPath();context.moveTo(left,py);context.lineTo(w-right,py);context.stroke();context.fillStyle='#6c7d92';context.fillText(num(value),w-right+9,py+3);}
    context.setLineDash([]);
    if(points.length>1){const fill=context.createLinearGradient(0,top,0,h-bottom);fill.addColorStop(0,'rgba(102,138,174,.15)');fill.addColorStop(1,'rgba(102,138,174,.015)');context.beginPath();context.moveTo(x(0),h-bottom);points.forEach((point,i)=>context.lineTo(x(i),y(point.price)));context.lineTo(x(points.length-1),h-bottom);context.closePath();context.fillStyle=fill;context.fill();}
    context.beginPath();points.forEach((point,i)=>i?context.lineTo(x(i),y(point.price)):context.moveTo(x(i),y(point.price)));context.strokeStyle='#668aaa';context.lineWidth=2;context.stroke();context.beginPath();context.arc(x(points.length-1),y(points.at(-1).price),3,0,Math.PI*2);context.fillStyle='#52799f';context.fill();
    const ticks=[...new Set([0,Math.floor((points.length-1)/2),points.length-1])];context.fillStyle='#6c7d92';ticks.forEach((index,i)=>{context.textAlign=i===0?'left':i===ticks.length-1?'right':'center';context.fillText(mode==='daily'?points[index].label.slice(5):points[index].label.slice(11,16),x(index),h-6);});
    if(hover!==null){const index=Math.max(0,Math.min(points.length-1,Math.round((hover-left)/span*(points.length-1)))),px=x(index);context.strokeStyle='#a9bbcf';context.lineWidth=1;context.setLineDash([3,4]);context.beginPath();context.moveTo(px,top);context.lineTo(px,h-bottom);context.stroke();context.setLineDash([]);tooltip.innerHTML=`<span>${esc(points[index].label)}</span><b>${selected==='sh000300'?'':'¥'}${num(points[index].price)}</b>`;tooltip.hidden=false;tooltip.style.left=Math.max(5,Math.min(w-160,px+12))+'px';tooltip.style.top='8px';}
  }

  root.addEventListener('click',async event=>{
    const remove=event.target.closest('[data-unwatch]');
    if(remove){event.stopPropagation();remove.disabled=true;try{accept(await request('/watchlist/'+encodeURIComponent(remove.dataset.unwatch),'DELETE'));}catch(error){toast(error.message);remove.disabled=false;}return;}
    const add=event.target.closest('[data-add-stock]');
    if(add){add.disabled=true;try{accept(await request('/watchlist','POST',{symbol:add.dataset.addStock}));select(add.dataset.addStock);$('#live-search-results').hidden=true;$('#live-stock-query').value='';}catch(error){toast(error.message);add.disabled=false;}return;}
    const tab=event.target.closest('[data-market-chart]');if(tab){mode=tab.dataset.marketChart;hover=null;root.querySelectorAll('[data-market-chart]').forEach(button=>button.setAttribute('aria-pressed',button===tab));drawChart();return;}
    const row=event.target.closest('[data-stock-row]');if(row)select(row.dataset.stockRow);
  });
  root.addEventListener('keydown',event=>{const row=event.target.closest('[data-stock-row]');if(row&&event.target===row&&(event.key==='Enter'||event.key===' ')){event.preventDefault();select(row.dataset.stockRow);}});
  document.querySelector('#market-strip').addEventListener('click',event=>{const button=event.target.closest('[data-live-stock]');if(button)select(button.dataset.liveStock);});
  $('#live-refresh').onclick=()=>refresh(true);
  $('#live-stock-search').onsubmit=async event=>{event.preventDefault();const query=$('#live-stock-query').value.trim();if(!query)return;const target=$('#live-search-results');target.hidden=false;target.textContent='正在检索股票…';try{const results=await request('/search?q='+encodeURIComponent(query));target.innerHTML=results.length?results.map(stock=>`<button class="secondary" data-add-stock="${esc(stock.symbol)}">${esc(stock.name)} <small>${esc(stock.symbol)}</small> ＋ 加入自选</button>`).join(''):'没有匹配的 A 股股票，请尝试完整代码或名称。';}catch(error){target.textContent=error.message;}};
  const canvas=$('#live-price-chart');canvas.onpointermove=event=>{hover=event.clientX-canvas.getBoundingClientRect().left;drawChart();};canvas.onpointerleave=()=>{hover=null;drawChart();};
  new ResizeObserver(()=>requestAnimationFrame(drawChart)).observe(canvas);
  setInterval(()=>{const interval=snapshot?.refresh_seconds||30,elapsed=Math.floor((Date.now()-lastReceived)/1000);$('#live-countdown').textContent=Math.max(0,interval-elapsed%interval)+' 秒后检查';if(isOverview()&&elapsed>=interval+3)refresh();},1000);
  refresh();
  return {accept,refresh,renderStrip,updateExperiment,setVisible(visible){root.hidden=!visible;if(visible){render();refresh();requestAnimationFrame(drawChart);}}};
}
