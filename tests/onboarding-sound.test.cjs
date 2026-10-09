const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/consensus/onboarding-sound.js'), 'utf8');
function fixture(unavailable = false) {
  const events = {}, sounds = [];
  let time = 1000, resumes = 0, created = 0;
  const document = {
    hidden: false,
    addEventListener(name, fn) { events[name] = fn; },
  };
  class AudioContext {
    constructor() { if (unavailable) throw Error('audio_unavailable'); created++; this.state = 'suspended'; this.currentTime = 0; }
    resume() { this.state = 'running'; resumes++; return Promise.resolve(); }
    createOscillator() {
      const record = { frequency: {}, connect() {}, disconnect() {}, start(value) { this.startAt = value; }, stop(value) { this.stopAt = value; } };
      sounds.push(record); return record;
    }
    createGain() { return { gain: { setValueAtTime() {}, linearRampToValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect() {}, disconnect() {} }; }
  }
  vm.runInNewContext(source, { document, window: { AudioContext }, performance: { now: () => time } });
  return { document, events, sounds, advance: () => time += 1000, get created() { return created; }, get resumes() { return resumes; } };
}

test('no toggle or preference; browser audio waits for first gesture', () => {
  const f = fixture();
  f.events['tvr:stage']({ detail: 'step' });
  assert.equal(f.sounds.length, 0);
  assert.equal(f.created, 0);
});
test('sounds work by default after a pointer gesture and are short finite cues', () => {
  const f = fixture();
  f.events.pointerdown();
  f.events['tvr:stage']({ detail: 'step' });
  assert.equal(f.resumes, 1);
  assert.equal(f.sounds.length, 2);
  f.advance(); f.events['tvr:stage']({ detail: 'success' });
  assert.equal(f.sounds.length, 5);
  for (const cue of f.sounds) assert.ok(cue.stopAt - cue.startAt < .3);
  f.advance(); f.events['tvr:stage']({ detail: 'step' });
  assert.equal(f.sounds.length, 7);
});
test('keyboard gesture works; hidden pages and duplicate rapid events stay silent', () => {
  const f = fixture();
  f.events['tvr:stage']({ detail: 'success' });
  assert.equal(f.sounds.length, 0);
  f.events.keydown({ key: 'Enter' });
  f.events['tvr:stage']({ detail: 'step' });
  assert.equal(f.sounds.length, 2);
  f.events['tvr:stage']({ detail: 'success' });
  assert.equal(f.sounds.length, 2);
  f.advance(); f.document.hidden = true;
  f.events['tvr:stage']({ detail: 'success' });
  assert.equal(f.sounds.length, 2);
});
test('audio errors never interrupt wizard operations', () => {
  const f = fixture(true);
  assert.doesNotThrow(() => f.events.pointerdown());
  assert.doesNotThrow(() => f.events['tvr:stage']({ detail: 'success' }));
  assert.doesNotThrow(() => f.events.keydown({ key: 'Enter' }));
});
