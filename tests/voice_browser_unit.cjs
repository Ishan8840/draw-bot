/* Browser logic tests with deterministic WebAudio/DOM mocks; no real browser. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function resampling(rate) {
  const frames=[];let Processor;
  const sandbox={sampleRate:rate,AudioWorkletProcessor:class {constructor(){this.port={postMessage:m=>frames.push(m)};}},registerProcessor:(name,p)=>{Processor=p;},Int16Array,Math};
  vm.runInNewContext(fs.readFileSync('frontend/mic-worklet.js','utf8'),sandbox);
  const processor=new Processor();
  for(let offset=0;offset<rate;offset+=128){const input=new Float32Array(Math.min(128,rate-offset)).fill(.25);processor.process([[input]]);}
  assert.equal(frames.length,31); // 15872 samples sent, 128 remain buffered.
  for(const frame of frames){assert.equal(frame.pcm.byteLength,1024);assert(Math.abs(frame.rms-.25)<.001);assert(Math.abs(new Int16Array(frame.pcm)[0]-8192)<=1);}
}
function harness() {
  const elements=new Map(),events={},sockets=[],contexts=[],worklets=[],tracks=[];
  function node(id){if(!elements.has(id))elements.set(id,{value:id==='voice-silence'?'450':id==='voice-threshold'?'0.5':'',checked:true,disabled:false,textContent:'',listeners:{},classList:{toggle(){},remove(){}},addEventListener(n,f){this.listeners[n]=f;},setAttribute(){},replaceChildren(){},add(option){if(!this.value)this.value=option.value;},length:1});return elements.get(id);}
  class Socket{static OPEN=1;constructor(){this.readyState=1;this.sent=[];this.bufferedAmount=0;sockets.push(this);}send(value){this.sent.push(value);}close(){this.readyState=3;this.onclose?.();}message(data){this.onmessage({data:JSON.stringify(data)});}}
  class Context{constructor(){this.audioWorklet={addModule:async()=>{}};this.destination={};this.started=0;this.stopped=0;this.decodes=[];contexts.push(this);}async resume(){} async close(){}createMediaStreamSource(){return {connect(){},disconnect(){}};}createGain(){return {gain:{value:0},connect(){},disconnect(){}};}decodeAudioData(){return new Promise(resolve=>this.decodes.push(resolve));}createBufferSource(){return {connect(){},disconnect(){},start:()=>this.started++,stop:()=>this.stopped++};}}
  class Worklet{constructor(){this.port={};worklets.push(this);}connect(){}disconnect(){}}
  const media={getUserMedia:async()=>{const track={stop(){this.stopped=true;},onended:null};tracks.push(track);return {getTracks:()=>[track]};},enumerateDevices:async()=>[],addEventListener(){}};
  const window={isSecureContext:true,AudioWorkletNode:Worklet,astraSessionId:'same-chat-session',addEventListener:(n,f)=>events[n]=f};
  const sandbox={window,document:{getElementById:node,querySelectorAll:()=>[]},navigator:{mediaDevices:media},localStorage:{getItem:()=>null},WebSocket:Socket,AudioContext:Context,AudioWorkletNode:Worklet,
    location:{protocol:'http:',host:'localhost:8000'},fetch:async()=>({json:async()=>({voices:['en_US-lessac-medium'],default_voice:'en_US-lessac-medium',enabled:true,tts_enabled:true,silence_ms:450,threshold:.5,missing_packages:[],vad_model_present:true})}),
    Option:class{constructor(text,value){this.text=text;this.value=value;}},Event:class{},CustomEvent:class{},setTimeout,clearTimeout,Uint8Array,Int16Array,ArrayBuffer,DataView,Math,Number,String,JSON,atob};
  vm.runInNewContext(fs.readFileSync('frontend/voice.js','utf8'),sandbox);
  return {node,events,sockets,contexts,worklets,tracks,media};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  resampling(48000);resampling(44100);resampling(16000);
  const h=harness();await tick();await h.node('voice-enable').listeners.click();
  const ws=h.sockets[0],ctx=h.contexts[0];ws.onopen();
  assert(JSON.parse(ws.sent[0]).type==='start');
  ws.message({type:'ready',stream_id:'stream-1',generation:0});
  ws.message({type:'playback_stop',stream_id:'stream-1',generation:1});
  const audio=Buffer.from('not-real-wav').toString('base64');
  ws.message({type:'tts_audio',stream_id:'stream-1',generation:1,audio_id:'old',audio});
  assert.equal(ctx.decodes.length,1);
  ws.message({type:'playback_stop',stream_id:'stream-1',generation:2,reason:'speech_onset'});
  ctx.decodes.shift()({});await tick();assert.equal(ctx.started,0,'obsolete decode must never play');
  ws.message({type:'tts_audio',stream_id:'stream-1',generation:2,audio_id:'new',audio});
  ctx.decodes.shift()({});await tick();assert.equal(ctx.started,1);
  for(let i=0;i<3;i++)h.worklets[0].port.onmessage({data:{pcm:new Int16Array(512).buffer,rms:.1}});
  assert.equal(ctx.stopped,1,'local speech onset must stop playback');
  assert(ws.sent.some(s=>typeof s==='string' && JSON.parse(s).type==='barge_in'));
  assert(ws.sent.some(s=>typeof s!=='string'),'capture continues during barge-in');
  h.node('voice-mute').listeners.click();
  assert.equal(h.node('voice-mute').textContent,'Unmute speaker');
  await h.node('voice-enable').listeners.click();
  assert(h.tracks[0].stopped,'disabling must release microphone');
  const denied=harness();await tick();denied.media.getUserMedia=async()=>{throw Object.assign(new Error('denied'),{name:'NotAllowedError'});};
  await denied.node('voice-enable').listeners.click();
  assert(denied.node('voice-error').textContent.includes('permission was denied'));
  assert.equal(denied.node('voice-enable').textContent,'Enable microphone');
  console.log('Passed: 16/44.1/48 kHz resampling, stale playback, local barge-in, continuous capture, mute, device release, permission failure.');
})().catch(error=>{console.error(error);process.exitCode=1;});
