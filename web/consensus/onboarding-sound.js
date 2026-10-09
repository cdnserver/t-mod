"use strict";
(() => {
  let activated = false, context, last = 0;
  function play(kind = 'step') {
    if (!activated || document.hidden || performance.now() - last < 160) return;
    try {
      const Constructor = window.AudioContext || window.webkitAudioContext;
      if (!Constructor) return;
      context ||= new Constructor();
      // Audio starts only after a deliberate gesture, never on page load.
      if (context.state !== 'running') {
        if (context.state === 'suspended') void context.resume().then(() => {
          if (context.state === 'running') play(kind);
        }).catch(() => {});
        return;
      }
      last = performance.now();
      const notes = kind === 'success' ? [392, 523.25, 659.25] : kind === 'back' ? [330, 261.63] : [349.23, 440];
      notes.forEach((frequency, index) => {
        const oscillator = context.createOscillator(), gain = context.createGain();
        const start = context.currentTime + index * .065;
        oscillator.type = 'sine'; oscillator.frequency.value = frequency;
        gain.gain.setValueAtTime(0, start);
        gain.gain.linearRampToValueAtTime(.018, start + .015);
        gain.gain.exponentialRampToValueAtTime(.0001, start + .25);
        oscillator.connect(gain); gain.connect(context.destination);
        oscillator.start(start); oscillator.stop(start + .27);
        oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); };
      });
    } catch { /* Audio is optional; never stop an account operation. */ }
  }
  function activate() {
    activated = true;
    try {
      const Constructor = window.AudioContext || window.webkitAudioContext;
      if (!Constructor) return;
      context ||= new Constructor();
      if (context.state === 'suspended') void context.resume().catch(() => {});
    } catch {}
  }
  document.addEventListener('pointerdown', activate, { passive: true });
  document.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') activate();
  });
  document.addEventListener('tvr:stage', event => play(event.detail === 'success' ? 'success' : event.detail === 'back' ? 'back' : 'step'));
  document.addEventListener('click', event => {
    if (event.target.closest('a.button,button[data-kind],.route-grid button')) play();
  });
})();
