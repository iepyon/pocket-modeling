// グリル質問パネル（templates/grill-panel.html）の動作確認。window.claude の db / user / comments を模擬し、
// 「これまでの回答」からの選び直し・反映待ちの経過時間・通知を確かめて、結果を JSON で標準出力に出す。
// 使い方: node grill_panel_check.js <eventstorming_build.py --artifact で作った HTML> [スクリーンショットの出力先]
// test_session_findings.py の GrillPanelBrowserTest から呼ばれる。
const { chromium } = require('playwright');
const pagePath = process.argv[2];
const outDir = process.argv[3] || '';

const MOCK = `
(function(){
  const now = Date.now();
  const store = {
    'grill/current': { questions: [ {qid:'q-2', text:'二問目？', options:[{label:'はい'},{label:'いいえ'}]} ] },
    'grill/progress': { reflected: { 'q-1': { at: new Date(now-60000).toISOString(), summary:'一問目を反映', decisions:['D3'] } } },
  };
  const answers = {
    'q-1--me': { qid:'q-1', by:'me', choices:['キャラが接客'], choice:'キャラが接客', text:'', hint:false,
                 question:'接客はどうする？', phase:'1', at:new Date(now-120000).toISOString(),
                 options:['キャラが接客','人が接客'], multi:false },
    'q-0--me': { qid:'q-0', by:'me', choices:['a'], text:'', hint:false, question:'古い質問',
                 at:new Date(now-10*60000).toISOString() },
  };
  const subs = { answers:[] };
  function snapDoc(k){ return { exists: k in store, data: () => store[k], metadata:{fromCache:false} }; }
  function fireAnswers(){
    const docs = Object.values(answers).sort((a,b)=>a.at<b.at?1:-1).map(d=>({data:()=>d}));
    subs.answers.forEach(f=>f({docs}));
  }
  const db = {
    doc(k){ return { onSnapshot(cb){ setTimeout(()=>cb(snapDoc(k)),0); return ()=>{}; } }; },
    collection(name){ return {
      orderBy(){ return this; }, limit(){ return this; },
      onSnapshot(cb){ subs.answers.push(cb); setTimeout(fireAnswers,0); return ()=>{}; },
      doc(id){ return { set(d){
        answers[id]=JSON.parse(JSON.stringify(d));
        window.__written = window.__written||[]; window.__written.push({id, d:answers[id]});
        setTimeout(fireAnswers,0); return Promise.resolve(); } }; }
    }; }
  };
  window.__notified = 0;
  const comments = {
    canSendToClaude: ()=>Promise.resolve('available'),
    anchorFor: ()=>Promise.resolve({}),
    sendToClaude: ()=>{ window.__notified++; return Promise.resolve({threadId:'t1'}); }
  };
  const user = { id: ()=>Promise.resolve('me'), can: ()=>Promise.resolve(true), profiles: ()=>Promise.resolve({}) };
  window.claude = { use(n){ return Promise.resolve({db, user, comments}[n]); } };
})();`;

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 900, height: 1400 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e)));
  await p.addInitScript(MOCK);
  await p.goto('file://' + pagePath);
  await p.waitForSelector('#grill .gr-q');
  const out = {};
  out.q = await p.textContent('#grill .gr-q');
  await p.click('#grill-foot summary');
  out.log = await p.$$eval('#grill-log li', ls => ls.map(l => l.innerText.replace(/\n/g, ' | ')));
  out.model = await p.innerText('#grill-model');
  if (outDir) await p.screenshot({ path: outDir + '/panel1.png', clip: { x: 0, y: 0, width: 900, height: 1100 } });

  // 反映済みで列から外れた q-1 を選び直す
  const links = await p.$$('#grill-log .gr-link');
  const texts = await Promise.all(links.map(l => l.evaluate(e => e.closest('li').innerText)));
  await links[texts.findIndex(t => t.includes('接客はどうする'))].click();
  out.formQ = await p.textContent('#grill .gr-q');
  out.note = await p.textContent('.gr-redo-note');
  out.pressed = await p.$$eval('.gr-opt', bs => bs.map(x => x.innerText.replace(/\n/g, ' ') + ':' + x.getAttribute('aria-pressed')));
  await p.click('.gr-opt:nth-child(2)');
  await p.click('#grill-send');
  await p.waitForTimeout(300);
  out.written = await p.evaluate(() => window.__written);
  out.strip = await p.textContent('#grill-strip');
  out.qAfter = await p.textContent('#grill .gr-q');
  out.modelAfter = await p.innerText('#grill-model');
  out.notified = await p.evaluate(() => window.__notified);
  if (outDir) await p.screenshot({ path: outDir + '/panel2.png', clip: { x: 0, y: 0, width: 900, height: 1100 } });
  out.errs = errs;
  console.log(JSON.stringify(out, null, 1));
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
