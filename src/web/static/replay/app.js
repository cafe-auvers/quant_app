/* Breakout Lab. Backtesting cutoffs and historical study are enforced server-side. */
"use strict";
const $ = id => document.getElementById(id);
const state = {status:null, scanId:null, rows:[], replay:null, busy:false, dirty:false, daily:null, rs:null, ti:null, intraday:null, intraData:null, paperAt:null, paperStudyId:null, contextDay:null, library:null, poll:null};
function preferredReplayMode(){try{return localStorage.getItem("breakout-lab-chart-mode")==="historical"?"historical":"backtesting";}catch{return "backtesting";}}
const colors = {bg:"#07130f",grid:"#152820",text:"#95a29f",up:"#39c894",down:"#e95b68",orange:"#f5c96a",purple:"#aa82e8",blue:"#56bde9"};
const pct = v => v == null ? "N/A" : `${v>=0?"+":""}${Number(v).toFixed(2)}%`;
const num = v => v == null ? "N/A" : Number(v).toFixed(2);
const esc = v => String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const tag = (kind,text) => `<span class="tag ${esc(kind)}">${esc(text||kind)}</span>`;
const dateText = v => new Date(v+"T12:00:00Z").toLocaleDateString("en-US",{month:"short",day:"numeric",year:"numeric",timeZone:"UTC"});
const nyTime = v => new Date(v*1000).toLocaleTimeString("en-US",{hour:"2-digit",minute:"2-digit",hour12:false,timeZone:"America/New_York"});
function toast(message,error=false){$("toast").textContent=message;$("toast").classList.toggle("error",error);$("toast").hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$("toast").hidden=true,error?8500:4200);}
async function api(path,method="GET",body){const response=await fetch(path,{method,headers:body?{"Content-Type":"application/json"}:{},body:body?JSON.stringify(body):undefined});const payload=await response.json();if(!response.ok)throw new Error(typeof payload.detail==="string"?payload.detail:"The request could not be completed.");return payload;}
async function run(action){if(state.busy)return;state.busy=true;document.body.classList.add("busy");try{updateButtons();await action();}catch(e){toast(e.message,true);}finally{state.busy=false;document.body.classList.remove("busy");updateButtons();}}
function sessionPath(suffix=""){return `/api/sessions/${state.replay.session.id}${suffix}`;}
function chartOptions(height,timeVisible=false){return {height,layout:{background:{type:"solid",color:colors.bg},textColor:colors.text,fontFamily:"Segoe UI, sans-serif",fontSize:12},grid:{vertLines:{color:colors.grid},horzLines:{color:colors.grid}},crosshair:{mode:0,vertLine:{color:"#618078",labelBackgroundColor:"#294039"},horzLine:{color:"#618078",labelBackgroundColor:"#294039"}},rightPriceScale:{borderColor:"#20362f",minimumWidth:80},timeScale:{borderColor:"#20362f",timeVisible,secondsVisible:false,rightOffset:2,barSpacing:9,lockVisibleTimeRangeOnResize:true},handleScale:{axisPressedMouseMove:true},localization:{locale:"en-US"}};}
function timeString(value){if(typeof value==="number"||typeof value==="string")return value;return `${value.year}-${String(value.month).padStart(2,"0")}-${String(value.day).padStart(2,"0")}`;}

class PriceChart {
  constructor(container,overlay,frame){
    this.container=$(container);this.overlay=$(overlay);this.frame=frame;this.tool="pan";this.start=null;this.preview=null;this.data=[];this.drawings=[];this.levels=[];
    this.chart=LightweightCharts.createChart(this.container,chartOptions(this.container.clientHeight,frame==="5m"));
    if(frame==="5m")this.chart.applyOptions({localization:{timeFormatter:t=>nyTime(t)},timeScale:{tickMarkFormatter:t=>nyTime(t),minBarSpacing:.5,rightOffset:1,lockVisibleTimeRangeOnResize:false},handleScroll:false,handleScale:{mouseWheel:false,pinch:false,axisPressedMouseMove:{time:false,price:true}}});
    this.series=this.chart.addCandlestickSeries({upColor:colors.up,downColor:colors.down,borderVisible:false,wickUpColor:colors.up,wickDownColor:colors.down});
    this.series.priceScale().applyOptions({scaleMargins:{top:.1,bottom:.22}});
    this.volume=this.chart.addHistogramSeries({priceFormat:{type:"volume"},priceScaleId:"volume",priceLineVisible:false,lastValueVisible:false});this.chart.priceScale("volume").applyOptions({scaleMargins:{top:.82,bottom:0},visible:false});
    this.mas=frame==="daily"?[10,20,50].map((n,i)=>({n,series:this.chart.addLineSeries({color:[colors.orange,colors.blue,colors.purple][i],lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false})})):[];
    this.chart.timeScale().subscribeVisibleLogicalRangeChange(()=>this.redraw());
    this.chart.subscribeCrosshairMove(param=>{
      if(frame==="daily")updateDailyLegend(param.time?this.data.find(b=>b.time===timeString(param.time)):this.data.find(b=>b.time===state.replay.session.cursor)||this.data.at(-1));
      this.redraw();
    });
    if(frame==="daily")this.container.addEventListener("pointerleave",()=>updateDailyLegend(this.data.find(b=>b.time===state.replay.session.cursor)||this.data.at(-1)));
    if(frame==="daily")this.container.addEventListener("contextmenu",event=>showChartMenu(this,event));
    if(frame==="5m")this.chart.subscribeClick(param=>{if(this.tool==="pan"&&param.time&&!state.busy&&state.intraData)run(()=>selectChartCandle(Number(param.time)));});
    this.overlay.addEventListener("click",event=>this.click(event));
    this.overlay.addEventListener("pointermove",event=>{this.preview=this.point(event);this.redraw();});
    this.observer=new ResizeObserver(()=>{this.chart.applyOptions({width:this.container.clientWidth,height:this.container.clientHeight});if(this.frame==="5m")this.fitSession();this.redraw();});this.observer.observe(this.container);
  }
  setTool(tool){this.tool=tool;this.start=null;this.preview=null;this.overlay.classList.toggle("drawing",tool!=="pan");const tools=$(this.frame==="daily"?"daily-tools":"intraday-tools");for(const button of tools.querySelectorAll("[data-tool]"))button.classList.toggle("selected",button.dataset.tool===tool);this.redraw();}
  fitSession(){if(this.data.length)this.chart.timeScale().setVisibleLogicalRange({from:-2,to:this.data.length+1});}
  point(event){const rect=this.overlay.getBoundingClientRect();const x=event.clientX-rect.left,y=event.clientY-rect.top;if(x>rect.width-this.chart.priceScale("right").width()||y>rect.height-(this.frame==="daily"?0:28))return null;const time=this.chart.timeScale().coordinateToTime(x),price=this.series.coordinateToPrice(y);if(time==null||price==null||price<=0)return null;const key=timeString(time);if(!this.data.some(row=>row.time===key)&&!(this.frame==="daily"&&state.replay.projection_days.includes(key)))return null;const raw=this.frame==="5m"?state.intraData?.bars.find(bar=>ReplayIndicators.intradayBucket(bar.time,state.intraData.interval_minutes)===key):null;return {time:raw?.time??key,price};}
  async click(event){if(this.tool==="pan"||state.busy)return;const point=this.point(event);if(!point)return;if(this.tool!=="horizontal"&&!this.start){this.start=point;this.redraw();return;}const kind=this.tool,points=kind==="horizontal"?[point]:[this.start,point];await run(async()=>{const result=await api(sessionPath("/drawings"),"POST",{frame:this.frame,kind,points,day:this.frame==="5m"?state.intraData.day:undefined,visible_bars:this.frame==="5m"?state.intraData.visible_bars:1,source:this.frame==="5m"?state.intraData.source:"auto",interval_minutes:this.frame==="5m"?state.intraData.interval_minutes:5});setReplay(result,true);this.setTool("pan");toast(kind==="measure"?`Measurement saved: ${pct((points[1].price/points[0].price-1)*100)}`:"Drawing saved to MySQL.");});}
  setData(data,drawings,markers=[],preserve=false){const range=this.chart.timeScale().getVisibleLogicalRange();this.data=data;this.drawings=drawings;this.series.setData([...data.map(({time,open,high,low,close})=>({time,open,high,low,close})),...(this.frame==="daily"?state.replay.projection_days.map(time=>({time})):[])]);this.volume.setData(data.map(row=>({time:row.time,value:row.volume,color:row.close>=row.open?"#39c89455":"#e95b6855"})));for(const ma of this.mas)ma.series.setData(data.filter(row=>row[`ma${ma.n}`]!=null).map(row=>({time:row.time,value:row[`ma${ma.n}`]})));this.series.setMarkers(markers);if(this.frame==="5m")this.fitSession();else if(preserve&&range)this.chart.timeScale().setVisibleLogicalRange(range);else this.chart.timeScale().setVisibleLogicalRange({from:Math.max(-2,data.length-65),to:data.length+12});if(this.frame==="daily"&&!preserve&&state.replay.session.mode==="historical")this.chart.timeScale().setVisibleLogicalRange({from:-2,to:data.length+12});this.redraw();}
  setLevels(orb){for(const level of this.levels)this.series.removePriceLine(level);this.levels=[];if(orb?.available)for(const [price,title,color] of [[orb.entry,"ORB entry",colors.up],[orb.stop,"OR low / stop",colors.down]])this.levels.push(this.series.createPriceLine({price,color,lineWidth:1,lineStyle:2,axisLabelVisible:true,title}));}
  setPaperLevel(paper){if(this.paperLevel)this.series.removePriceLine(this.paperLevel);this.paperLevel=null;const stop=paper?.stop_price||paper?.fills?.findLast(f=>f.side==="buy")?.stop_price;if(stop)this.paperLevel=this.series.createPriceLine({price:stop,color:"#f7a54a",lineWidth:2,lineStyle:2,axisLabelVisible:true,title:"Paper stop"});}
  drawItem(ctx,item,preview=false){const points=item.points;const xy=points.map(point=>({x:this.chart.timeScale().timeToCoordinate(this.frame==="5m"?ReplayIndicators.intradayBucket(point.time,state.intraData?.interval_minutes):point.time),y:this.series.priceToCoordinate(point.price)}));if(xy.some(p=>p.x==null||p.y==null))return;const color=item.kind==="measure"?colors.blue:colors.orange;ctx.strokeStyle=color;ctx.fillStyle=color;ctx.lineWidth=preview?1:1.6;ctx.setLineDash(preview?[5,4]:[]);ctx.beginPath();if(item.kind==="horizontal"){ctx.moveTo(0,xy[0].y);ctx.lineTo(this.container.clientWidth-this.chart.priceScale("right").width(),xy[0].y);}else{ctx.moveTo(xy[0].x,xy[0].y);ctx.lineTo(xy[1].x,xy[1].y);}ctx.stroke();ctx.setLineDash([]);for(const p of xy){ctx.beginPath();ctx.arc(p.x,p.y,2.7,0,Math.PI*2);ctx.fill();}if(item.kind==="measure"){const label=pct((points[1].price/points[0].price-1)*100);ctx.font="11px Consolas";const width=ctx.measureText(label).width+14;const x=Math.max(3,Math.min(xy[1].x-width/2,this.container.clientWidth-width-this.chart.priceScale("right").width())),y=Math.max(20,xy[1].y-10);ctx.fillStyle="#203340";ctx.fillRect(x,y-17,width,22);ctx.fillStyle=color;ctx.fillText(label,x+7,y-2);}}
  redraw(){const dpr=devicePixelRatio||1,w=this.container.clientWidth,h=this.container.clientHeight;if(!w||!h)return;this.overlay.width=Math.round(w*dpr);this.overlay.height=Math.round(h*dpr);const ctx=this.overlay.getContext("2d");ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);ctx.save();ctx.beginPath();ctx.rect(0,0,w-this.chart.priceScale("right").width(),h-(this.frame==="daily"?0:28));ctx.clip();if(this.frame==="daily"&&state.replay?.session.mode==="historical"){const x=this.chart.timeScale().timeToCoordinate(state.replay.session.cursor);if(x!=null){ctx.strokeStyle=colors.blue;ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,h);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle=colors.blue;ctx.font="10px Consolas";ctx.fillText("Selected · "+state.replay.session.cursor,Math.max(5,Math.min(x+6,w-this.chart.priceScale("right").width()-155)),h-8);}}for(const drawing of this.drawings)this.drawItem(ctx,drawing.payload);if(this.start&&this.preview)this.drawItem(ctx,{kind:this.tool,points:[this.start,this.preview]},true);ctx.restore();}
}

function initializeCharts(){
  if(state.daily)return;
  if(!window.LightweightCharts)throw new Error("The local TradingView chart library could not load.");
  state.daily=new PriceChart("daily-chart","daily-overlay","daily");
  state.daily.chart.applyOptions({timeScale:{visible:false}});
  state.rs=LightweightCharts.createChart($("rs-chart"),chartOptions($("rs-chart").clientHeight));
  state.rs.applyOptions({layout:{fontSize:13}});
  state.rsBackground=state.rs.addHistogramSeries({priceScaleId:"regime",priceLineVisible:false,lastValueVisible:false});
  state.rsBackground.priceScale().applyOptions({scaleMargins:{top:0,bottom:0},visible:false});
  const percentFormat={type:"custom",formatter:v=>(v>=0?"+":"")+v.toFixed(1)+"%",minMove:.1};
  state.rsSeries=state.rs.addLineSeries({title:"Relative vs SPY",color:"#95a29f",lineWidth:2,priceLineVisible:false,priceFormat:percentFormat});
  state.rsSeries.priceScale().applyOptions({scaleMargins:{top:.03,bottom:.03}});
  state.rsSeries.createPriceLine({price:0,color:"#94a3b8",lineWidth:1,lineStyle:2,axisLabelVisible:true,title:"SPY baseline"});
  state.rsAverage=state.rs.addLineSeries({title:"Relative SMA 50",color:"#e5e7eb",lineWidth:1,priceLineVisible:false,priceFormat:percentFormat});
  state.ti=LightweightCharts.createChart($("ti-chart"),chartOptions($("ti-chart").clientHeight));
  state.tiSeries=state.ti.addLineSeries({color:colors.up,lineWidth:1,priceLineVisible:false,priceFormat:{type:"price",precision:3,minMove:.001}});
  state.ti.applyOptions({timeScale:{visible:false}});
  for(const [price,color] of [[1.05,"#48684d"],[.95,"#694543"]])state.tiSeries.createPriceLine({price,color,lineWidth:1,lineStyle:2,axisLabelVisible:false});
  let syncing=false;
  const syncRange=(source,range)=>{
    if(syncing||state.updatingCharts||!range)return;
    syncing=true;
    for(const chart of [state.daily.chart,state.rs,state.ti])if(chart!==source)chart.timeScale().setVisibleLogicalRange(range);
    syncing=false;
    requestAnimationFrame(refreshMoveLabels);
  };
  for(const chart of [state.daily.chart,state.rs,state.ti])chart.timeScale().subscribeVisibleLogicalRangeChange(range=>syncRange(chart,range));
  for(const [id,chart] of [["rs-chart",state.rs],["ti-chart",state.ti]]){
    new ResizeObserver(()=>{
      const container=$(id);
      if(!container.clientWidth||!container.clientHeight)return;
      chart.applyOptions({width:container.clientWidth,height:container.clientHeight});
      const labels=container.clientWidth>=700;
      if(id==="rs-chart"){
        refreshMoveLabels();
        state.rsSeries.applyOptions({title:labels?"Relative vs SPY":""});
        state.rsAverage.applyOptions({title:labels?"Relative SMA 50":""});
      }
    }).observe($(id));
  }
  for(const button of $("daily-tools").querySelectorAll("[data-tool]"))button.addEventListener("click",()=>state.daily.setTool(button.dataset.tool));
}
function setChartFocus(focused){
  document.body.classList.toggle("chart-focus",focused);
  $("focus-chart").textContent=focused?"⛶ Restore workspace":"⛶ Focus chart";
  $("focus-chart").setAttribute("aria-pressed",String(focused));
}
function setStudyIndicators(payload){
  const study=ReplayIndicators.relativeStudy(payload.bars);
  const empty=payload.projection_days.map(time=>({time}));
  state.rsBackground.setData([...study.regime,...empty]);
  state.rsSeries.setData([...study.relative,...empty]);
  state.rsMarkers=study.markers;
  refreshMoveLabels();
  state.rsAverage.setData([...study.average,...empty]);
  const last=payload.bars.at(-1);
  state.rsSeries.applyOptions({color:last.rs>0?"#22c55e":last.rs<0?"#ef4444":"#95a29f"});
  const score=payload.metrics.trend_intensity;
  $("chart-trend-score").textContent=score==null?"—":Math.round(score);
  $("chart-trend-label").textContent=score==null?"UNAVAILABLE":score>=90?"STRONG":score>=50?"MODERATE":"WEAK";
  $("chart-trend-context").textContent=`TI65 ${Number(last.ti65).toFixed(3)} · ${last.ti65>1.05?"BULLISH":last.ti65<.95?"BEARISH":"NEUTRAL"}`;
  $("rs-anchor").textContent=`Rebased ${payload.rs_anchor} · tint: TI65 regime`;
  $("rs-value").style.color=last.rs>=0?"#22c55e":"#ef4444";
}
function refreshMoveLabels(){
  if(!state.rsMarkers||!state.rs)return;
  const plotWidth=$("rs-chart").clientWidth-state.rs.priceScale("right").width();
  const previous={aboveBar:null,belowBar:null};
  const markers=state.rsMarkers.slice().reverse().map(marker=>{
    const x=state.rs.timeScale().timeToCoordinate(marker.time);
    const width=marker.text.length*7+5,last=previous[marker.position];
    const show=x!=null&&x>=0&&x<=plotWidth&&(!last||last.x-x>=(last.width+width)/2+3);
    if(show)previous[marker.position]={x,width};
    return {...marker,text:show?marker.text:""};
  }).reverse();
  state.rsSeries.setMarkers(markers);
}
function updateDailyLegend(row){
  if(row)$("ohlc").textContent=`${row.time}  O ${num(row.open)}  H ${num(row.high)}  L ${num(row.low)}  C ${num(row.close)}  ${pct(row.change_pct)}`;
}
function metric(label,value,positive=null){return `<div class="metric"><label>${label}</label><strong class="${positive===null?"":positive?"positive":"negative"}">${value}</strong></div>`;}
function dailyMarkers(payload){
  const markers=[];
  for(const review of payload.reviews){
    if(review.decision==="expected")markers.push({time:review.day,position:"aboveBar",color:colors.orange,shape:"circle",text:review.expected_day===review.day?"Expected day":"Expected → "+review.expected_day});
    if(review.outcome==="actual")markers.push({time:review.day,position:"belowBar",color:colors.blue,shape:"arrowUp",text:"Actual breakout"});
    if(review.outcome==="failed")markers.push({time:review.day,position:"aboveBar",color:colors.down,shape:"square",text:"No breakout"});
  }
  markers.push(...ReplayIndicators.paperMarkers({...payload.paper,fills:payload.paper.chart_fills},null,true));return markers.sort((a,b)=>a.time.localeCompare(b.time));
}
function setReplay(payload,preserve=false){updateStockReview(payload);state.replay=payload;updateReplayModeUI(payload);state.dirty=false;$("dirty-label").textContent="";$("welcome").hidden=true;$("replay-content").hidden=false;initializeCharts();$("active-symbol").textContent=payload.session.symbol;$("active-date").textContent=dateText(payload.session.cursor);$("day-position").textContent=`SESSION ${payload.index+1} / ${payload.total}`;const m=payload.metrics;$("daily-paper-summary").textContent=`${payload.paper.study_label} · NAV ${money(payload.paper.nav)} | cash ${money(payload.paper.cash)} | ${payload.paper.quantity} shares | realized ${money(payload.paper.realized_pnl)}${payload.paper.pending?" | Stop check pending on "+payload.paper.pending.day:""}`;$("metrics").innerHTML=metric("CLOSE",`$${num(m.price)}`)+metric(payload.session.setup_name==="Setup 2"?"ADR / HIGH-LOW":"ADR · 20D",pct(studyAdr(m)))+metric("1M GROWTH",pct(m.return_1m),m.return_1m>=0)+metric("3M GROWTH",pct(m.return_3m),m.return_3m>=0)+metric("TI65",num(m.ti65),m.ti65>=1.05);state.updatingCharts=true;state.daily.setData(payload.bars,payload.drawings.filter(d=>d.frame==="daily"),dailyMarkers(payload),preserve);setStudyIndicators(payload);state.tiSeries.setData([...payload.bars.map(b=>({time:b.time,value:b.ti65})),...payload.projection_days.map(time=>({time}))]);state.updatingCharts=false;const range=state.daily.chart.timeScale().getVisibleLogicalRange();if(range){state.rs.timeScale().setVisibleLogicalRange(range);state.ti.timeScale().setVisibleLogicalRange(range);}const last=payload.bars.find(b=>b.time===payload.session.cursor)||payload.bars.at(-1);updateDailyLegend(last);$("rs-value").textContent=pct(last.rs);$("ti-value").textContent=Number(last.ti65).toFixed(3);$("coverage-label").textContent=`${payload.reviewed_count} / ${payload.total} sessions reviewed`;$("coverage-progress").style.width=`${100*payload.reviewed_count/payload.total}%`;const review=payload.current_review;$("pass-button").innerHTML=payload.has_next?"Pass & next session <span>→</span>":"Pass & finish";$("notes").value=review?.notes||"";$("pattern").value=review?.pattern||"";$("expected-entry").hidden=!payload.entry_preview_day;$("expect-date").textContent=`For ${dateText(payload.next_expected_day)}`;$("expected-day-date").value=payload.session.cursor;$("expected-day-date").min=state.status?.study_start||"2024-01-01";$("expected-day-date").max=payload.latest_day;$("decision-label").textContent=review?(review.decision==="expected"?"◇ Expected breakout recorded":"✓ Pass recorded"):"No decision recorded";if(review?.metrics.retrospective_revision)$("decision-label").textContent+=" · retrospective revision";$("outcome-label").textContent=review?.outcome==="actual"?"Actual breakout marked on this session":review?.outcome==="failed"?"No breakout marked on this session":"Not assessed";$("actual-button").classList.toggle("selected",review?.outcome==="actual");$("failed-button").classList.toggle("selected",review?.outcome==="failed");$("save-state").textContent="● SAVED TO MYSQL";$("chart-hint").textContent=(payload.session.mode==="historical"?"Historical study · select a day or right-click for intraday.":payload.can_predict?"Right arrow saves before revealing the next.":"Previously revealed · revisions are logged.")+` ${payload.daily_preload.ahead} daily sessions preloaded.`;const important=payload.reviews.filter(r=>r.decision==="expected"||r.outcome);$("saved-total").textContent=important.length;$("review-timeline").innerHTML=important.slice(-12).reverse().map(r=>`<div class="timeline-item"><small>${esc(r.day)}</small>${r.decision==="expected"?tag("expected","Expected → "+r.expected_day):""} ${r.outcome?tag(r.outcome,r.outcome==="actual"?"Actual breakout":"No breakout"):""}${r.metrics.retrospective_revision?tag("revision","Retrospective"):""}<br><button data-intra-day="${esc(r.expected_day&&(payload.session.mode==="historical"||r.expected_day<=payload.session.cursor||r.expected_day===payload.entry_preview_day)?r.expected_day:r.day)}">Study 5m ↗</button></div>`).join("")+payload.snapshots.slice(-4).reverse().map(s=>`<div class="timeline-item"><small>${esc(s.day)} · ${esc(s.frame)}</small><a href="/api/snapshots/${esc(s.id)}" target="_blank">Snapshot ↗</a></div>`).join("")||'<p class="muted">No setup markers yet. Your decisions are saved session by session.</p>';for(const button of $("review-timeline").querySelectorAll("[data-intra-day]"))button.addEventListener("click",()=>openIntraday(button.dataset.intraDay));if(state.intraday&&state.intraData){if(payload.study_drawings)state.intraData.drawings=payload.study_drawings;state.intraday.drawings=intradayDrawings(payload,state.intraData);state.intraday.redraw();}renderStocks();updateButtons();}
function updateReplayModeUI(payload){
  const historical=payload.session.mode==="historical";
  for(const mode of ["backtesting","historical"]){const button=$("mode-"+mode);button.setAttribute("aria-pressed",String(payload.session.mode===mode));}
  const badge=document.querySelector(".blind-pill");badge.textContent=historical?"ALL HISTORY VISIBLE":"FUTURE HIDDEN";badge.classList.toggle("historical",historical);
  $("daily-study-date-label").hidden=!historical;$("daily-study-date").value=payload.session.cursor;$("daily-study-date").min=payload.daily_days[0];$("daily-study-date").max=payload.latest_day;
  $("replay-mode-note").textContent=historical?`All daily candles through ${dateText(payload.chart_through)}. Metrics and notes follow the selected day.`:`Daily candles and intraday dates after ${dateText(payload.session.cursor)} are hidden.`;
  $("next-day").title=historical?"Select the next session":"Save this day and reveal the next (right arrow)";
}
async function changeReplayMode(mode){
  hideChartMenu();await run(async()=>{if(state.dirty)await saveReview();setReplay(await api(sessionPath("/mode"),"POST",{mode}));try{localStorage.setItem("breakout-lab-chart-mode",mode);}catch{}toast(mode==="historical"?"Historical study · all available candles shown.":"Backtesting · future candles hidden. Earlier reveals stay recorded.");});
}
function clearReplay(){setChartFocus(false);state.replay=null;state.intraData=null;$("replay-content").hidden=true;$("welcome").hidden=false;$("expected-entry").hidden=true;$("notes").value="";$("pattern").value="";$("expect-date").textContent="Select a stock to begin.";$("decision-label").textContent="No decision recorded";$("outcome-label").textContent="Not assessed";$("review-timeline").innerHTML="";$("save-state").textContent="DATABASE READY";updateButtons();}
function updateButtons(){const r=state.replay;$("scanner-setup").disabled=state.busy||!!state.scanRunning;for(const id of ["intraday-day","intraday-date","intraday-source","intraday-interval","load-intraday"])$(id).disabled=state.busy;$("focus-expected").disabled=!r||state.busy;$("focus-pass").disabled=!r||state.busy;for(const id of ["mode-backtesting","mode-historical","daily-study-date","expected-entry","pass-button","save-notes","actual-button","failed-button","pattern","notes","expected-day-date","mark-expected-day"])$(id).disabled=!r||state.busy;$("expected-button").disabled=!r||state.busy;$("previous-day").disabled=!r||!r.has_previous||state.busy;$("next-day").disabled=!r||!r.has_next||state.busy;$("notes").disabled=!r||state.busy;$("pattern").disabled=!r||state.busy;$("save-notes").disabled=!r||state.busy;$("undo-drawing").disabled=!r||!r.drawings.some(d=>d.frame==="daily");if(state.intraData){$("intraday-undo").disabled=state.busy||!intradayDrawings(r,state.intraData).length;}updatePaperPlan();}
async function saveReview(decision=state.replay?.current_review?.decision||"pass",outcome=state.replay?.current_review?.outcome||null){const payload=await api(sessionPath("/review"),"POST",{decision,outcome,pattern:$("pattern").value,notes:$("notes").value});setReplay(payload,true);}
async function step(direction){if(!state.replay)return;await run(async()=>{if(state.dirty)await saveReview();setReplay(await api(sessionPath("/step"),"POST",{direction}));});}
function renderStocks(){const filter=$("symbol-filter").value.trim().toUpperCase(),status=$("status-filter").value;const rows=state.rows.filter(row=>row.symbol.includes(filter)&&(status==="all"||status==="pending"&&!row.reviewed||status==="reviewed"&&row.reviewed||status==="expected"&&row.decision==="expected"||status==="actual"&&row.outcome==="actual"||status==="complete"&&row.complete));$("stock-rows").innerHTML=rows.map(row=>{const label=(row.outcome?tag(row.outcome,row.outcome==="actual"?"Actual":"No BO"):row.reviewed?tag(row.decision,row.decision==="expected"?"Expected":"Pass"):tag("pending","To review"))+(row.complete?" "+tag("complete","Complete"):"");return `<tr tabindex="0" data-symbol="${esc(row.symbol)}" class="${state.replay?.session.symbol===row.symbol?"selected":""}" aria-label="Replay ${esc(row.symbol)}"><td>${esc(row.symbol)}<small>${row.coverage.reviewed}/${row.coverage.total} sessions</small></td><td class="${row.metrics.return_1m>=0?"positive":"negative"}">${Number(row.metrics.return_1m).toFixed(1)}</td><td>${num(studyAdr(row.metrics,state.scanSetup))}${state.scanSetup==="Setup 2"&&row.metrics.adr_20_high_low>=5?tag("fast","Fast"):""}</td><td>${label}</td></tr>`;}).join("");$("scan-empty").hidden=rows.length>0;$("scan-empty").textContent=state.rows.length?"No stocks match this filter.":state.scanRunning?"Preparing your historical scan...":state.scanId?`No ${(state.scanSetup||"Setup 1").replace("Setup","Scanner")} matches for this session.`:"Run a scan to build your historical review queue.";$("match-count").textContent=state.rows.length;$("review-count").textContent=`${state.rows.filter(r=>r.reviewed).length} reviewed`;for(const row of $("stock-rows").querySelectorAll("[data-symbol]")){const open=()=>run(async()=>{if(state.dirty)await saveReview();setReplay(await api("/api/sessions","POST",{scan_id:state.scanId,symbol:row.dataset.symbol,mode:state.replay?.session.mode||preferredReplayMode()}));toast("Resumed at the first unreviewed session.");});row.addEventListener("click",open);row.addEventListener("keydown",e=>{if(e.key==="Enter")open();});}}
async function refreshScan(poll=true){if(!state.scanId)return;const requestedId=state.scanId;const data=await api(`/api/scans/${requestedId}`);if(state.scanId!==requestedId)return;$("scan-history").value=requestedId;state.rows=data.results;const scan=data.scan,p=scan.payload;state.scanSetup=scan.setup_name||"Setup 1";state.scanRules=p.rules;$("scanner-setup").value=state.scanSetup;renderScannerRules(p.rules);$("scanner-setup").disabled=scan.status==="running"||state.busy;$("scan-date").value=scan.requested_day;state.scanRunning=scan.status==="running";$("scan-status").textContent=scan.status==="running"?`${p.message} · ${p.processed.toLocaleString()} / ${p.total.toLocaleString()}`:scan.status!=="complete"?p.message:`${scan.scan_day} · ${p.eligible?.toLocaleString()} eligible / ${p.total?.toLocaleString()} cached symbols${scan.requested_day!==scan.scan_day?" · shifted to first cached session on/after your date":""}`;$("scan-progress").style.width=`${p.total?100*p.processed/p.total:0}%`;$("run-scan").disabled=scan.status==="running";renderStocks();if(scan.status==="running"&&poll){clearTimeout(state.poll);state.poll=setTimeout(pollScan,1600);}else if(scan.status!=="running"){if(p.prepared){const select=$("scan-history");if(!Array.from(select.options).some(option=>option.value===scan.id)){const option=new Option(`${scan.scan_day} / ${state.scanSetup.replace("Setup","Scanner")} / complete`,scan.id);select.add(option,1);}select.value=scan.id;}else await loadStatus();}}
async function pollScan(){try{await refreshScan();}catch(e){toast(e.message,true);state.poll=setTimeout(pollScan,4000);}}
function renderScannerRules(rules=null){
  const name=$("scanner-setup").value, study=name==="Setup 2";
  $("scanner-badge").textContent=name.replace("Setup","SCANNER");
  $("scanner-rules-title").textContent=name.replace("Setup","Scanner")+" conditions";
  const labels={volume:"Daily volume",dollar_volume:"Dollar volume",adr_20:"ADR (20d)",growth_rank_1m:"1M growth rank",trend_intensity:"Trend intensity",adr_20_high_low:"ADR (20d High/Low)",avg_dollar_volume_20d:"Average dollar volume (20d)",momentum_rank_any:"Best 1 / 3 / 6M percentile"};
  const selectedRules=rules||state.status?.scanner_setups?.[name]?.rules||state.status?.rules||[];
  $("rules").innerHTML=selectedRules.map(r=>`<div class="rule"><span>${esc(labels[r.attribute]||r.attribute)}</span><strong>${esc(r.operator)} ${Number(r.threshold).toLocaleString()}</strong></div>`).join("");
  $("scanner-rule-note").textContent=study?"Top 2% in ANY of 1, 3 or 6 months, ranked before filters. ADR uses 20 complete High/Low sessions. ADR >= 5% is tagged Fast. TI65 and strength versus SPY are display only; review consolidation manually.":"Calculated at the close of the selected session. Growth rank is cross-sectional.";
}
function studyAdr(metrics,setup=state.replay?.session.setup_name||state.scanSetup){return setup==="Setup 2"?metrics.adr_20_high_low:metrics.adr_20;}
async function loadStatus(){
  const s=await api("/api/status");state.status=s;$("global-error").hidden=true;
  const chosen=$("scanner-setup").value;
  $("scanner-setup").innerHTML=Object.keys(s.scanner_setups).map(name=>`<option value="${esc(name)}">${esc(name.replace("Setup","Scanner"))}${name==="Setup 2"?" - leadership study":""}</option>`).join("");
  $("scanner-setup").value=chosen||s.default_setup||"Setup 2";renderScannerRules(state.scanRules);
  $("cache-summary").textContent=`${s.database} / ${s.bounds.symbols.toLocaleString()} stocks / through ${s.bounds.last?.slice(0,10)}`;
  $("scan-date").min=s.study_start;$("scan-date").max=s.latest_completed_session;
  $("provider-status").textContent=`Alpaca: ${s.providers.alpaca_sip?"connected":"keys required"} / Massive: ${s.providers.massive?"connected":"keys required"} / client cap ${s.alpaca_client_rpm}/${s.alpaca_plan_rpm} requests/min`;
  $("source-dot").style.color=s.providers.alpaca_sip||s.providers.massive?colors.up:colors.orange;
  const selected=$("scan-history").value;
  $("scan-history").innerHTML='<option value="">Choose a saved scan...</option>'+s.scans.map(scan=>`<option value="${esc(scan.id)}">${esc(scan.scan_day||scan.requested_day)} / ${esc((scan.setup_name||"Setup 1").replace("Setup","Scanner"))} / ${esc(scan.status)}</option>`).join("");
  $("scan-history").value=selected||state.scanId||"";return s;
}

async function openIntraday(day){if(!state.replay||state.busy)return;if(day&&!state.replay.intraday_days.includes(day)){toast("Advance the replay to this day or switch to Historical study.",true);return;}if(state.dirty){await run(()=>saveReview());if(state.dirty)return;}state.paperStudyId=null;$("intraday-title").textContent=state.replay.session.symbol+" · "+dateText(day||state.replay.session.cursor);const expected=new Set(state.replay.reviews.map(r=>r.expected_day).filter(Boolean));$("intraday-day").innerHTML=state.replay.intraday_days.slice().reverse().map(stamp=>`<option value="${stamp}">${stamp}${expected.has(stamp)?" · expected":""}${state.replay.reviews.some(r=>r.day===stamp&&r.outcome==="actual")?" · actual":""}${stamp>state.replay.session.cursor?" · hindsight":""}</option>`).join("");$("intraday-day").value=day||state.replay.session.cursor;$("intraday-date").min=state.replay.intraday_days[0];$("intraday-date").max=state.replay.intraday_days.at(-1);$("intraday-date").value=$("intraday-day").value;$("intraday-study-mode").textContent=`Choose an available session · selected daily day stays on ${dateText(state.replay.session.cursor)}`;$("intraday-dialog").showModal();if(!state.intraday){state.intraday=new PriceChart("intraday-chart","intraday-overlay","5m");for(const button of $("intraday-tools").querySelectorAll("[data-tool]"))button.addEventListener("click",()=>state.intraday.setTool(button.dataset.tool));}state.intraData=null;state.paperAt=null;$("opening-moves").innerHTML="";$("paper-summary").innerHTML="";$("paper-ledger").innerHTML="";$("paper-warning").hidden=true;state.intraday.setData([],[]);state.intraday.setPaperLevel(null);$("orb-metrics").innerHTML="";$("intraday-error").hidden=true;loadIntraday();}
function intradayDrawings(payload,data){return (data.drawings||payload.drawings).filter(d=>d.frame==="5m"&&(d.payload.session_day||d.day)===data.day&&(!d.payload.intraday_asof||d.payload.intraday_asof<=data.bars.at(-1)?.time));}
function updateStockReview(payload){
  const row=state.rows.find(item=>item.symbol===payload.session.symbol);
  if(!row)return;
  row.coverage={...row.coverage,reviewed:payload.reviewed_count,total:payload.total};
  row.complete=payload.total>0&&payload.reviewed_count===payload.total;
  row.reviewed=!!payload.scan_review;
  row.decision=payload.scan_review?.decision||null;
  row.outcome=payload.scan_review?.outcome||null;
}
function orbChip(label,value,good=null,tone=""){
  return `<div class="orb-chip"><label>${label}</label><strong class="${tone||(good===null?"":good?"positive":"negative")}">${value}</strong></div>`;
}
const money=value=>new Intl.NumberFormat("en-US",{style:"currency",currency:"USD"}).format(value||0);
function paperChartMarkers(data){return ReplayIndicators.intradayChartMarkers(data.paper?.fills?.length?
  ReplayIndicators.paperMarkers(data.paper,data.day):ReplayIndicators.intradayMarkers(data.orb),data.interval_minutes);}
async function selectChartCandle(stamp){
  const data=state.intraData;
  const bars=data.bars.filter(bar=>ReplayIndicators.intradayBucket(bar.time,data.interval_minutes)===stamp);
  const chosen=bars.at(-1);
  if(chosen)await selectPaperCandle(chosen.time);
}
function renderOpeningMoves(data){
  $("opening-moves").innerHTML=[5,30].map(minutes=>{
    const window=data.opening_moves.windows[String(minutes)], end=minutes===5?"09:35":"10:00";
    return `<div class="opening-move"><label>FIRST ${minutes} MIN · ${end} NY</label><strong class="${window.available?(window.close_pct>=0?"positive":"negative"):""}">${window.available?pct(window.close_pct):"Unavailable"}</strong><span>${window.available?`Peak ${pct(window.high_pct)} · close ${money(window.close)}`:esc(window.reason)}</span></div>`;
  }).join("")+`<p>Gain from the 09:30 session open. Peak shows the highest gain inside each window. ${data.interval_minutes}-minute entry range ends at ${data.interval_minutes===5?"09:35":"10:00"} NY; fills and stops use 5-minute bars.</p>`;
}
function updatePaperPlan(){
  const book=state.paperAt, stamp=Number($("paper-time").value), price=Number($("paper-price").value), percent=Number($("paper-stop").value);
  const stop=price*(1-percent/100), risk=book?(book.cash+book.quantity*price)*.01:0;
  const remaining=Math.max(0,risk-(book?.quantity||0)*Math.max(0,(book?.average_price||0)-stop));
  const shares=price>0&&stop>0&&stop<price?Math.max(0,Math.floor(Math.min((book?.cash||0)/price,remaining/(price-stop))+1e-9)):0;
  const locked=book?.latest_action_time&&stamp<book.latest_action_time;
  const closed=!!(book?.fills.length&&!book.quantity&&!book.pending);
  const loading=!book||state.busy||book.pending||book.read_only||!(price>0);
  $("paper-new-study").disabled=state.busy||!state.intraData;$("paper-study").disabled=state.busy||!state.intraData;
  const changedStop=book?.quantity&&Math.abs(stop-book.stop_price)>.000001;
  const beforeOpening=stamp+300<Date.parse(state.intraData?.orb.opening_end)/1000;
  $("paper-plan").textContent=book?.read_only?"Saved earlier account · start a new study to trade":closed?"Study closed · start a new $10,000 study for another entry":locked?"Earlier candle: NAV review only":book?.pending?`Stop check pending: ${book.pending.day}`:changedStop?"Additional buys keep the current stop":beforeOpening?`${state.intraData.interval_minutes}m opening window has not completed`:`Risk ${money(risk)} · buy ${shares} shares / ${money(shares*price)}`;
  $("paper-buy").disabled=!!(loading||locked||closed||changedStop||beforeOpening||shares<1);
  $("paper-sell-partial").disabled=!!(loading||locked||!book?.quantity||!(Number($("paper-partial").value)>0&&Number($("paper-partial").value)<100));
  $("paper-sell-all").disabled=!!(loading||locked||!book?.quantity);
}
function renderPaper(book){
  state.paperAt=book;
  const field=(label,value,tone="")=>`<div><label>${label}</label><strong class="${tone}">${value}</strong></div>`;
  $("paper-summary").innerHTML=field(`NAV · ${nyTime(Number($("paper-time").value))} NY`,money(book.nav))+
    field("CASH",money(book.cash))+field("SHARES",book.quantity)+field("AVG BUY",book.average_price==null?"—":money(book.average_price))+
    field("REALIZED P&L",money(book.realized_pnl),book.realized_pnl>=0?"positive":"negative")+
    field("OPEN P&L",money(book.unrealized_pnl),book.unrealized_pnl>=0?"positive":"negative")+field("1% RISK",money(book.risk_budget));
  const full=state.intraData.paper;
  $("paper-ledger").innerHTML=full.fills.slice().reverse().map(fill=>`<div class="paper-fill"><span>${esc(fill.day)} ${nyTime(fill.time)}</span><span>${esc(fill.reason.replaceAll("_"," "))} · ${fill.shares} @ ${money(fill.price)}</span><span>P&L ${money(fill.realized_pnl)}</span><span>NAV ${money(fill.nav_after)}</span></div>`).join("")+
    full.equity_curve.slice().reverse().map(point=>`<div class="paper-fill"><span>${esc(point.day)} close</span><span>Session NAV ${money(point.nav)}</span><span>Cash ${money(point.cash)}</span></div>`).join("")||"No paper trades yet. Choose a candle and fill price, then Buy.";
  const warning=book.pending?`Stop check unresolved on ${book.pending.day}: ${book.pending.reason}. NAV uses the last verified price.`:book.warnings.join(" ");
  $("paper-warning").textContent=warning;
  $("paper-warning").hidden=!warning;
  updatePaperPlan();
}
async function selectPaperCandle(stamp,price=null){
  const data=state.intraData, bar=data?.bars.find(b=>b.time===stamp);
  if(!bar)return;
  $("paper-time").value=String(stamp);
  $("paper-price").value=Number(price??bar.close).toFixed(4);
  state.paperAt=null;
  updatePaperPlan();
  const book=await api(sessionPath(`/paper?day=${data.day}&time=${stamp}&source=${encodeURIComponent(data.source)}${paperStudyQuery()}`));
  const fill=Number($("paper-price").value);
  if(book.stop_price)$("paper-stop").value=String(Number(((fill-book.stop_price)/fill*100).toFixed(8)));
  renderPaper(book);
}
function paperStudyQuery(){return state.paperStudyId?`&study_id=${encodeURIComponent(state.paperStudyId)}`:"";}
function renderPaperStudies(data){
  state.paperStudyId=data.paper.study_id;
  $("paper-study").innerHTML='<option value="">Auto · open trade or fresh $10,000</option>'+data.paper.studies.map(study=>`<option value="${esc(study.id)}">${esc(study.label)}</option>`).join("");
  $("paper-study").value=state.paperStudyId||"";
}
async function setupPaper(data){
  renderPaperStudies(data);
  $("paper-time").innerHTML=data.bars.map(bar=>`<option value="${bar.time}">${nyTime(bar.time)} NY</option>`).join("");
  const noTrades=!data.paper.fills.length;
  const trigger=data.orb.entry_taken?Date.parse(data.orb.entry_time)/1000:null;
  const stamp=noTrades&&trigger?trigger:data.bars.at(-1)?.time;
  $("paper-stop").value=noTrades&&data.orb.available?String(Number(Number(data.orb.sl_pct).toFixed(8))):"5";
  state.intraday.setPaperLevel(data.paper);
  if(stamp)await selectPaperCandle(stamp,noTrades&&trigger?data.orb.entry:null);
}
async function paperTrade(action){
  await run(async()=>{
    const time=Number($("paper-time").value), price=Number($("paper-price").value);
    const data=await api(sessionPath("/paper/trades"),"POST",{request_id:crypto.randomUUID(),day:state.intraData.day,time,
      action,price,stop_pct:Number($("paper-stop").value),percent:Number($("paper-partial").value),source:state.intraData.source,
      interval_minutes:state.intraData.interval_minutes,study_id:state.paperStudyId});
    state.intraData=data;
    renderPaperStudies(data);
    state.intraday.series.setMarkers(paperChartMarkers(data));
    state.intraday.setPaperLevel(data.paper);
    await selectPaperCandle(time,price);
    setReplay(await api(sessionPath()),true);
    toast(`Paper ${action.replaceAll("_"," ")} saved · session NAV ${money(data.paper.nav)}`);
  });
}
async function loadIntraday(){
  await run(async()=>{
    try{
      $("intraday-error").hidden=true;
      $("intraday-provenance").textContent="Loading historical bars…";
      const day=$("intraday-day").value;
      const data=await api(sessionPath(`/intraday?day=${encodeURIComponent(day)}&source=${encodeURIComponent($("intraday-source").value)}&interval_minutes=${$("intraday-interval").value}${paperStudyQuery()}`));
      state.intraData=data;
      if(data.entry_preview||data.hindsight)setReplay(await api(sessionPath()),true);
      $("intraday-title").textContent=state.replay.session.symbol+" · "+dateText(day);
      $("intraday-provenance").textContent=`${data.source} · ${data.cached?"MySQL archive":"downloaded & archived"}`;
      $("intraday-date").value=day;
      $("intraday-study-mode").textContent=data.mode==="historical"?
        `Historical study · all daily history visible · selected daily day stays on ${dateText(data.daily_asof)}`:
        `${data.hindsight?"Earlier session study":"Backtesting session study"} · daily replay stays on ${dateText(data.daily_asof)}`;
      $("intraday-study-mode").classList.toggle("hindsight",data.hindsight);
      const orb=data.orb;
      renderOpeningMoves(data);
      $("intraday-eyebrow").textContent=`ENTRY-DAY STUDY / ${data.interval_minutes} MINUTE CANDLES`;
      state.intraday.setTool("pan");
      state.intraday.setData(ReplayIndicators.intradayCandles(data.bars,data.interval_minutes),intradayDrawings(state.replay,data),paperChartMarkers(data));
      state.intraday.setLevels(orb);
      const stopLabel=orb.stop_hit?nyTime(Date.parse(orb.stop_time)/1000)+" NY":orb.uncertain_stop_time?"Order unclear":orb.entry_taken?"Not hit":"No trade";
      $("orb-metrics").innerHTML=orb.available?
        orbChip("ORB LEVEL",`$${num(orb.entry)}`)+
        orbChip("OPENING LOW / STOP",`$${num(orb.stop)}`)+
        orbChip("STOP DISTANCE",pct(orb.sl_pct))+
        orbChip("STOP / PRIOR ADR",`${num(orb.stop_adr_pct)}%`,null,ReplayIndicators.stopAdrTone(orb.stop_adr_pct,orb.stop_valid))+
        orbChip("STOP CHECK",orb.stop_valid===null?"ADR unavailable":orb.stop_valid?"Within bounds":"Outside bounds",orb.stop_valid)+
        orbChip("ORB ENTRY",orb.entry_taken?nyTime(Date.parse(orb.entry_time)/1000)+" NY":"No entry",orb.entry_taken?true:null)+
        orbChip("ORB STOP",stopLabel,null,orb.stop_hit?"negative":orb.uncertain_stop_time?"warning":orb.entry_taken?"positive":""):
        orbChip("ORB UNAVAILABLE",orb.reason);
      $("intraday-position").textContent=`Full session · ${state.intraday.data.length} × ${data.interval_minutes}m candles · ${data.total_bars} source 5m bars · 09:30–${data.bars.length?nyTime(data.bars.at(-1).time+300)+" NY":"No bars"}`;
      const status=orb.entry_taken?orb.stop_hit?"Entry taken; stop loss hit.":orb.uncertain_stop_time?"Entry and stop touched in the same 5-minute candle; order is unclear.":"Entry taken; no stop hit observed.":`No entry: ${orb.no_entry_reason||orb.reason}.`;
      $("orb-note").textContent=orb.available?`${data.interval_minutes}m ORB: ${status} Stop/ADR range ${orb.min_stop_adr_pct}–${orb.max_stop_adr_pct}%, using previous-session ADR ${pct(data.adr)}. ORB check excludes capital.${orb.complete?"":" Session has missing 5-minute bars; results cover available bars only."}`:orb.reason;
      await setupPaper(data);
      updateButtons();
    }catch(e){
      state.intraData=null;
      state.paperAt=null;
      state.intraday.setData([],[]);
      state.intraday.setLevels(null);
      state.intraday.setPaperLevel(null);
      $("orb-metrics").innerHTML="";
      $("opening-moves").innerHTML="";
      $("intraday-position").textContent="Session unavailable";
      $("orb-note").textContent="";
      $("intraday-error").textContent=e.message;
      $("intraday-error").hidden=false;
      $("intraday-provenance").textContent="Data unavailable";
      throw e;
    }
  });
}

async function snapshot(frame="daily"){if(!state.replay)return;await run(async()=>{if(state.dirty)await saveReview();const intra=frame==="5m",target=intra?state.intraday:state.daily;if(intra&&!state.intraData)throw new Error("Load an intraday session first.");target.redraw();const width=1400,chartHeight=intra?450:550,extra=intra?145:355,canvas=document.createElement("canvas");canvas.width=width;canvas.height=145+chartHeight+extra+125;const ctx=canvas.getContext("2d");ctx.fillStyle=colors.bg;ctx.fillRect(0,0,width,canvas.height);ctx.fillStyle=colors.up;ctx.font="18px Consolas";ctx.fillText("BREAKOUT LAB / HISTORICAL REPLAY",36,37);ctx.fillStyle="#e1ece7";ctx.font="27px Consolas";const day=intra?state.intraData.day:state.replay.session.cursor;ctx.fillText(`${state.replay.session.symbol} · ${day} · ${intra?state.intraData.interval_minutes+" MIN":"1 DAY"}`,36,79);ctx.fillStyle=colors.text;ctx.font="13px Consolas";const m=state.replay.metrics;ctx.fillText(intra?`${state.intraData.source} · ${state.intraData.interval_minutes}m chart / ${state.intraData.total_bars} source 5m bars · prior ADR ${pct(state.intraData.adr)}`:`ADR ${pct(studyAdr(m))}   1M ${pct(m.return_1m)}   3M ${pct(m.return_3m)}   TI65 ${num(m.ti65)}`,36,111);ctx.drawImage(target.chart.takeScreenshot(),28,140,width-56,chartHeight);ctx.drawImage(target.overlay,28,140,width-56,chartHeight);let y=140+chartHeight;if(!intra){for(const [label,chart,height] of [["RELATIVE vs SPY / SMA 50 / 4% DAYS / TI65 REGIME",state.rs,190],["TI65 · 7 MA / 65 MA",state.ti,85]]){ctx.fillStyle=colors.text;ctx.font="12px Consolas";ctx.fillText(label,36,y+21);ctx.drawImage(chart.takeScreenshot(),28,y+28,width-56,height);y+=height+35;}}else{const o=state.intraData.orb;ctx.fillStyle=colors.text;ctx.font="13px Consolas";ctx.fillText(o.available?`ORB entry $${num(o.entry)}  Stop $${num(o.stop)}  SL ${pct(o.sl_pct)}  Stop/ADR ${num(o.stop_adr_pct)}%  ${o.stop_valid?"PASS":"FAIL"}  ${o.entry_taken?"Entry "+nyTime(Date.parse(o.entry_time)/1000)+" NY":"No entry"}  ${o.stop_hit?"Stop hit "+nyTime(Date.parse(o.stop_time)/1000)+" NY":o.uncertain_stop_time?"Entry / stop order unclear":o.entry_taken?"No stop hit observed":"No trade"}`:o.reason,36,y+35);const p=state.intraData.paper;ctx.fillText(`${p.study_label} · NAV ${money(p.nav)} | cash ${money(p.cash)} | shares ${p.quantity} | realized ${money(p.realized_pnl)} | 1% NAV risk`,36,y+57);const moves=state.intraData.opening_moves.windows;ctx.fillText([5,30].map(minutes=>`${minutes}m from open: ${moves[minutes].available?pct(moves[minutes].close_pct)+" / peak "+pct(moves[minutes].high_pct):"unavailable"}`).join(" | "),36,y+79);y+=115;}const capturedReview=intra?(state.intraData.entry_preview?state.replay.current_review:state.intraData.review):state.replay.current_review;ctx.fillStyle=colors.orange;ctx.font="13px Consolas";ctx.fillText(`Decision: ${capturedReview?.decision||"unreviewed"} · outcome: ${capturedReview?.outcome||"not assessed"} · shape: ${capturedReview?.pattern||"unclassified"}`,36,y+35);ctx.fillStyle=colors.text;ctx.font="12px Segoe UI";const text=(intra?capturedReview?.notes||"":$("notes").value).slice(0,450),lines=text.match(/.{1,170}(?:\s|$)|.{1,170}/g)||[];lines.slice(0,3).forEach((line,i)=>ctx.fillText(line.trim(),36,y+62+i*19));ctx.fillText(`Charts by TradingView \u00b7 www.tradingview.com \u00b7 ${intra?"full intraday session":state.replay.session.mode==="historical"?"all daily history visible":"future daily candles hidden"} at capture`,36,canvas.height-18);const saved=await api(sessionPath("/snapshots"),"POST",{data_url:canvas.toDataURL("image/png"),frame,day,visible_bars:intra?state.intraData.visible_bars:1,source:intra?state.intraData.source:"auto",interval_minutes:intra?state.intraData.interval_minutes:5,study_id:intra?state.paperStudyId:null});setReplay(await api(sessionPath()),true);toast(`Snapshot saved: ${saved.path}`);});}

async function loadLibrary(){state.library=await api("/api/library");renderLibrary();}
function renderLibrary(){const data=state.library;if(!data)return;const rows=data.reviews,filter=$("library-filter").value.toLowerCase(),status=$("library-status").value;$("library-count").textContent=rows.length||"";$("library-stats").innerHTML=[[rows.length,"SESSIONS REVIEWED"],[new Set(rows.map(r=>r.symbol)).size,"STOCKS STUDIED"],[rows.filter(r=>r.decision==="expected").length,"EXPECTED SETUPS"],[rows.filter(r=>r.outcome==="actual").length,"ACTUAL BREAKOUTS"]].map(([value,label])=>`<div class="stat-card"><strong>${value}</strong><span>${label}</span></div>`).join("");const visible=rows.filter(r=>[r.symbol,r.pattern,r.notes].join(" ").toLowerCase().includes(filter)&&(status==="all"||status==="expected"&&r.decision==="expected"||status==="pass"&&r.decision==="pass"||status===r.outcome));$("library-rows").innerHTML=visible.slice().reverse().map(r=>`<tr><td>${esc(r.symbol)}<small>${esc(r.day)}</small></td><td>${tag(r.decision)}${r.metrics.retrospective_revision?tag("revision","Retrospective"):""}</td><td>${esc(r.expected_day||"—")}</td><td>${r.expected_day?(r.expected_outcome?tag(r.expected_outcome,r.expected_outcome==="actual"?"Actual":"No breakout"):"Not assessed"):(r.outcome?tag(r.outcome):"—")}</td><td>${esc(r.pattern||"—")}</td><td>${pct(r.metrics.adr_20)}</td><td>${pct(r.metrics.return_1m)}</td><td>${esc(r.notes)}</td></tr>`).join("");$("library-empty").hidden=visible.length>0;$("snapshot-gallery").innerHTML=data.snapshots.map(s=>`<a class="snapshot-card" href="/api/snapshots/${esc(s.id)}" target="_blank"><img loading="lazy" src="/api/snapshots/${esc(s.id)}" alt="${esc(s.symbol)} ${esc(s.day)} ${esc(s.frame)} replay snapshot"><p>${esc(s.symbol)} · ${esc(s.day)} · ${esc(s.frame)}</p></a>`).join("")||'<p class="muted">Press S in replay to save your first chart packet.</p>';}

$("mode-backtesting").addEventListener("click",()=>changeReplayMode("backtesting"));
$("mode-historical").addEventListener("click",()=>changeReplayMode("historical"));
$("daily-study-date").addEventListener("change",()=>run(async()=>{const day=$("daily-study-date").value;if(state.dirty)await saveReview();setReplay(await api(sessionPath("/day"),"POST",{day}),true);}));
$("scan-form").addEventListener("submit",e=>{e.preventDefault();run(async()=>{if(state.dirty)await saveReview();clearTimeout(state.poll);const scan=await api("/api/scans","POST",{day:$("scan-date").value,setup_name:$("scanner-setup").value});state.scanId=scan.id;clearReplay();state.rows=[];state.scanSetup=$("scanner-setup").value;renderStocks();await refreshScan();});});
$("scanner-setup").addEventListener("change",()=>run(async()=>{if(state.dirty)await saveReview();clearTimeout(state.poll);clearReplay();state.scanId=null;state.scanRules=null;state.rows=[];state.scanSetup=$("scanner-setup").value;$("scan-history").value="";$("scan-status").textContent="Choose a date and run the selected scanner.";$("scan-progress").style.width="0%";renderScannerRules();renderStocks();}));
$("scan-history").addEventListener("change",()=>run(async()=>{if(!$("scan-history").value)return;const chosenScan=$("scan-history").value;if(state.dirty)await saveReview();clearTimeout(state.poll);clearReplay();state.scanId=chosenScan;await refreshScan();}));
$("focus-chart").addEventListener("click",()=>setChartFocus(!document.body.classList.contains("chart-focus")));
$("focus-details").addEventListener("click",()=>{setChartFocus(false);$("notes").focus();});
$("focus-expected").addEventListener("click",()=>$("expected-button").click());
$("focus-pass").addEventListener("click",()=>$("pass-button").click());
$("symbol-filter").addEventListener("input",renderStocks);$("status-filter").addEventListener("change",renderStocks);
$("previous-day").addEventListener("click",()=>step(-1));$("next-day").addEventListener("click",()=>step(1));
$("pass-button").addEventListener("click",()=>run(async()=>{await saveReview("pass");if(state.replay.has_next)setReplay(await api(sessionPath("/step"),"POST",{direction:1}));}));
$("expected-button").addEventListener("click",()=>run(async()=>{await saveReview("expected");toast(state.replay.current_review.metrics.retrospective_revision?"Expected day revised · saved as retrospective.":"Expectation saved before revealing the next session.");}));
$("save-notes").addEventListener("click",()=>run(async()=>{await saveReview();toast("Observations saved.");}));
$("actual-button").addEventListener("click",()=>run(async()=>{await saveReview(undefined,"actual");}));$("failed-button").addEventListener("click",()=>run(async()=>{await saveReview(undefined,"failed");}));
for(const id of ["notes","pattern"])$(id).addEventListener("input",()=>{state.dirty=true;$("dirty-label").textContent="UNSAVED";$("save-state").textContent="● OBSERVATIONS UNSAVED";});
$("undo-drawing").addEventListener("click",()=>run(async()=>{const item=state.replay.drawings.filter(d=>d.frame==="daily").at(-1);if(item)setReplay(await api(sessionPath(`/drawings/${item.id}`),"DELETE"),true);}));
$("expected-entry").addEventListener("click",()=>openIntraday(state.replay.entry_preview_day));$("intraday-button").addEventListener("click",()=>openIntraday());$("snapshot-button").addEventListener("click",()=>snapshot());$("intraday-undo").addEventListener("click",()=>run(async()=>{const item=intradayDrawings(state.replay,state.intraData).at(-1);if(item){setReplay(await api(sessionPath(`/drawings/${item.id}`),"DELETE"),true);state.intraday.redraw();}}));$("intraday-snapshot").addEventListener("click",()=>snapshot("5m"));
$("load-intraday").addEventListener("click",()=>loadIntraday());
$("intraday-interval").addEventListener("change",()=>loadIntraday());
$("intraday-day").addEventListener("change",()=>{state.paperStudyId=null;$("intraday-date").value=$("intraday-day").value;loadIntraday();});
$("intraday-date").addEventListener("change",()=>{state.paperStudyId=null;
  const day=$("intraday-date").value;
  if(!state.replay.intraday_days.includes(day)){toast("Choose a day with daily data for this stock.",true);return;}
  $("intraday-day").value=day;loadIntraday();
});
$("paper-time").addEventListener("change",()=>run(()=>selectPaperCandle(Number($("paper-time").value))));
for(const id of ["paper-price","paper-stop","paper-partial"])$(id).addEventListener("input",updatePaperPlan);
$("paper-buy").addEventListener("click",()=>paperTrade("buy"));
$("paper-sell-partial").addEventListener("click",()=>paperTrade("partial_sell"));
$("paper-sell-all").addEventListener("click",()=>paperTrade("sell_all"));

$("sources-button").addEventListener("click",()=>$("sources-dialog").showModal());for(const button of document.querySelectorAll("[data-close]"))button.addEventListener("click",()=>$(button.dataset.close).close());
$("check-provider").addEventListener("click",()=>run(async()=>{const result=await api("/api/provider/check","POST",{});$("provider-check-result").textContent=result.message;await loadStatus();toast("Historical 5-minute data verified.");}));
$("library-tab").addEventListener("click",()=>run(async()=>{if(state.dirty)await saveReview();setChartFocus(false);$("workspace").hidden=true;$("library").hidden=false;$("library-tab").classList.add("active");$("replay-tab").classList.remove("active");await loadLibrary();}));$("replay-tab").addEventListener("click",()=>{$("workspace").hidden=false;$("library").hidden=true;$("library-tab").classList.remove("active");$("replay-tab").classList.add("active");});
$("library-filter").addEventListener("input",renderLibrary);$("library-status").addEventListener("change",renderLibrary);
document.addEventListener("keydown",e=>{if(e.target.matches("input,textarea,select")||e.ctrlKey||e.metaKey||e.altKey||state.busy)return;if($("intraday-dialog").open){if(e.key==="ArrowRight"||e.key==="ArrowLeft")e.preventDefault();return;}if($("sources-dialog").open||$("workspace").hidden||!state.replay)return;if(e.key==="ArrowRight"&&state.replay.has_next){e.preventDefault();step(1);}else if(e.key==="ArrowLeft"&&state.replay.has_previous){e.preventDefault();step(-1);}else if(e.key.toLowerCase()==="f")setChartFocus(!document.body.classList.contains("chart-focus"));else if(e.key.toLowerCase()==="e")$("expected-button").click();else if(e.key.toLowerCase()==="s")snapshot();else if(e.key==="Escape")state.daily.setTool("pan");});
window.addEventListener("beforeunload",e=>{if(state.dirty){e.preventDefault();e.returnValue="";}});
async function pollPreparation(){
  try{
    const data=await api("/api/preparation"),p=data.progress;
    let label=data.end_day?`Saved scans ready: ${data.start_day} to ${data.end_day}.`:"Preparing saved study dates in the background.";
    if(p&&p.phase!=="ready")label+=(p.phase==="daily_archives"?` Daily charts: ${p.processed.toLocaleString()} / ${p.total.toLocaleString()}.`:p.phase==="historical_scans"?` Scan dates: ${p.processed.toLocaleString()} / ${p.total.toLocaleString()}.`:p.phase==="daily_history"?` Adding earlier history: ${p.processed.toLocaleString()} / ${p.total.toLocaleString()} ranges.`:" Checking daily history.");
    $("preparation-status").textContent=label;
    if(data.end_day&&!state.scanId&&!state.busy)$("scan-status").textContent="Ready · select a date to load its saved scan.";
  }catch(e){$("preparation-status").textContent="Prepared-date status will reconnect shortly.";}
  setTimeout(pollPreparation,10000);
}
async function bootstrap(){try{const s=await loadStatus();$("global-error").hidden=true;$("scan-status").textContent=s.prepared_through?"Ready · select a date to load its saved scan.":s.preparation?"Initial study preparation is running. Recent saved scans remain available.":"Select a date to prepare its scan.";if(state.scanId)await refreshScan();updateButtons();}catch(e){$("global-error").textContent=e.message;$("global-error").hidden=false;$("scan-status").textContent="Reconnecting to MySQL...";setTimeout(bootstrap,4000);}}
pollPreparation();
bootstrap();

function hideChartMenu(){ $("chart-context-menu").hidden=true;state.contextDay=null; }
function showChartMenu(chart,event){
  event.preventDefault();if(state.busy||!state.replay)return;
  const point=chart.point(event);if(!point)return;
  state.contextDay=point.time;
  $("chart-context-date").textContent=dateText(point.time);
  const available=state.replay.intraday_days.includes(point.time);
  $("context-intraday").disabled=!available;$("context-expected").disabled=!available;
  const menu=$("chart-context-menu");menu.hidden=false;
  menu.style.left=Math.max(8,Math.min(event.clientX,innerWidth-menu.offsetWidth-8))+"px";
  menu.style.top=Math.max(8,Math.min(event.clientY,innerHeight-menu.offsetHeight-8))+"px";
}
async function markExpectedDay(day){
  hideChartMenu();await run(async()=>{if(state.dirty)await saveReview();setReplay(await api(sessionPath("/expect-day"),"POST",{day}),true);toast(`Expected breakout marked for ${dateText(day)} · revisions saved.`);});
}
$("context-intraday").addEventListener("click",()=>{const day=state.contextDay;hideChartMenu();openIntraday(day);});
$("context-expected").addEventListener("click",()=>markExpectedDay(state.contextDay));
$("mark-expected-day").addEventListener("click",()=>markExpectedDay($("expected-day-date").value));
document.addEventListener("pointerdown",event=>{if(!event.target.closest("#chart-context-menu"))hideChartMenu();});
document.addEventListener("keydown",event=>{if(event.key==="Escape")hideChartMenu();});
$("paper-study").addEventListener("change",()=>{state.paperStudyId=$("paper-study").value||null;loadIntraday();});
$("paper-new-study").addEventListener("click",async()=>{
  await run(async()=>{const created=await api(sessionPath("/paper/studies"),"POST",{day:state.intraData.day,request_id:crypto.randomUUID()});state.paperStudyId=created.study_id;});
  await loadIntraday();
});
