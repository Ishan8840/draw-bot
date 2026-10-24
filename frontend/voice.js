(() => {
  'use strict';
  const el=id=>document.getElementById(id);
  let enabled=false, ready=false, socket=null, stream=null, context=null, worklet=null;
  let source=null, silentGain=null, sequence=0, generation=0, streamId=null;
  let speakerMuted=false, playing=null, playbackQueue=[], playingJob=false;
  let playbackEpoch=0, localBargeBlocked=false, loudFrames=0, reconnectTimer=null, lifecycle=0;
  function error(text) { el('voice-error').textContent=text; }
  function send(data) { if(socket?.readyState===WebSocket.OPEN) socket.send(JSON.stringify(data)); }
  function stopPlayback() {
    playbackEpoch++; playbackQueue=[]; playingJob=false;
    if(playing) { try { playing.stop(); } catch {} playing.disconnect(); playing=null; }
    el('speaking-status').textContent='Astra silent';
  }
  async function playNext() {
    if(playingJob || !playbackQueue.length || speakerMuted || localBargeBlocked || !enabled) return;
    const item=playbackQueue.shift(), epoch=playbackEpoch;
    if(item.generation!==generation || item.stream_id!==streamId) return playNext();
    playingJob=true;
    try {
      const bytes=Uint8Array.from(atob(item.audio),c=>c.charCodeAt(0));
      const buffer=await context.decodeAudioData(bytes.buffer);
      if(epoch!==playbackEpoch || item.generation!==generation || speakerMuted || localBargeBlocked || !enabled) return;
      await context.resume();
      if(epoch!==playbackEpoch || item.generation!==generation || !enabled || localBargeBlocked) return;
      const node=context.createBufferSource(); node.buffer=buffer; node.connect(context.destination); playing=node;
      node.onended=()=>{node.disconnect(); if(playing===node) playing=null; if(epoch===playbackEpoch){playingJob=false;el('speaking-status').textContent='Astra silent';playNext();}};
      node.start(); el('speaking-status').textContent='Astra speaking';
      send({type:'playback_started',generation,audio_id:item.audio_id});
    } catch(e) { if(epoch===playbackEpoch){playingJob=false;error(`Audio playback failed: ${e.message}`);playNext();} }
  }
  function bargeIn() {
    if(localBargeBlocked) return;
    localBargeBlocked=true; stopPlayback(); send({type:'barge_in'});
  }
  function connect(token) {
    ready=false; streamId=null; generation=0; sequence=0; localBargeBlocked=false;
    stopPlayback(); el('mic-status').textContent='Loading local speech models…';
    const sid=window.astraSessionId || localStorage.getItem('astra-session');
    const ws=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/voice/ws/${encodeURIComponent(sid)}`);
    socket=ws;
    ws.onopen=()=>send({type:'start',voice:el('voice-select').value || 'en_US-lessac-medium',tts_enabled:el('voice-responses').checked,silence_ms:Number(el('voice-silence').value),threshold:Number(el('voice-threshold').value)});
    ws.onmessage=event=>{
      if(token!==lifecycle || !enabled || socket!==ws) return;
      const data=JSON.parse(event.data);
      if(data.type==='ready') { streamId=data.stream_id;generation=data.generation;ready=true;send({type:'mute',muted:speakerMuted});el('mic-status').textContent='Listening';error('');return; }
      if(data.stream_id && data.stream_id!==streamId) return;
      if(data.type==='playback_stop') {
        if(data.generation>=generation){generation=data.generation;stopPlayback();}
        // Server acknowledgement revokes old WAVs; a new valid response may now play.
        localBargeBlocked=false;
      } else if(data.type==='status') {
        el('mic-status').textContent=data.state==='speaking'?'Hearing speech…':'Listening';
        el('mic-status').classList.toggle('listening',data.state==='listening');
        if(data.state==='speaking') stopPlayback();
      } else if(data.type==='transcript') {
        el('voice-transcript').textContent=data.text || 'No speech recognized.';
      } else if(data.type==='transcription') {
        if(['transcribing','submitting'].includes(data.state)) el('mic-status').textContent=data.state==='transcribing'?'Listening · transcribing…':'Listening · sending to Astra…';
        else if(data.state==='superseded') error('That utterance was superseded by a newer instruction.');
        else el('mic-status').textContent='Listening';
      } else if(data.type==='control') {
        error(data.action==='cancel'?'Remaining drawing cancelled; ink preserved.':'Drawing paused.');
      } else if(data.type==='tts_status' && data.generation===generation) {
        el('speaking-status').textContent='Preparing speech…';
      } else if(data.type==='tts_audio') {
        if(data.generation===generation && !speakerMuted && el('voice-responses').checked && !localBargeBlocked) {playbackQueue.push(data);playNext();}
      } else if(data.type==='error') error(data.message);
    };
    ws.onerror=()=>error('Voice connection failed. Check the server and local voice models.');
    ws.onclose=()=>{
      if(socket!==ws) return;
      ready=false;stopPlayback();
      if(enabled && token===lifecycle) {
        el('mic-status').textContent='Voice disconnected';
        // Stop the hardware capture on failure. Re-enable explicitly instead of
        // repeatedly loading failed models or recording without a connection.
        disable(); error('Voice disconnected. Check the server/model setup, then enable the microphone again.');
      }
    };
  }
  async function inputs() {
    if(!navigator.mediaDevices?.enumerateDevices) return;
    const selected=el('audio-input').value;
    const devices=await navigator.mediaDevices.enumerateDevices();
    el('audio-input').replaceChildren(new Option('Default input',''));
    for(const device of devices.filter(d=>d.kind==='audioinput')) el('audio-input').add(new Option(device.label || `Microphone ${el('audio-input').length}`,device.deviceId));
    el('audio-input').value=selected;
  }
  async function enable() {
    const token=++lifecycle;
    if(!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {error('Microphone streaming requires localhost or HTTPS and a browser with AudioWorklet support.');return;}
    if(!window.astraSessionId) {error('Wait for the chat session to connect first.');return;}
    el('voice-enable').disabled=true;
    try {
      const deviceId=el('audio-input').value;
      const acquired=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true,...(deviceId?{deviceId:{exact:deviceId}}:{})},video:false});
      if(token!==lifecycle){acquired.getTracks().forEach(t=>t.stop());return;}
      stream=acquired;
      context=new AudioContext(); await context.resume();
      await context.audioWorklet.addModule('/static/mic-worklet.js');
      if(token!==lifecycle){disable();return;}
      source=context.createMediaStreamSource(stream); worklet=new AudioWorkletNode(context,'astra-microphone');
      silentGain=context.createGain();silentGain.gain.value=0;
      source.connect(worklet);worklet.connect(silentGain);silentGain.connect(context.destination);
      worklet.port.onmessage=event=>{
        if(!enabled || !ready || socket?.readyState!==WebSocket.OPEN) return;
        loudFrames=event.data.rms>.035?loudFrames+1:0;
        if(playing && loudFrames>=3) bargeIn(); // Local audio-only anticipation; Silero remains authoritative VAD.
        if(socket.bufferedAmount>64000) {disable();error('Audio upload fell behind. Re-enable voice after the connection recovers.');return;}
        const samples=new Int16Array(event.data.pcm);
        const packet=new ArrayBuffer(4+1024), view=new DataView(packet);
        view.setUint32(0,sequence++,true);
        for(let i=0;i<samples.length;i++) view.setInt16(4+i*2,samples[i],true);
        socket.send(packet);
      };
      for(const track of stream.getTracks()) track.onended=()=>{if(enabled){disable();error('Microphone disconnected. Select an input and enable it again.');}};
      enabled=true;el('voice-enable').textContent='Disable microphone';
      connect(token); await inputs();
    } catch(e) {
      disable(); error(e.name==='NotAllowedError'?'Microphone permission was denied. Allow access in your browser and try again.':`Microphone setup failed: ${e.message}`);
    } finally {el('voice-enable').disabled=false;}
  }
  function disable() {
    enabled=false;ready=false;++lifecycle;clearTimeout(reconnectTimer);stopPlayback();
    send({type:'disable'});if(socket){const ws=socket;socket=null;ws.close();}
    if(stream){stream.getTracks().forEach(t=>{t.onended=null;t.stop();});stream=null;}
    source?.disconnect();worklet?.disconnect();silentGain?.disconnect();
    source=worklet=silentGain=null;
    if(context){context.close().catch(()=>{});context=null;}
    el('voice-enable').textContent='Enable microphone';el('mic-status').textContent='Microphone off';el('mic-status').classList.remove('listening');
  }
  el('voice-enable').addEventListener('click',()=>enabled?disable():enable());
  el('voice-mute').addEventListener('click',()=>{speakerMuted=!speakerMuted;stopPlayback();send({type:'mute',muted:speakerMuted});el('voice-mute').textContent=speakerMuted?'Unmute speaker':'Mute speaker';el('voice-mute').setAttribute('aria-pressed',String(speakerMuted));});
  el('voice-responses').addEventListener('change',()=>{stopPlayback();send({type:'responses',enabled:el('voice-responses').checked});});
  window.addEventListener('astra-user-input',()=>{stopPlayback();send({type:'barge_in'});});
  for(const button of document.querySelectorAll('[data-action]')) button.addEventListener('click',()=>{stopPlayback();send({type:'barge_in'});});
  window.addEventListener('pagehide',disable);
  navigator.mediaDevices?.addEventListener?.('devicechange',()=>inputs().catch(()=>{}));
  fetch('/voice/config').then(r=>r.json()).then(config=>{
    const voices=config.voices.length?config.voices:[config.default_voice];
    for(const voice of voices) el('voice-select').add(new Option(voice,voice));
    if(voices.includes(config.default_voice))el('voice-select').value=config.default_voice;
    el('voice-silence').value=config.silence_ms;el('voice-threshold').value=config.threshold;
    el('voice-responses').checked=config.tts_enabled;
    if(!config.enabled){el('voice-enable').disabled=true;error('Voice is disabled on the server.');}
    else if(config.missing_packages.length || !config.vad_model_present)error('Local speech setup is incomplete. Follow docs/VOICE.md to install models.');
  }).catch(()=>error('Could not load voice settings.'));
  inputs().catch(()=>{});
})();
