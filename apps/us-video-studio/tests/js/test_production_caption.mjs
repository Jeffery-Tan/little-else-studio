import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';

const require=createRequire(import.meta.url),ui=require('../../studio/web/production.js');
const oldHash='b'.repeat(64),newHash='a'.repeat(64);
const movie={name:'old.mp4',sha256:newHash,kind:'video',url:'/api/runs/demo/materials/old.mp4'};
const base={draft_id:'old',kind:'video',source_version:1,source_sha256:oldHash,source_ready:true,
  reference_approved:true,review:{verdict:'ready'},attempts:[{attempt_id:'done',status:'succeeded',output:movie}]};
const data={drafts:[base],materials:[movie],capabilities:{video:{available:true}},sources:[]};
const writing=(version,sha,text)=>({stage:'writing',has_doc:true,version,artifact_sha256:sha,integrity_ok:true,
  blocks:text?[{type:'text',title:'发布文案',text}]:[]});
const archived=(text)=>({status:'ready',artifact:{version:1,sha256:oldHash,integrity_ok:true,
  blocks:text?[{type:'text',title:'发布文案',text}]:[]}});

test('same-version video and caption pair only with the exact intact script fingerprint',()=>{
  const work=ui.currentProductionWork(data,'video','demo'),same=writing(1,oldHash,'同版配文');
  assert.equal(ui.captionMatches(same,work),true);
  const html=ui.captionHtml([same],work);
  assert.match(html,/当前视频对应的发布文案 · 脚本第 1 版/);
  assert.match(html,/同版配文/);
  assert.match(html,/复制这段视频的配文/);
  assert.doesNotMatch(html,/尚未对应当前视频/);
  for(const changed of [{version:2},{artifact_sha256:newHash},{integrity_ok:false}])
    assert.equal(ui.captionMatches({...same,...changed},work),false);
});

test('old successful video keeps its archived caption when a changed script generation fails',()=>{
  const failed={...base,draft_id:'new',source_version:2,source_sha256:newHash,
    attempts:[{attempt_id:'failed',status:'failed',message:'新版未生成'}]};
  const work=ui.currentProductionWork({...data,drafts:[base,failed]},'video','demo');
  assert.equal(work.phase,'failed');assert.equal(work.displaySourceVersion,1);
  assert.equal(work.displaySourceSha256,oldHash);
  const current=writing(2,newHash,'新版文案');
  const html=ui.captionHtml([current],work,archived('旧视频配文'));
  assert.match(html,/当前视频对应的发布文案 · 脚本第 1 版[\s\S]*旧视频配文/);
  assert.match(html,/新版文案 · 脚本第 2 版[\s\S]*尚未对应当前视频/);
  assert.match(html,/复制这段视频的配文/);
  assert.match(html,/复制未配对文案/);
  assert.ok(html.indexOf('旧视频配文')<html.indexOf('新版文案 · 脚本第 2 版'));
  const wrong={...archived('不应出现'),artifact:{...archived('不应出现').artifact,sha256:newHash}};
  assert.doesNotMatch(ui.captionHtml([current],work,wrong),/不应出现/);
});

test('unknown new result preserves old video pairing and original-task recovery',()=>{
  const pending={...base,draft_id:'new',source_version:2,source_sha256:newHash,
    attempts:[{attempt_id:'unknown',status:'unknown',task_id:'provider-task'}]};
  const work=ui.currentProductionWork({...data,drafts:[base,pending]},'video','demo');
  assert.equal(work.phase,'unknown');assert.equal(work.primaryAction,'refresh');
  assert.equal(work.displaySourceSha256,oldHash);
  const html=ui.captionHtml([writing(2,newHash,'新稿尚无视频')],work,archived('旧视频配文'));
  assert.match(html,/旧视频配文/);assert.match(html,/新稿尚无视频/);
  assert.match(html,/尚未对应当前视频/);
});

test('missing old caption is explicit while a newer caption remains visibly unpaired',()=>{
  const work=ui.currentProductionWork(data,'video','demo');
  const current=writing(2,newHash,'新版文案');
  const missing=ui.captionHtml([current],work,{status:'missing'});
  assert.match(missing,/找不到可核对的同版配文/);
  assert.match(missing,/新版文案 · 脚本第 2 版[\s\S]*尚未对应当前视频/);
  assert.equal((missing.match(/data-copy-caption/g)||[]).length,1);
  const noBlock=ui.captionHtml([current],work,archived(''));
  assert.match(noBlock,/这段视频对应的脚本没有独立发布文案/);
  assert.doesNotMatch(noBlock,/复制这段视频的配文/);
});
