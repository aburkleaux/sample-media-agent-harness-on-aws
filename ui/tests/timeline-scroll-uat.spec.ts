import { test, expect, Page, Route } from '@playwright/test';
const BASE='http://localhost:3000', CDN='https://dexamplecdn002.cloudfront.net';
const RUN=`sc-${Date.now()}`, SID=`${RUN}-11111111-2222-3333-444444444444`, SK=`${RUN}/meridian.mp4`;
const json=(r:Route,b:unknown)=>r.fulfill({status:200,contentType:'application/json',body:JSON.stringify(b)});
async function dev(p:Page){await p.goto(BASE);await p.waitForTimeout(700);const e=p.locator("button:has-text('Enter the harness'),button:has-text('Explore the harness'),button:has-text('Sign in')").first();if(await e.isVisible({timeout:5000}).catch(()=>false)){await e.click();await p.waitForTimeout(800);}const d=p.locator("button:has-text('Dev Login')");if(await d.isVisible({timeout:6000}).catch(()=>false)){await d.click();await p.waitForTimeout(1500);}}
test('tall timeline makes the render panel vertically scrollable (#4)', async ({page})=>{
  test.setTimeout(60000);
  await page.setViewportSize({width: 1000, height: 640}); // force overflow
  await page.route('**/jobs**',(r)=>{const u=r.request().url();if(/\/jobs(\?|$|\/)/.test(u)&&!u.includes('/jobs/stream'))return json(r,{jobs:[]});return r.continue();});
  await page.route('**/jobs/stream**',(r)=>r.fulfill({status:200,contentType:'text/event-stream',body:''}));
  const markers=Array.from({length:15},(_,i)=>({id:`m-${i}`,label:String(i),category:'content',time:i*5,endTime:i*5+5}));
  const segments=[{start:2,end:18,group:'SPEAKER_00'},{start:22,end:40,group:'SPEAKER_01'}];
  const anns=Array.from({length:5},(_,e)=>({entityId:`entity-${e}`,start:e*8,end:e*8+10,box:{x:0.1,y:0.1,width:0.2,height:0.3},confidence:0.9}));
  const manifest={id:'c',template:'composite-timeline',duration:80,createdAt:'2026-08-27T10:00:00Z',sourceKey:SK,clips:[],peaks:{'audio-music':[0.2,0.5]},tracks:[{id:'video-1',type:'video',src:`${CDN}/x.mp4`,start:0,end:80},{id:'audio-music',type:'audio',label:'Audio Music',src:`${CDN}/m.wav`,start:0,end:80}],markers,segments,annotations:anns};
  await page.route(`**/sessions/${SID}/renders**`,(r)=>json(r,{sessionId:SID,renders:[{key:`composite:${SK}`,composite:true,sourceKey:SK,memberKeys:['a'],createdAt:'2026-08-27T10:00:00Z',label:'Meridian',manifest}]}));
  await page.request.post(`${BASE}/sessions`,{data:{sessionId:SID,title:SID}}).catch(()=>{});
  await dev(page);
  const link=page.locator(`text=${SID}`).first(); if(await link.isVisible({timeout:12000}).catch(()=>false)) await link.click();
  await expect(page.locator('[data-testid="unified-timeline"]').first()).toBeVisible({timeout:20000});
  const info=await page.locator('[data-testid="renderer-content"]').first().evaluate((el)=>{
    const c=el as HTMLElement; const cs=getComputedStyle(c);
    // walk ancestors for overflow/height
    const chain:any[]=[]; let n:HTMLElement|null=c;
    for(let i=0;i<5 && n;i++){const s=getComputedStyle(n);chain.push({tag:n.tagName,testid:n.getAttribute('data-testid'),h:Math.round(n.clientHeight),sh:n.scrollHeight,overflowY:s.overflowY,flex:s.flex,minH:s.minHeight});n=n.parentElement;}
    return {contentClientH:c.clientHeight,contentScrollH:c.scrollHeight,overflowY:cs.overflowY,scrollable:c.scrollHeight>c.clientHeight+4,chain};
  });
  // #4 — a tall composite must make the render panel VERTICALLY SCROLLABLE. The container used to be
  // a flex column that shrank+clipped its children (NleTimeline overflow:hidden); now it's a block
  // scroll container so the lanes below (face timeline) are reachable.
  expect(info.overflowY, 'renderer-content is a scroll container').toBe('auto');
  expect(info.scrollable, 'renderer content scrolls when the timeline overflows the panel').toBe(true);
});
