/* Continuous PCM16 mono capture; fractional-area resampling to 16 kHz. */
class AstraMicrophone extends AudioWorkletProcessor {
  constructor() {
    super(); this.ratio=sampleRate/16000; this.sum=0; this.filled=0;
    this.frame=new Int16Array(512); this.index=0; this.energy=0;
  }
  process(inputs) {
    const input=inputs[0]?.[0];
    if (!input) return true;
    for (let i=0;i<input.length;i++) {
      let remaining=1;
      while(remaining>1e-8) {
        const take=Math.min(remaining,this.ratio-this.filled);
        this.sum+=input[i]*take; this.filled+=take; remaining-=take;
        if(this.filled>=this.ratio-1e-8) {
          const value=Math.max(-1,Math.min(1,this.sum/this.ratio));
          this.frame[this.index++]=Math.round(value*32767); this.energy+=value*value;
          this.sum=0; this.filled=0;
          if(this.index===512) {
            this.port.postMessage({pcm:this.frame.buffer,rms:Math.sqrt(this.energy/512)},[this.frame.buffer]);
            this.frame=new Int16Array(512); this.index=0; this.energy=0;
          }
        }
      }
    }
    return true;
  }
}
registerProcessor('astra-microphone',AstraMicrophone);
