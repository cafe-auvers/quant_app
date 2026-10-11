const test = require("node:test");
const assert = require("node:assert/strict");
const {relativeStudy, stopAdrTone, intradayMarkers, paperMarkers, intradayCandles, intradayChartMarkers} = require("../../src/web/static/replay/indicators.js");

const bars = Array.from({length: 80}, (_, i) => ({
  time: `session-${i}`, rs: i - 25, ti65: i % 3 === 0 ? 1.06 : i % 3 === 1 ? .94 : 1,
  change_pct: i % 4 === 0 ? 4.2 : i % 4 === 1 ? -5.7 : 3.9,
}));

test("thirty-minute candles combine six raw bars without changing the archive", () => {
  const start=Date.parse("2025-01-03T09:30:00-05:00")/1000;
  const raw=Array.from({length:78},(_,i)=>({time:start+i*300,open:100+i,high:102+i,low:99+i,close:101+i,volume:1000}));
  const original=JSON.parse(JSON.stringify(raw));
  const grouped=intradayCandles(raw,30);
  assert.equal(grouped.length,13);
  assert.deepEqual(grouped[0],{time:start,open:100,high:107,low:99,close:106,volume:6000,count:6});
  assert.deepEqual(raw,original);
  assert.equal(intradayCandles(raw,5),raw);
  assert.equal(intradayCandles(raw.slice(1),30)[0].count,5);
});

test("thirty-minute chart markers retain their actual New York fill times", () => {
  const start=Date.parse("2025-07-02T09:30:00-04:00")/1000;
  const markers=[{time:start+300,text:"Buy 20"},{time:start+1800,text:"Stop 20"}];
  assert.deepEqual(intradayChartMarkers(markers,30),[{time:start,text:"Buy 20 09:35"},
    {time:start+1800,text:"Stop 20 10:00"}]);
  assert.equal(intradayChartMarkers(markers,5),markers);
});

test("paper markers preserve share quantities and filter to the selected intraday date", () => {
  const fills=[{day:"2025-01-03",time:1,side:"buy",reason:"manual_buy",shares:20},
    {day:"2025-01-03",time:2,side:"sell",reason:"partial_sell",shares:10},
    {day:"2025-01-06",time:3,side:"sell",reason:"stop_gap",shares:10}];
  assert.deepEqual(paperMarkers({fills},"2025-01-03").map(m=>m.text),["Buy 20","Partial sell 10"]);
  const stop=paperMarkers({fills},"2025-01-06")[0];
  assert.equal(stop.text,"Stop 10");
  assert.equal(stop.shape,"arrowDown");
  assert.deepEqual(paperMarkers({fills},null,true).map(m=>m.time),["2025-01-03","2025-01-03","2025-01-06"]);
  assert.deepEqual(paperMarkers(null,"2025-01-03"),[]);
});

test("stop/ADR warning colors obey the strict 100% and 150% thresholds", () => {
  assert.equal(stopAdrTone(null, null), "");
  assert.equal(stopAdrTone(40, true), "positive");
  assert.equal(stopAdrTone(100, false), "negative");
  assert.equal(stopAdrTone(100.01, false), "warning");
  assert.equal(stopAdrTone(150, false), "warning");
  assert.equal(stopAdrTone(150.01, false), "negative");
});

test("intraday entry, stop and rejected-trigger markers describe distinct events", () => {
  const orb = {available:true, entry_taken:true, entry_time:"2025-01-03T09:40:00-05:00",
    stop_hit:true, stop_time:"2025-01-03T12:30:00-05:00"};
  const markers = intradayMarkers(orb);
  assert.deepEqual(markers.map(m=>m.text), ["Entry", "Stop loss"]);
  assert.ok(markers[0].time < markers[1].time);
  assert.equal(markers[0].shape, "arrowUp");
  assert.equal(markers[1].shape, "arrowDown");
  const rejected = intradayMarkers({available:true, entry_taken:false, triggered:true, trigger_time:orb.entry_time});
  assert.equal(rejected[0].text, "No entry · stop check");
  assert.deepEqual(intradayMarkers({available:true, entry_taken:false, triggered:false}), []);
  assert.deepEqual(intradayMarkers({available:false}), []);
  const ambiguous = intradayMarkers({...orb,stop_hit:null,stop_time:null,uncertain_stop_time:orb.entry_time});
  assert.equal(ambiguous[1].text, "Entry / stop order unclear");
  assert.ok(!ambiguous.some(m=>m.text === "Stop loss"));
});

test("relative SMA uses exactly 50 revealed sessions and remains causal", () => {
  const prefix = relativeStudy(bars.slice(0, 60));
  const full = relativeStudy(bars);
  assert.equal(prefix.average[48].value, undefined);
  assert.equal(prefix.average[49].value, -.5);
  assert.equal(prefix.average[59].value, 9.5);
  for (const key of ["relative", "average", "regime"]) {
    assert.deepEqual(prefix[key], full[key].slice(0, 60));
  }
  assert.deepEqual(prefix.markers, full.markers.filter(marker => Number(marker.time.slice(8)) < 60));
});

test("missing SPY data stays whitespace and does not become zero", () => {
  const source = structuredClone(bars);
  source[25].rs = null;
  const result = relativeStudy(source);
  assert.deepEqual(result.relative[25], {time: source[25].time});
  assert.equal(result.average[74].value, undefined);
  assert.equal(result.average[75].value, 25.5);
  assert.ok(!result.markers.some(marker => marker.time === source[25].time));
  assert.equal(source[25].rs, null);
});

test("daily movement labels and TI65 shading use the reference thresholds", () => {
  const result = relativeStudy(bars);
  assert.equal(result.markers[0].text, "+4%");
  assert.equal(result.markers[0].position, "aboveBar");
  assert.equal(result.markers[1].text, "-6%");
  assert.equal(result.markers[1].position, "belowBar");
  assert.equal(result.markers.length, 40);
  assert.equal(result.regime[0].color, "rgba(34,197,94,0.18)");
  assert.equal(result.regime[1].color, "rgba(239,68,68,0.18)");
  assert.equal(result.regime[2].color, "transparent");
  assert.equal(result.relative[0].color, "#ef4444");
  assert.equal(result.relative[79].color, "#22c55e");
  assert.deepEqual(relativeStudy([]), {relative: [], average: [], regime: [], markers: []});
});
