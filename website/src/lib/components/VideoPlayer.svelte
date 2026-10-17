<script lang="ts">
  import { flushSync } from 'svelte';
  import type { VideoSource } from '$lib/types';

  let {
    video,
    title,
    poster,
    vertical = false
  }: { video: VideoSource; title: string; poster?: string; vertical?: boolean } = $props();

  let playing = $state(false);
  let player = $state<HTMLVideoElement>();
  const thumbnail = $derived(video.provider === 'youtube' ? `https://i.ytimg.com/vi/${video.src}/maxresdefault.jpg` : poster);

  function fallbackThumbnail(event: Event) {
    const image = event.currentTarget as HTMLImageElement;
    if (video.provider === 'youtube' && image.src.endsWith('/maxresdefault.jpg') &&
        (event.type === 'error' || image.naturalWidth <= 120)) {
      image.src = `https://i.ytimg.com/vi/${video.src}/hqdefault.jpg`;
    }
  }

  function play() {
    flushSync(() => (playing = true));
    if (video.provider === 'file') player?.play().catch(() => {});
  }
</script>

{#if playing}
  {#if video.provider === 'youtube'}
    <iframe
      class="player"
      class:vertical
      src="https://www.youtube-nocookie.com/embed/{video.src}?autoplay=1&rel=0"
      {title}
      allow="accelerometer; autoplay; clipboard-write; encrypted-media; picture-in-picture"
      allowfullscreen
    ></iframe>
  {:else}
    <!-- svelte-ignore a11y_media_has_caption -->
    <video bind:this={player} class="player" class:vertical {poster} controls playsinline preload="metadata" {title}>
      <source src={video.src} type="video/mp4" />
    </video>
  {/if}
{:else}
  <button
    class="poster"
    class:vertical
    type="button"
    onclick={play}
    aria-label={`Reproducir ${title}`}
  >
    {#if thumbnail}
      <img src={thumbnail} alt="" loading="lazy" onload={fallbackThumbnail} onerror={fallbackThumbnail} />
    {:else}
      <span class="filler" aria-hidden="true"></span>
    {/if}
    <span class="play" aria-hidden="true">
      <svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true">
        <path d="M8 5v14l11-7z" fill="currentColor" />
      </svg>
    </span>
  </button>
{/if}

<style>
  .player,
  .poster {
    display: block;
    width: 100%;
    border: 0;
    padding: 0;
    background: #000;
  }

  .player {
    aspect-ratio: 16 / 9;
  }

  .player.vertical {
    aspect-ratio: 9 / 16;
  }

  video.player {
    object-fit: cover;
  }

  .poster {
    position: relative;
    aspect-ratio: 16 / 9;
    cursor: pointer;
    overflow: hidden;
  }

  .poster.vertical {
    aspect-ratio: 9 / 16;
  }

  .poster img,
  .filler {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .poster img {
    filter: brightness(0.62) saturate(0.9);
    transition: filter 0.5s ease, transform 0.7s ease;
  }

  .filler {
    background:
      repeating-linear-gradient(115deg, #14171a 0 14px, #0e1013 14px 28px),
      radial-gradient(circle at 70% 25%, rgb(255 59 47 / 0.16), transparent 60%);
  }

  .poster:hover img,
  .poster:hover .filler {
    filter: brightness(0.8) saturate(1);
    transform: scale(1.03);
  }

  .play {
    position: absolute;
    inset: 0;
    margin: auto;
    width: clamp(3rem, 7vw, 4.5rem);
    height: clamp(3rem, 7vw, 4.5rem);
    display: grid;
    place-items: center;
    border-radius: 50%;
    color: var(--bg);
    background: var(--fg);
    box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.4), 0 18px 50px rgba(0, 0, 0, 0.55);
    transition: transform 0.35s ease, background 0.35s ease;
  }

  .play svg {
    margin-left: 0.15em;
  }

  .poster:hover .play {
    transform: scale(1.09);
    background: var(--hot);
  }
</style>
