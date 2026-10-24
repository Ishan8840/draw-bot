(() => {
  'use strict';
  const panel=document.getElementById('simulation-panel');
  const frame=document.getElementById('simulation-frame');
  const status=document.getElementById('simulation-status');
  const camera=document.getElementById('simulation-camera');
  let closed=false, timer, url=null, controller;
  async function poll() {
    if(closed) return;
    try {
      controller=new AbortController();
      const response=await fetch('/simulation/status',{signal:controller.signal});
      if(!response.ok) throw new Error('Simulator status unavailable');
      const state=await response.json();
      if(!state.enabled) {panel.hidden=true;return;}
      panel.hidden=false;
      document.getElementById("trace-note").hidden=false;
      document.getElementById("board").setAttribute("aria-label","Estimated pen trace and manual input; camera confirms actual marks");
      const feedback=state.drawing_feedback;
      const check=feedback?` · camera: ${feedback.phase}${feedback.latest?' / '+feedback.latest.outcome:''} · correction ${(feedback.depth_offset_m*1000).toFixed(2)} mm`:'';
      status.textContent=state.fault?`Paused: ${state.fault}`:`${state.execution_status} · ${state.phase} · encoder estimate · ink unverified · simulation ${state.simulation_time.toFixed(1)} s${check}`;
      if(!document.hidden) {
        const image=await fetch(`/simulation/frame?camera=${encodeURIComponent(camera.value)}`,{signal:controller.signal});
        if(!image.ok) {const detail=await image.json();throw new Error(detail.detail || 'Rendering failed');}
        const blob=await image.blob();
        if(closed) return;
        const previous=url;url=URL.createObjectURL(blob);frame.src=url;
        if(previous) URL.revokeObjectURL(previous);
      }
    } catch(error) {
      if(!closed) {panel.hidden=false;status.textContent=`Simulation: ${error.message}. Retrying…`;}
    } finally {if(!closed) timer=setTimeout(poll,250);}
  }
  window.addEventListener('pagehide',()=>{closed=true;clearTimeout(timer);controller?.abort();if(url)URL.revokeObjectURL(url);});
  poll();
})();
