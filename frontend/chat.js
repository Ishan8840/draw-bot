/* Local session ID only: OpenAI credentials never enter the browser. */
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  let sessionId = localStorage.getItem('astra-session');
  let timer;
  let closed = false;
  let lastEventId = -1;
  let pendingMessage = null;
  let sending = false;
  let requestVersion = 0;
  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) { const error = new Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail)); error.status=response.status; throw error; }
    return result;
  }
  function notice(text, error=false) {
    el('chat-notice').textContent=text;
    el('chat-notice').classList.toggle('error',error);
  }
  function render(session) {
    el('agent-status').textContent=session.status;
    const newest = session.events.at(-1)?.id ?? 0;
    if (newest === lastEventId) return;
    lastEventId=newest;
    const history=el('chat-history');
    const nearBottom=history.scrollHeight-history.scrollTop-history.clientHeight<70;
    history.replaceChildren();
    for (const event of session.events) {
      const wrapper=document.createElement('div');
      wrapper.className=`chat-event ${event.kind}`;
      if (event.kind==='tool_call' || event.kind==='tool_result') {
        const details=document.createElement('details');
        const title=document.createElement('summary');
        title.textContent=`${event.kind==='tool_call'?'Tool':'Result'} · ${event.tool_name}`;
        const pre=document.createElement('pre');
        try { pre.textContent=JSON.stringify(JSON.parse(event.content),null,2); } catch { pre.textContent=event.content; }
        details.append(title,pre); wrapper.append(details);
      } else {
        const label=document.createElement('strong');
        label.textContent=event.kind==='user'?(event.source==='board'?'Your drawing':'You'):event.kind==='assistant'?'Astra':event.kind==='error'?'Error':'Interrupted';
        const text=document.createElement('p'); text.textContent=event.kind==='user' && event.source==='board'?'You drew on the board. Astra is responding.':event.content;
        wrapper.append(label,text);
      }
      history.append(wrapper);
    }
    if (nearBottom || session.events.at(-1)?.kind==='user') history.scrollTop=history.scrollHeight;
  }
  async function poll() {
    if (closed) return;
    const version=requestVersion;
    try {
      const session=await api(`/agent/sessions/${sessionId}`);
      if (version===requestVersion) render(session);
    } catch(e) { notice(`Conversation connection lost: ${e.message}. Retrying…`,true); }
    if (!closed) timer=setTimeout(poll,600);
  }
  el('chat-form').addEventListener('submit',async event => {
    event.preventDefault();
    const text=el('chat-input').value.trim();
    if (!text || !sessionId || sending) return;
    sending=true; el('chat-send').disabled=true;
    // Reuse the same ID after a lost HTTP response, preventing a duplicated turn.
    if (!pendingMessage || pendingMessage.text!==text) pendingMessage={text,request_id:crypto.randomUUID()};
    ++requestVersion;
    window.dispatchEvent(new Event('astra-user-input'));
    try {
      const session=await api(`/agent/sessions/${sessionId}/messages`,pendingMessage);
      render(session); el('chat-input').value=''; pendingMessage=null;
      notice('Astra is planning. You can send another instruction or Stop at any time.');
    } catch(e) { notice(e.message,true); }
    finally { sending=false; el('chat-send').disabled=false; ++requestVersion; }
  });
  el('chat-stop').addEventListener('click',async () => {
    window.dispatchEvent(new Event('astra-user-input'));
    ++requestVersion;
    try { render(await api(`/agent/sessions/${sessionId}/stop`,{})); notice('Stopped. Existing ink and pending plans are preserved.'); }
    catch(e) { notice(e.message,true); }
    finally { ++requestVersion; }
  });
  async function init() {
    try {
      let session;
      if (sessionId) {
        try { session=await api(`/agent/sessions/${sessionId}`); }
        catch(e) { if(e.status!==404) throw e; }
      }
      if (!session) { session=await api('/agent/sessions',{}); sessionId=session.session_id; localStorage.setItem('astra-session',sessionId); }
      window.astraSessionId=sessionId;
      window.dispatchEvent(new CustomEvent('astra-session',{detail:{sessionId}}));
      render(session);
      const config=await api('/agent/config');
      if (!config.configured) notice('Chat is not configured. Set OPENAI_API_KEY on the server, then restart it. Structured commands still work.',true);
      else notice(`${config.model} · New messages pause active drawing while Astra revises the plan.`);
      poll();
    } catch(e) { notice(e.message,true); if(!closed) timer=setTimeout(init,2000); }
  }
  window.addEventListener('pagehide',()=>{closed=true;clearTimeout(timer);});
  init();
})();
