/* Local, receive-only viewer. Never persist credentials or echo SDK errors. */
(() => {
  'use strict';
  let generation = 0, client = null, videoTrack = null, frameTimer = null;
  const notify = (event, gen = generation, diagnostic = {}) => {
    const body = JSON.stringify({event, generation: gen, ...diagnostic});
    if (window.camera && window.camera.postMessage) window.camera.postMessage(body);
  };
  const errorCodes = new Set(['INVALID_TOKEN','TOKEN_EXPIRED','CAN_NOT_GET_GATEWAY_SERVER','NETWORK_ERROR',
    'NETWORK_TIMEOUT','NOT_SUPPORTED','INVALID_PARAMS','UID_CONFLICT','OPERATION_ABORTED',
    'SUBSCRIBE_FAILED','REMOTE_USER_IS_NOT_PUBLISHED','UNEXPECTED_RESPONSE','WEB_SECURITY_RESTRICT',
    'PERMISSION_DENIED','ICE_FAILED','DISCONNECTED','DECRYPTION_FAILED']);
  const safeCode = error => error && errorCodes.has(error.code) ? error.code : 'sdk_error';
  AgoraRTC.setLogLevel(4);
  AgoraRTC.disableLogUpload();
  async function stop(next) {
    generation = next;
    if (frameTimer !== null) clearTimeout(frameTimer);
    frameTimer = null;
    const old = client;
    client = null;
    if (videoTrack) videoTrack.stop();
    videoTrack = null;
    document.getElementById('video').replaceChildren();
    if (old) {
      old.removeAllListeners();
      try { await old.leave(); } catch (_) { /* Credentials stay private. */ }
    }
  }
  async function start(config, next) {
    await stop(next);
    if (generation !== next) return;
    let sawFrame = false, subscribedUid = null, cryptFailed = false;
    let mine = null, phase = 'create', joined = false;
    const current = () => generation === next && (mine === null || client === mine);
    const failed = (stage, error) => {
      if (current()) notify('error', next, {stage, reason: safeCode(error)});
    };
    try {
      mine = AgoraRTC.createClient({mode: 'live', codec: 'h264'});
      client = mine;
      // This callback has no error argument. A gateway may report encrypted
      // packets before the newly opened printer session is ready. Keep the
      // same lease and its encryption settings until the first-frame deadline;
      // never retry with plaintext or reopen the camera automatically.
      mine.on('crypt-error', () => {
        if (!current()) return;
        if (sawFrame) failed('encryption', {code:'DECRYPTION_FAILED'});
        else {
          if (!cryptFailed) notify('waiting_encryption', next);
          cryptFailed = true;
        }
      });
      mine.on('token-privilege-did-expire', () => failed('token', {code:'TOKEN_EXPIRED'}));
      mine.on('connection-state-change', state => {
        if (current() && joined && state === 'DISCONNECTED') failed('connection', {code:'DISCONNECTED'});
      });
      mine.on('user-published', async (user, kind) => {
        if (!current() || kind !== 'video' ||
            config.device_uid !== null && String(user.uid) !== String(config.device_uid) ||
            subscribedUid !== null && String(user.uid) !== subscribedUid) return;
        subscribedUid = String(user.uid);
        try {
          await mine.subscribe(user, 'video');
          if (!current()) return;
          videoTrack = user.videoTrack;
          videoTrack.on('first-frame-decoded', () => {
            if (!current()) return;
            sawFrame = true;
            clearTimeout(frameTimer);
            frameTimer = null;
            notify('playing', next);
          });
          videoTrack.play(document.getElementById('video'), {fit: 'contain', mirror: false});
        } catch (error) { failed('subscribe', error); }
      });
      mine.on('user-unpublished', (user, kind) => {
        if (current() && kind === 'video' && String(user.uid) === subscribedUid)
          failed('connection', {code:'DISCONNECTED'});
      });
      phase = 'role';
      // Match the known-working Anycubic client role. Role alone creates no
      // media: no local tracks, audio subscription or publish call exists here.
      await mine.setClientRole('host');
      phase = 'encryption';
      if (config.encryption_mode !== 'none') {
        const salt = config.salt ? Uint8Array.from(atob(config.salt), c => c.charCodeAt(0)) : undefined;
        mine.setEncryptionConfig(config.encryption_mode, config.encryption_key, salt);
      }
      if (!current()) { await mine.leave(); return; }
      phase = 'join';
      await mine.join(config.appid, config.channel, config.token, config.uid);
      joined = true;
      if (!current()) { await mine.leave(); return; }
      config = {device_uid: config.device_uid};
      if (!sawFrame) frameTimer = setTimeout(() => {
        if (cryptFailed) failed('encryption', {code:'DECRYPTION_FAILED'});
        else failed('frames', {code:'NETWORK_TIMEOUT'});
      }, 30000);
    } catch (error) { failed(phase, error); }
  }
  document.addEventListener('securitypolicyviolation', event => {
    if (!client) return;
    const codes = {'script-src':'CSP_SCRIPT','script-src-elem':'CSP_SCRIPT','worker-src':'CSP_WORKER',
      'connect-src':'CSP_CONNECT','media-src':'CSP_MEDIA'};
    notify('error', generation, {stage:'csp',reason:codes[event.effectiveDirective] || 'CSP_OTHER'});
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); notify('exit_fullscreen'); }
  });
  window.orcaCamera = {start, stop};
  // WebView2 can finish attaching its private message bridge after page load.
  let readyAttempts = 0;
  function ready() {
    if (window.camera && window.camera.postMessage) notify('ready', 0);
    else if (++readyAttempts < 100) setTimeout(ready, 100);
  }
  ready();
})();
