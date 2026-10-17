import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { visibleVod } from '../src/lib/types.ts';

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
  const known = new Set(['not_ingested', 'not_uploaded']);
  for (const vod of catalog.vods) {
    if (clips(vod).length === 0) {
      assert.ok(vod.pendingReason, `queued stream ${vod.id} needs a reason`);
      assert.ok(known.has(vod.pendingReason), `unknown reason ${vod.pendingReason}`);
    }
  }
});

test('production shows only YouTube uploads; development preserves local previews', () => {
  const before = structuredClone(catalog);
  const publicVods = catalog.vods.map((vod) => visibleVod(vod, false));
  const publicClips = publicVods.flatMap(clips);
  assert.deepEqual(publicClips, catalog.vods.flatMap(clips).filter((clip) => clip.video.provider === 'youtube'));
  for (const vod of catalog.vods) {
    assert.deepEqual(visibleVod(vod, true), vod);
    const publicVod = publicVods.find((item) => item.id === vod.id);
    if (clips(vod).length > 0 && clips(publicVod).length === 0) {
      assert.equal(publicVod.pendingReason, 'not_uploaded');
    }
  }
  assert.deepEqual(catalog, before);
});

test('production filters long videos and shorts independently', () => {
  const local = catalog.vods.flatMap(clips).find((clip) => clip.video.provider === 'file');
  const youtube = catalog.vods.flatMap(clips).find((clip) => clip.video.provider === 'youtube');
  const vod = { ...catalog.vods[0], long: local, shorts: [local, youtube] };
  const visible = visibleVod(vod, false);
  assert.equal(visible.long, undefined);
  assert.deepEqual(visible.shorts, [youtube]);
});
