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
