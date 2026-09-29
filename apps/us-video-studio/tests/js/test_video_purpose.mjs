import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import fs from 'node:fs';

const require=createRequire(import.meta.url);
const composer=require('../../studio/web/conversation-input.js');

test('video attachment asks for a use before it can enter the conversation',()=>{
  const run='video-purpose-test';
  composer.setFiles(run,[{name:'product.mp4',type:'video/mp4'},{name:'photo.jpg',type:'image/jpeg'}]);
  assert.equal(composer.missingVideoUse(run),true);
  composer.setVideoUse(run,0,'product');
  assert.equal(composer.missingVideoUse(run),false);
  assert.equal(composer.describe(run,['product.mp4','photo.jpg']),
    'product.mp4（商品视频，用于补全商品信息）、photo.jpg');
  composer.clear(run);
});

test('reference use is explicit and a new selection cannot inherit the previous use',()=>{
  const run='reference-purpose-test';
  composer.setFiles(run,[{name:'source.mov',type:''}]);
  assert.equal(composer.missingVideoUse(run),true);
  composer.setVideoUse(run,0,'reference');
  assert.equal(composer.describe(run,['source.mov']),'source.mov（参考视频，用于拆解表达）');
  composer.setFiles(run,[{name:'another.mov',type:''}]);
  assert.equal(composer.missingVideoUse(run),true);
  composer.clear(run);
});

test('both workspaces show the purpose before upload and the selected path labels video',()=>{
  const html=fs.readFileSync(new URL('../../studio/web/index.html',import.meta.url),'utf8');
  for(const id of ['start-attach','start-reference-attach','chat-attach','chat-reference-attach'])
    assert.match(html,new RegExp(`id="${id}"`));
  assert.match(html,/＋ 商品图片\/视频/);
  assert.match(html,/＋ 参考视频/);
  const run='visible-purpose-test';
  composer.setFiles(run,[{name:'item.mp4',type:'video/mp4'}],'product');
  assert.equal(composer.missingVideoUse(run),false);
  assert.match(composer.describe(run,['item.mp4']),/商品视频/);
  composer.setFiles(run,[{name:'viral.mp4',type:'video/mp4'}],'reference');
  assert.equal(composer.missingVideoUse(run),false);
  assert.match(composer.describe(run,['viral.mp4']),/参考视频/);
  composer.clear(run);
});
