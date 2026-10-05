import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {File} from 'node:buffer';
const ui=createRequire(import.meta.url)('../../studio/web/conversation-input.js');

test('conversation attachments stay with their selected project',()=>{
  const file=new File(['fixture'],'local.mp4');
  ui.setFiles('one',[file]);ui.setFiles('two',[]);
  assert.equal(ui.files('one')[0].name,'local.mp4');
  assert.deepEqual(ui.files('two'),[]);
  ui.clear('two');assert.equal(ui.files('one').length,1);ui.clear('one');
});

test('uncertain upload retry preserves bytes, filename and idempotent hash',async()=>{
  ui.setFiles('retry',[new File(['fixture'],'原视频.mp4')]);
  const calls=[];
  const request=async(url,options)=>{
    calls.push({url,file:options.body.get('file')});
    if(calls.length===1)throw Error('lost response');
    return {ok:true};
  };
  await assert.rejects(ui.upload('retry',request),/lost response/);
  assert.equal(ui.files('retry').length,1);
  assert.deepEqual(await ui.upload('retry',request),['原视频.mp4']);
  assert.equal(calls[0].url,calls[1].url);
  assert.match(calls[0].url,/retry_sha256=[a-f0-9]{64}$/);
  assert.equal(await calls[0].file.text(),await calls[1].file.text());ui.clear('retry');
});

test('failed upload keeps attachments and creates no conversation request',async()=>{
  ui.setFiles('failed',[new File(['fixture'],'local.mp4')]);
  const calls=[];
  await assert.rejects(ui.upload('failed',async(url)=>{calls.push(url);return {ok:false,json:async()=>({detail:'同名文件不同'})};}),/同名文件不同/);
  assert.equal(ui.files('failed').length,1);
  assert.ok(calls.every(url=>url.includes('/materials?')));ui.clear('failed');
});

test('adding reference keeps product uses and uploaded receipts, including removal and an unset use',async()=>{
  const product=new File(['product'],'product.mp4',{type:'video/mp4'});
  const reference=new File(['reference'],'reference.mp4',{type:'video/mp4'});
  ui.setFiles('append',[product],'product');
  const calls=[];
  const request=async(url)=>{calls.push(url);return {ok:true};};
  await ui.upload('append',request);
  ui.appendFiles('append',[reference],'reference');
  assert.deepEqual(ui.files('append').map(file=>file.name),['product.mp4','reference.mp4']);
  assert.deepEqual(ui.attachmentMetadata('append',await ui.upload('append',request)),[
    {name:'product.mp4',purpose:'product'},{name:'reference.mp4',purpose:'reference'},
  ]);
  assert.equal(calls.length,2,'the product receipt is retained instead of uploading twice');
  ui.setVideoUse('append',1,'');
  assert.equal(ui.missingVideoUse('append'),true);
  ui.setVideoUse('append',1,'product');
  assert.equal(ui.missingVideoUse('append'),false);
  assert.equal(ui.attachmentMetadata('append',['product.mp4','reference.mp4'])[1].purpose,'product');
  ui.clear('append');
});

test('attachment preflight rejects duplicates, limits and filenames before any upload',async()=>{
  const valid=new File(['bytes'],'product.png',{type:'image/png'});
  const cases=[
    [[valid,new File(['different'],'Product.PNG')],/重复附上/],
    [Array.from({length:25},(_,i)=>new File(['bytes'],`file-${i}.png`)),/24/],
    [[{name:'large.mp4',size:64*1024*1024+1}],/64 MB/],
    [[new File([],'empty.png')],/空文件/],
    [[new File(['bytes'],'CON.mp4')],/文件名/],
    [[new File(['bytes'],'a'.repeat(117)+'.png')],/文件名/],
    [[new File(['bytes'],'bad:name.mp4')],/文件名/],
    [[new File(['bytes'],'trailing.png ')],/文件名/],
    [[new File(['bytes'],'unsupported.heic')],/格式/],
  ];
  let calls=0;
  for(const [files,error] of cases){
    ui.setFiles('preflight',files);
    await assert.rejects(ui.upload('preflight',async()=>{calls++;return {ok:true};}),error);
  }
  assert.equal(calls,0);
  assert.doesNotThrow(()=>ui.validateFiles([new File(['bytes'],'家庭 种草.mp4')]));
  ui.clear('preflight');
});
