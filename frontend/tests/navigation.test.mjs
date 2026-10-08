import test from 'node:test';
import assert from 'node:assert/strict';
import {filtersFor,libraryTitle,cloudPlan} from '../src/lib/navigation.js';
test('each library category sends an actual distinct filter',()=>{
  for(const kind of ['liked','saved','collection','creator','link']) assert.deepEqual(filtersFor(kind,''),{source_kinds:[kind]});
  assert.deepEqual(filtersFor('collection','s_fixture'),{source_kinds:['collection'],scope_id:'s_fixture'});
  assert.deepEqual(filtersFor('',''),{});
  assert.equal(libraryTitle('liked'),'喜欢');
});
test('cloud request count comes from prepared inputs and unknown inputs cannot authorize a batch',()=>{
  assert.equal(cloudPlan([]),null);
  assert.equal(cloudPlan([{planned_calls_before_reuse:null}]),null);
  assert.deepEqual(cloudPlan([{planned_calls_before_reuse:5},{planned_calls_before_reuse:3}]),{perItem:5,total:8});
});
