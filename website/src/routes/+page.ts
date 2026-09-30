import type { CatalogFile } from '$lib/types';
import { error } from '@sveltejs/kit';
import type { PageLoad } from './$types';

// The catalog is a build-time input, so the gallery ships as static HTML.
export const prerender = true;

export const load: PageLoad = async ({ fetch }) => {
  const response = await fetch('/catalog.json');
  if (!response.ok) error(response.status, 'Catalog unavailable');

  const catalog = (await response.json()) as CatalogFile;
  // Newest first; unedited VODs sink to the bottom instead of disappearing.
  const vods = [...catalog.vods].sort(
    (a, b) => Date.parse(b.date) - Date.parse(a.date) || Number(!!b.long) - Number(!!a.long)
  );

  return { catalog, vods };
};
