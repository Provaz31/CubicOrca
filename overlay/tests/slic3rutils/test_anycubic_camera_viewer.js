const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const code = fs.readFileSync(path.resolve(__dirname,'../../resources/web/anycubic_camera/viewer.js'),'utf8');
let checks = 0;
function fixture(options={}) {
  const posts=[], calls=[], clients=[], timers=new Map(), dom={}, container={replaceChildren(){calls.push('clear')}};
  let nextTimer=0;
  const sdk={setLogLevel:n=>assert.equal(n,4),disableLogUpload(){calls.push('no-log-upload')},createClient(opts){
    if(options.createError)throw Object.assign(new Error('PRIVATE_TOKEN'),{code:'NOT_SUPPORTED'});
    assert.deepEqual(JSON.parse(JSON.stringify(opts)),{mode:'live',codec:'h264'});
    const events={},trackEvents={};
    const track={on:(event,fn)=>trackEvents[event]=fn,stop:()=>calls.push('track-stop'),play(){calls.push('play');if(options.frameInPlay)trackEvents['first-frame-decoded']();}};
    const client={events,trackEvents,track,on:(event,fn)=>events[event]=fn,removeAllListeners(){calls.push('remove-listeners')},
      async setClientRole(role){assert.equal(role,'host');calls.push('host')},
      setEncryptionConfig(mode,key,salt){assert.equal(mode,'aes-128-gcm2');assert.equal(key,'offline-encryption');assert.equal(salt.length,32);calls.push('encryption')},
      async join(){calls.push('join');if(options.cryptInJoin)events['crypt-error']();if(options.joinDisconnect)events['connection-state-change']('DISCONNECTED');if(options.joinError)throw Object.assign(new Error('PRIVATE_TOKEN'),{code:options.joinCode || 'PRIVATE_TOKEN'});if(options.join)await options.join;
        if(options.publishInJoin)await events['user-published']({uid:456,videoTrack:track},'video');},
      async subscribe(user,kind){assert.equal(kind,'video');calls.push('subscribe')},async leave(){calls.push('leave')},
      publish(){throw new Error('Publishing forbidden')}
    };clients.push(client);return client;
  }};
  const window=options.lateBridge ? {} : {camera:{postMessage:raw=>posts.push(JSON.parse(raw))}};
  const context=vm.createContext({window,AgoraRTC:sdk,document:{getElementById:()=>container,addEventListener:(name,fn)=>dom[name]=fn},
    Uint8Array,atob:raw=>Buffer.from(raw,'base64').toString('binary'),setTimeout:fn=>{timers.set(++nextTimer,fn);return nextTimer},clearTimeout:id=>timers.delete(id)});
  vm.runInContext(code,context);
  return {window,posts,calls,clients,timers,dom};
}
const config=()=>({appid:'a'.repeat(32),channel:'offline-channel',token:'PRIVATE_TOKEN',uid:123,device_uid:456,
  encryption_mode:'aes-128-gcm2',encryption_key:'offline-encryption',salt:Buffer.alloc(32).toString('base64')});
(async()=>{
  let f=fixture();assert.equal(f.clients.length,0);assert.deepEqual(f.posts,[{event:'ready',generation:0}]);checks++;
  await f.window.orcaCamera.start(config(),1);assert(f.calls.indexOf('host')<f.calls.indexOf('join'));
  assert(f.calls.indexOf('encryption')<f.calls.indexOf('join'));assert.equal(f.posts.length,1);checks++;
  let c=f.clients[0];await c.events['user-published']({uid:999,videoTrack:c.track},'video');
  await c.events['user-published']({uid:456,videoTrack:c.track},'audio');assert(!f.calls.includes('subscribe'));checks++;
  await c.events['user-published']({uid:456,videoTrack:c.track},'video');assert(f.calls.includes('play'));
  assert(!f.posts.some(p=>p.event==='playing'));c.trackEvents['first-frame-decoded']();
  assert(f.posts.some(p=>p.event==='playing'&&p.generation===1));assert.equal(f.timers.size,0);checks++;
  c.events['user-unpublished']({uid:999},'video');assert(!f.posts.some(p=>p.event==='error'));checks++;
  await f.window.orcaCamera.stop(2);assert(f.calls.includes('leave'));assert.equal(f.timers.size,0);
  let prevented=false;f.dom.keydown({key:'Escape',preventDefault(){prevented=true}});
  assert(prevented);assert.equal(f.posts.at(-1).event,'exit_fullscreen');assert.equal(f.posts.at(-1).generation,2);checks++;
  let release;const delayed=new Promise(r=>release=r);f=fixture({join:delayed});
  const opening=f.window.orcaCamera.start(config(),10);for(let n=0;n<6;n++)await Promise.resolve();
  await f.window.orcaCamera.stop(11);release();await opening;
  assert(!f.calls.includes('play'));assert(!f.posts.some(p=>p.event==='playing'));checks++;
  f=fixture({joinError:true});await f.window.orcaCamera.start(config(),20);
  assert(f.posts.some(p=>p.event==='error'));assert(!JSON.stringify(f.posts).includes('PRIVATE_TOKEN'));assert.equal(f.posts.at(-1).stage,'join');assert.equal(f.posts.at(-1).reason,'sdk_error');checks++;
  f=fixture({frameInPlay:true,publishInJoin:true});await f.window.orcaCamera.start(config(),30);
  assert(f.posts.some(p=>p.event==='playing'));assert.equal(f.timers.size,0);checks++;
  f=fixture();await f.window.orcaCamera.start(config(),40);assert.equal(f.timers.size,1);
  [...f.timers.values()][0]();assert(f.posts.some(p=>p.event==='error'));checks++;
  f=fixture({lateBridge:true});assert.equal(f.posts.length,0);
  f.window.camera={postMessage:raw=>f.posts.push(JSON.parse(raw))};
  [...f.timers.values()][0]();assert.equal(f.posts[0].event,'ready');checks++;
  f=fixture({createError:true});await f.window.orcaCamera.start(config(),50);
  assert.equal(f.posts.at(-1).stage,'create');assert.equal(f.posts.at(-1).reason,'NOT_SUPPORTED');checks++;
  f=fixture({joinError:true,joinCode:'INVALID_TOKEN'});await f.window.orcaCamera.start(config(),60);
  assert.equal(f.posts.at(-1).reason,'INVALID_TOKEN');assert(!JSON.stringify(f.posts).includes('PRIVATE_TOKEN'));checks++;
  f=fixture();await f.window.orcaCamera.start(config(),70);
  f.dom.securitypolicyviolation({effectiveDirective:'worker-src',blockedURI:'PRIVATE_TOKEN'});
  assert.equal(f.posts.at(-1).stage,'csp');assert.equal(f.posts.at(-1).reason,'CSP_WORKER');
  assert(!JSON.stringify(f.posts).includes('PRIVATE_TOKEN'));checks++;
  f=fixture({joinDisconnect:true,joinError:true,joinCode:'INVALID_TOKEN'});await f.window.orcaCamera.start(config(),80);
  assert.equal(f.posts.filter(p=>p.event==='error').length,1);assert.equal(f.posts.at(-1).stage,'join');
  assert.equal(f.posts.at(-1).reason,'INVALID_TOKEN');checks++;
  // A no-argument crypt-error during startup must not tear down the lease
  // before a frame can arrive. Real SDK callbacks do not carry an Error.
  f=fixture();await f.window.orcaCamera.start(config(),90);c=f.clients[0];
  c.events['crypt-error']();c.events['crypt-error']();
  assert.equal(f.posts.filter(p=>p.event==='waiting_encryption').length,1);
  assert(!f.posts.some(p=>p.event==='error'));assert.equal(f.timers.size,1);
  await c.events['user-published']({uid:456,videoTrack:c.track},'video');
  c.trackEvents['first-frame-decoded']();assert.equal(f.timers.size,0);
  assert.equal(f.posts.at(-1).event,'playing');
  assert.equal(f.calls.filter(call=>call==='encryption').length,1);
  assert.equal(f.calls.filter(call=>call==='join').length,1);checks++;
  // Persistent failures remain bounded and distinguish decryption from
  // configuration errors without exposing remote exception contents.
  f=fixture();await f.window.orcaCamera.start(config(),91);c=f.clients[0];
  c.events['crypt-error']();[...f.timers.values()][0]();
  assert.equal(f.posts.at(-1).stage,'encryption');
  assert.equal(f.posts.at(-1).reason,'DECRYPTION_FAILED');checks++;
  f=fixture({frameInPlay:true,publishInJoin:true});await f.window.orcaCamera.start(config(),92);
  f.clients[0].events['crypt-error']();assert.equal(f.posts.at(-1).reason,'DECRYPTION_FAILED');checks++;
  f=fixture();await f.window.orcaCamera.start(config(),93);c=f.clients[0];
  c.events['crypt-error']();await f.window.orcaCamera.stop(94);const stoppedPosts=f.posts.length;
  c.events['crypt-error']();assert.equal(f.posts.length,stoppedPosts);assert.equal(f.timers.size,0);checks++;
  f=fixture({cryptInJoin:true,frameInPlay:true,publishInJoin:true});
  await f.window.orcaCamera.start(config(),95);
  assert(!f.posts.some(p=>p.event==='error'));assert.equal(f.posts.at(-1).event,'playing');
  assert.equal(f.timers.size,0);checks++;
  f=fixture({cryptInJoin:true,joinError:true,joinCode:'INVALID_TOKEN'});
  await f.window.orcaCamera.start(config(),96);
  assert.equal(f.posts.at(-1).stage,'join');assert.equal(f.posts.at(-1).reason,'INVALID_TOKEN');
  assert.equal(f.timers.size,0);checks++;
  console.log(`${checks} receive-only viewer checks passed. No network or camera accessed.`);
})().catch(e=>{console.error(e);process.exitCode=1});
