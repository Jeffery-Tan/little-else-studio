import {createRequire} from 'node:module';
import test from 'node:test';
import assert from 'node:assert/strict';
const require=createRequire(import.meta.url);
global.escapeHtml=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const {deletionPromptHtml}=require('../../studio/web/project-management.js');
test('delete confirmation names the exact project safely and explains recovery',()=>{
  const html=deletionPromptHtml({title:'<script>bad</script>'});
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(html.includes('回收站'));
  assert.ok(html.includes('恢复'));
  assert.ok(html.includes('data-cancel'));
  assert.ok(html.includes('data-confirm'));
});
