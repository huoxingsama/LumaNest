const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../src/personal_cinema/web/app.js');

test('filter searches labels, help and stable keys without changing section', () => {
  assert.equal(typeof ui.filterFields, 'function');
  const fields = [{key:'playback.brightness',label:'亮度',help:'HDR 画面调节',section:'playback'}, {key:'cache.limit',label:'容量',help:'磁盘',section:'cache'}];
  assert.deepEqual(ui.filterFields(fields,'playback',' hdr '), [fields[0]]);
  assert.deepEqual(ui.filterFields(fields,'cache','亮度'), []);
});

test('number validation rejects blank, nonfinite and out-of-range values', () => {
  assert.equal(typeof ui.parseValue, 'function');
  const field={kind:'int',label:'缓冲',min:1,max:100};
  assert.deepEqual(ui.parseValue(field,'10'), {value:10});
  for (const value of ['', 'Infinity', '0', '101', '2.5']) assert.ok(ui.parseValue(field,value).error);
});

test('effective scoped values inherit globals, retain false and separate drafts', () => {
  assert.equal(typeof ui.effectiveValues, 'function');
  const config={values:{a:1,b:true},overrides:{device:{a:4,b:false},'item:42':{a:8}}};
  assert.deepEqual(ui.effectiveValues(config,'device',{a:9}),{a:9,b:false});
  assert.deepEqual(config.values,{a:1,b:true});
  assert.deepEqual(ui.effectiveValues(config,'item:42'),{a:8,b:true});
});

test('source entry helper validates identity and never accepts a root drive silently', () => {
  assert.equal(typeof ui.sourceEntry, 'function');
  assert.ok(ui.sourceEntry({id:'',name:'A',kind:'local',path:'C:/Movies'}).error);
  assert.ok(ui.sourceEntry({id:'a',name:'A',kind:'webdav',path:''}).error);
  assert.ok(ui.sourceEntry({id:'a',name:'A',kind:'local',path:'C:/'}).error);
  assert.equal(ui.sourceEntry({id:'movies',name:'电影',kind:'local',path:'C:/Movies',enabled:true,allow_write:false}).value.id,'movies');
});

test('plain error formatter does not present objects as useful user messages', () => {
  assert.equal(typeof ui.errorMessage, 'function');
  assert.equal(ui.errorMessage({error:{message:'连接失败'}}),'连接失败');
  assert.equal(ui.errorMessage({}), '操作未完成，请重试。');
});
