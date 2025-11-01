'use client';
import { useEffect, useMemo, useRef, useState } from 'react';
function colorize(v, thr){ if(v>=thr) return 'bg-green-600 text-white'; if(v>=0) return 'bg-yellow-400 text-black'; return 'bg-red-600 text-white'; }
export default function Home() {
  const [data,setData]=useState(null);
  const [rttMs,setRttMs]=useState(0);
  const [lastTs,setLastTs]=useState(0);
  const wsRef=useRef(null);
  useEffect(()=>{
    const proto = window.location.protocol==='https:'?'wss':'ws';
    const url = `${proto}://${window.location.host}/ws/edges`;
    const ws = new WebSocket(url); wsRef.current = ws;
    let tmr;
    ws.onopen=()=>{ tmr=setInterval(()=>{ const t0=performance.now(); ws.send('ping'); setRttMs(Math.round(performance.now()-t0)); }, 1000); };
    ws.onmessage=(ev)=>{ const txt = ev.data; if (typeof txt === 'string' && txt.startsWith('pong:')) { const t0 = parseFloat(txt.split(':')[1]); if (!Number.isNaN(t0)) setRttMs(Math.round(performance.now()-t0)); return; } try{ const payload=JSON.parse(ev.data); setData(payload); setLastTs(Date.now()); }catch(e){} };
    ws.onclose=()=>{ clearInterval(tmr); };
    return ()=>{ clearInterval(tmr); ws.close(); };
  },[]);
  const ageMs = useMemo(()=> lastTs? Date.now()-lastTs : 0, [lastTs]);
  const ps = data?.edge_ps_mm_bps ?? 0; const sp=data?.edge_sp_mm_bps ?? 0; const thr=data?.threshold_bps ?? 3; const base=data?.base ?? 'HYPE';
  return (<main className="min-h-screen bg-black text-white p-4">
    <div className="fixed top-3 right-3 bg-white/10 text-white px-3 py-1 rounded-md shadow">
      <div className="text-xs">latency (ws echo): {rttMs} ms</div>
      <div className="text-xs">age: {ageMs} ms</div><div className="text-xs">server recv: {data?.latency_ms ?? 0} ms</div>
    </div>
    <h1 className="text-2xl font-bold mb-4">HL Arb â€” {base}/USDC</h1>
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div className={`rounded p-4 ${colorize(ps,thr)}`}>
        <div className="text-lg font-semibold">perp â†’ spot</div>
        <div className="text-3xl font-bold">{ps.toFixed(2)} bps</div>
        <div className="text-xs opacity-90 mt-2">threshold: {thr} bps</div>
      </div>
      <div className={`rounded p-4 ${colorize(sp,thr)}`}>
        <div className="text-lg font-semibold">spot â†’ perp</div>
        <div className="text-3xl font-bold">{sp.toFixed(2)} bps</div>
        <div className="text-xs opacity-90 mt-2">threshold: {thr} bps</div>
      </div>
    </div>
    <p className="mt-6 opacity-80 text-sm">Colors: red &lt; 0 Â· yellow â‰¥ 0 but &lt; threshold Â· green â‰¥ threshold.</p>
  </main>);
}

