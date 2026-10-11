/* Indicators use only the bars returned by the replay cutoff. */
"use strict";
const ReplayIndicators = (() => {
  function relativeStudy(bars) {
    const relative = [], average = [], regime = [], markers = [], window = [];
    let sum = 0, valid = 0;
    for (const bar of bars) {
      const hasRelative = Number.isFinite(bar.rs);
      const value = hasRelative ? bar.rs : null;
      relative.push(hasRelative ? {
        time: bar.time, value,
        color: value > 0 ? "#22c55e" : value < 0 ? "#ef4444" : "#95a29f",
      } : {time: bar.time});
      window.push(value);
      if (hasRelative) { sum += value; valid++; }
      if (window.length > 50) {
        const removed = window.shift();
        if (removed !== null) { sum -= removed; valid--; }
      }
      average.push(window.length === 50 && valid === 50
        ? {time: bar.time, value: sum / 50} : {time: bar.time});
      regime.push({time: bar.time, value: 1, color:
        bar.ti65 > 1.05 ? "rgba(34,197,94,0.18)" :
        bar.ti65 != null && bar.ti65 < .95 ? "rgba(239,68,68,0.18)" : "transparent"});
      if (hasRelative && Number.isFinite(bar.change_pct) && Math.abs(bar.change_pct) >= 4) {
        const up = bar.change_pct >= 4;
        markers.push({time: bar.time, position: up ? "aboveBar" : "belowBar",
          color: up ? "#22c55e" : "#ef4444", shape: "circle", size: 1,
          text: `${up ? "+" : ""}${Math.round(bar.change_pct)}%`});
      }
    }
    return {relative, average, regime, markers};
  }
  function stopAdrTone(value, valid) {
    if (!Number.isFinite(value)) return "";
    if (value > 150) return "negative";
    if (value > 100) return "warning";
    return valid == null ? "" : valid ? "positive" : "negative";
  }
  function intradayMarkers(orb) {
    if (!orb?.available) return [];
    const markers = [];
    if (orb.entry_taken) markers.push({time: Date.parse(orb.entry_time) / 1000,
      position: "belowBar", color: "#39c894", shape: "arrowUp", text: "Entry"});
    else if (orb.triggered) markers.push({time: Date.parse(orb.trigger_time) / 1000,
      position: "aboveBar", color: "#f7a54a", shape: "circle", text: "No entry · stop check"});
    if (orb.stop_hit) markers.push({time: Date.parse(orb.stop_time) / 1000,
      position: "aboveBar", color: "#e95b68", shape: "arrowDown", text: "Stop loss"});
    if (orb.uncertain_stop_time) markers.push({time: Date.parse(orb.uncertain_stop_time) / 1000,
      position: "aboveBar", color: "#f7a54a", shape: "circle", text: "Entry / stop order unclear"});
    return markers.sort((a, b) => a.time - b.time);
  }
  function paperMarkers(paper, day, daily=false) {
    return (paper?.fills||[]).filter(fill=>daily||fill.day===day).map(fill=>({
      time:daily?fill.day:fill.time,position:fill.side==="buy"?"belowBar":"aboveBar",
      color:fill.side==="buy"?"#56bde9":fill.reason==="partial_sell"?"#f7a54a":"#e95b68",
      shape:fill.side==="buy"?"arrowUp":"arrowDown",
      text:`${fill.side==="buy"?"Buy":fill.reason.startsWith("stop")?"Stop":fill.reason==="partial_sell"?"Partial sell":"Sell all"} ${fill.shares}${fill.reason==="stop_same_bar"?" · order unclear":""}`,
    })).sort((a,b)=>a.time<b.time?-1:a.time>b.time?1:0);
  }
  function intradayBucket(time, minutes=5) {
    return minutes===30?Math.floor(time/1800)*1800:time;
  }
  function intradayCandles(bars, minutes=5) {
    if(minutes===5)return bars;
    const grouped=[];
    for(const bar of bars){
      const time=intradayBucket(bar.time,minutes), last=grouped.at(-1);
      if(!last||last.time!==time)grouped.push({...bar,time,count:1});
      else{last.high=Math.max(last.high,bar.high);last.low=Math.min(last.low,bar.low);
        last.close=bar.close;last.volume+=bar.volume;last.count++;}
    }
    return grouped;
  }
  function intradayChartMarkers(markers, minutes=5) {
    if(minutes===5)return markers;
    return markers.map(marker=>({...marker,time:intradayBucket(marker.time,minutes),
      text:`${marker.text} ${new Date(marker.time*1000).toLocaleTimeString("en-US",{
        hour:"2-digit",minute:"2-digit",hour12:false,timeZone:"America/New_York"})}`}));
  }
  return {relativeStudy, stopAdrTone, intradayMarkers, paperMarkers, intradayBucket, intradayCandles, intradayChartMarkers};
})();
if (typeof module !== "undefined") module.exports = ReplayIndicators;
