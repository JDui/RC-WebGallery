const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
function app() {
  const context = vm.createContext({localStorage:{getItem:()=>null},URL,Intl,Date,URLSearchParams});
  vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../app/static/app.js'),'utf8'),context);
  return context.galleryApp();
}

test('xhs content uses the author instead of the subscription name', () => {
  const state=app();
  assert.equal(state.creatorName({folder_name:'xhs_1_note',subscription_name:'我的小红书点赞',author_name:'笔记作者'}),'笔记作者');
  assert.equal(state.creatorName({folder_name:'xhs_1_note',subscription_name:'我的小红书点赞',author_id:'author-id'}),'作者 author-id');
  assert.equal(state.creatorName({subscription_name:'UP 主'}),'UP 主');
});

test('author names link to profiles and unsupported URL schemes stay unlinked', () => {
  const state=app();
  assert.equal(state.creatorProfileUrl({author_id:'author-id'}),'https://www.xiaohongshu.com/user/profile/author-id');
  assert.equal(state.creatorProfileUrl({subscription_uid:'123'}),'https://space.bilibili.com/123');
  assert.equal(state.creatorProfileUrl({creator_url:'https://example.test/blog/'}),'https://example.test/blog/');
  assert.equal(state.creatorProfileUrl({creator_url:'javascript:alert(1)'}),'');
  assert.equal(state.creatorProfileUrl({creator_url:'https://name:password@example.test/'}),'');
  assert.equal(state.creatorProfileUrl({folder_name:'xhs_1_unknown'}),'');
});
