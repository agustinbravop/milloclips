import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const catalog = JSON.parse(readFileSync(new URL('../static/catalog.json', import.meta.url), 'utf8'));

const clips = (vod) => [...(vod.long ? [vod.long] : []), ...vod.shorts];

test('every stream has the fields the rundown renders', () => {
  assert.ok(catalog.vods.length > 0);
  for (const vod of catalog.vods) {
    assert.match(vod.date, /^\d{4}-\d{2}-\d{2}$/, vod.title);
    assert.match(vod.startTime, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/, vod.title);
    assert.ok(Number.isFinite(vod.duration) && vod.duration > 0, vod.title);
    assert.match(vod.kickUrl, /^https:\/\/kick\.com\//, vod.title);
    assert.ok(Array.isArray(vod.shorts), vod.title);
  }
});

test('stream and clip ids are unique', () => {
  const seen = new Set();
  for (const vod of catalog.vods) {
    assert.ok(!seen.has(vod.id), `duplicate stream ${vod.id}`);
    seen.add(vod.id);
    for (const clip of clips(vod)) {
      assert.ok(!seen.has(clip.id), `duplicate clip ${clip.id}`);
      seen.add(clip.id);
    }
  }
});

test('every clip carries the metadata printed under the player', () => {
  for (const vod of catalog.vods) {
    for (const clip of clips(vod)) {
      assert.ok(clip.title, clip.id);
      assert.match(clip.poster, /^\/images\/.+\.(jpg|png|webp)$/, clip.id);
      assert.ok(Number.isFinite(clip.duration) && clip.duration > 0, clip.id);
      assert.ok(clip.width > 0 && clip.height > 0, clip.id);
      assert.ok(['file', 'youtube'].includes(clip.video.provider), clip.video.provider);
      assert.ok(clip.video.src, clip.id);
    }
  }
});

test('a queued stream explains why it has no outputs', () => {
  const known = new Set(['not_ingested']);
  for (const vod of catalog.vods) {
    if (clips(vod).length === 0) {
      assert.ok(vod.pendingReason, `queued stream ${vod.id} needs a reason`);
      assert.ok(known.has(vod.pendingReason), `unknown reason ${vod.pendingReason}`);
    }
  }
});
