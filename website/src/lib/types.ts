export type Provider = 'file' | 'youtube';

export interface VideoSource {
  provider: Provider;
  /** Public URL for `file`, YouTube video id for `youtube`. */
  src: string;
}

export interface Clip {
  id: string;
  title: string;
  poster?: string;
  /** Rendered duration in seconds. */
  duration: number;
  width: number;
  height: number;
  video: VideoSource;
}

export interface Vod {
  id: string;
  title: string;
  /** ISO date (`YYYY-MM-DD`) from the Kick VOD. */
  date: string;
  /** ISO start time from Kick. */
  startTime: string;
  /** Source duration in seconds. */
  duration: number;
  kickUrl: string;
  thumbnail?: string;
  /** Why this stream has no outputs yet. Only shown when there are no clips. */
  pendingReason?: PendingReason;
  long?: Clip;
  shorts: Clip[];
}

/** Visitor-facing copy for a stream the pipeline has not cut yet. */
export const PENDING_REASONS = {
  not_ingested: 'Todavía sin editar: este stream no ha sido procesado.',
  not_uploaded: 'Editado, pendiente de publicar en YouTube.'
} as const;

export type PendingReason = keyof typeof PENDING_REASONS;

export interface CatalogFile {
  generatedAt: string;
  channel: { name: string; kickUrl: string };
  vods: Vod[];
}

/** Clip count decides whether a stream is published or still queued. */
export function clipCount(vod: Vod): number {
  return (vod.long ? 1 : 0) + vod.shorts.length;
}

/** Local files are previews; only YouTube uploads are public. */
export function visibleVod(vod: Vod, includeLocal: boolean): Vod {
  if (includeLocal) return vod;
  const visible = {
    ...vod,
    long: vod.long?.video.provider === 'youtube' ? vod.long : undefined,
    shorts: vod.shorts.filter((clip) => clip.video.provider === 'youtube')
  };
  if (clipCount(vod) > 0 && clipCount(visible) === 0) visible.pendingReason = 'not_uploaded';
  return visible;
}

export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  const pad = (value: number) => value.toString().padStart(2, '0');
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(rest)}` : `${minutes}:${pad(rest)}`;
}

export function formatDate(iso: string): string {
  const date = new Date(`${iso}T00:00:00Z`);
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleDateString('es-ES', { day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC' });
}

export function formatTimeRange(startTime: string, duration: number): string {
  const start = new Date(startTime);
  if (Number.isNaN(start.getTime())) return startTime;
  const format = new Intl.DateTimeFormat('es-ES', {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
    timeZone: 'Europe/Madrid'
  });
  return `${format.format(start)} - ${format.format(new Date(start.getTime() + duration * 1000))}`;
}
