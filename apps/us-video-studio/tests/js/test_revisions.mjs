import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url),ui=require('../../studio/web/revisions.js');
const data={stage:'direction',editable:true,columns:['claim','hook'],labels:{claim:'主张',hook:'开场'},rows:[{key:'A',claim:'One',hook:'First'},{key:'B',claim:'Two',hook:'Second'}]};
test('collect only explicit changed cells; preserve other objects',()=>{
 assert.deepEqual(ui.cells(data,{'0:claim':'One','0:hook':'New','1:key':'Z','99:hook':'extra'}),[{row:0,field:'hook',value:'New'}]);
});
test('editor binds displayed object and escapes all model and user fields',()=>{
 const html=ui.editor(data,1,{'1:hook':'<script>bad</script>'});
 assert.match(html,/data-row="1"/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
 assert.match(ui.editor({...data,editable:false},0),/<fieldset disabled>/);
});
test('version comparison keeps both sides, unknowns and prose truncation explicit',()=>{
 const html=ui.comparison({from_version:1,to_version:2,changes:[{path:['options',0,'hook'],before:'Old',after:'<img onerror=x>'}],prose_diff:'-Old\n+New',prose_truncated:true,inputs_changed:true},data.labels);
 assert.match(html,/版本 1 → 版本 2/);assert.match(html,/依据也有变化/);assert.match(html,/仅显示部分差异/);
 assert.match(html,/Old/);assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<img/);
});
