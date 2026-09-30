<script lang="ts">
  import {
    PENDING_REASONS,
    clipCount,
    formatDate,
    formatDuration,
    formatTimeRange,
    type Clip,
    type Vod
  } from '$lib/types';
  import VideoPlayer from './VideoPlayer.svelte';

  let { vod, index }: { vod: Vod; index: number } = $props();

  const published = $derived(clipCount(vod) > 0);
  const outputs = $derived([...(vod.long ? [vod.long] : []), ...vod.shorts]);
</script>

<li class="stream" class:published>
  <div class="head">
    <span class="slot mono">{String(index + 1).padStart(2, '0')}</span>

    <div class="what">
      <h2>{vod.title}</h2>
      <p class="links">
        <a href={vod.kickUrl} target="_blank" rel="noreferrer noopener">Ver en Kick &#8599;</a>
        <span class="state mono">
          <span class="dot" aria-hidden="true"></span>{published
            ? `${clipCount(vod)} ${clipCount(vod) === 1 ? 'vídeo' : 'vídeos'}`
            : 'en cola'}
        </span>
      </p>
      {#if !published}
        <p class="note">
          {vod.pendingReason
            ? PENDING_REASONS[vod.pendingReason]
            : 'Todavía no hay ningún vídeo de este stream.'}
        </p>
      {/if}
    </div>

    <p class="when mono">{formatDate(vod.date)}<br />{formatTimeRange(vod.startTime, vod.duration)}</p>
  </div>

  {#if published}
    <!-- Scrollable, so it must stay reachable by keyboard. -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div class="lane" tabindex="0" role="region" aria-label="Vídeos de {vod.title}">
      {#each outputs as clip (clip.id)}
        <figure class="clip" class:short={clip.height > clip.width}>
          <div class="screen">
            <VideoPlayer video={clip.video} title={clip.title} poster={clip.poster ?? vod.thumbnail} vertical={clip.height > clip.width} />
          </div>
          <figcaption>
            <h3>{clip.title}</h3>
            <p class="spec mono">
              {formatDuration(clip.duration)} · {clip.width}×{clip.height}
            </p>
          </figcaption>
        </figure>
      {/each}
      <p class="spacer" aria-hidden="true"></p>
    </div>
    <p class="hint mono">desliza la fila para ver todos los vídeos &rarr;</p>
  {/if}
</li>

<style>
  .stream {
    padding: clamp(1.3rem, 3vw, 2.2rem) 0;
    border-top: 1px solid var(--line);
  }

  .head {
    display: grid;
    grid-template-columns: 2.6rem minmax(0, 1fr) auto;
    gap: clamp(0.8rem, 2.5vw, 2rem);
    align-items: start;
  }

  .slot {
    transform: translateY(-0.1rem);
  }

  .what h2 {
    margin: 0;
    font-family: var(--display);
    font-weight: 400;
    font-size: clamp(1.4rem, 0.9rem + 2.1vw, 3rem);
    line-height: 0.98;
    text-wrap: balance;
  }

  .links {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem 1.1rem;
    margin: 0.7rem 0 0;
  }

  .links a {
    font-family: var(--mono);
    font-size: 0.72rem;
    letter-spacing: 0;
    text-transform: uppercase;
    text-decoration: none;
    color: var(--fg);
    border-bottom: 1px solid currentColor;
    padding-bottom: 1px;
  }

  .links a:hover {
    color: #53fc18;
  }

  .state {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }

  .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--muted);
  }

  .published .state {
    color: var(--fg);
  }

  .published .dot {
    background: #53fc18;
  }

  .note {
    margin: 0.6rem 0 0;
    max-width: 52ch;
    color: var(--muted);
    font-size: 0.95rem;
  }

  .when.mono {
    text-align: right;
    line-height: 1.7;
    white-space: nowrap;
    color: var(--meta);
    transform: translateY(-0.1rem);
  }

  /* The output lane never shrinks its videos; it scrolls instead. */
  .lane {
    display: flex;
    gap: clamp(0.9rem, 2vw, 1.6rem);
    margin-top: clamp(1.2rem, 3vw, 2rem);
    padding: 2px var(--edge) 1rem 0;
    overflow-x: auto;
    overscroll-behavior-x: contain;
    scroll-snap-type: x proximity;
  }

  .clip {
    flex: 0 0 auto;
    width: clamp(20rem, 46vw, 44rem);
    margin: 0;
    scroll-snap-align: start;
  }

  .clip.short {
    width: clamp(12.5rem, 24vw, 17rem);
  }

  .screen {
    border: 1px solid var(--line);
    background: #000;
  }

  figcaption {
    display: grid;
    gap: 0.25rem;
    margin-top: 0.7rem;
  }

  h3 {
    margin: 0;
    font-size: clamp(1rem, 0.95rem + 0.3vw, 1.2rem);
    font-weight: 500;
    line-height: 1.25;
  }

  .spec.mono {
    margin: 0;
    color: var(--meta);
    font-size: 0.78rem;
  }

  .spacer {
    flex: 0 0 1px;
    margin: 0;
  }

  .hint {
    display: none;
    margin: 0;
  }

  .stream:not(.published) .what h2,
  .stream:not(.published) .when {
    color: var(--muted);
  }

  .stream:not(.published) {
    opacity: 0.55;
  }

  .stream:not(.published):hover {
    opacity: 0.8;
  }

  @media (max-width: 64rem) {
    .hint {
      display: block;
    }
  }

  @media (max-width: 40rem) {
    .head {
      grid-template-columns: 2rem minmax(0, 1fr);
    }

    .when {
      grid-column: 2;
      text-align: left;
    }

    .when br {
      display: none;
    }

    .when {
      white-space: normal;
    }
  }
</style>
