import { visibleVod, type CatalogFile } from '$lib/types';
import { dev } from '$app/environment';
import { error } from '@sveltejs/kit';
import type { PageServerLoad } from './$types';

// The catalog is a build-time input, so the gallery ships as static HTML.
export const prerender = true;

export const load: PageServerLoad = async ({ fetch }) => {
  const response = await fetch('/catalog.json');
  if (!response.ok) error(response.status, 'Catalog unavailable');

  const catalog = (await response.json()) as CatalogFile;
  catalog.vods = catalog.vods.map((vod) => visibleVod(vod, dev));
  // Newest first; unedited VODs sink to the bottom instead of disappearing.
  const vods = [...catalog.vods].sort(
    (a, b) => Date.parse(b.date) - Date.parse(a.date) || Number(!!b.long) - Number(!!a.long)
  );

  return {
    catalog,
    vods: await Promise.all(vods.map(async (vod) => {
      const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(vod.kickUrl));
      const workspaceId = Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, '0')).join('').slice(0, 12);
      return { ...vod, workspaceId };
    }))
  };
};
