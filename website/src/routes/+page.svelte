<script lang="ts">
  import { clipCount, type Vod } from "$lib/types";
  import StreamRow from "$lib/components/StreamRow.svelte";
  import type { PageProps } from "./$types";

  let { data }: PageProps = $props();

  const longs = $derived(data.vods.filter((vod: Vod) => vod.long).length);
  const processed = $derived(
    data.vods.filter((vod: Vod) => clipCount(vod) > 0).length,
  );
  const shorts = $derived(
    data.vods.reduce((sum: number, vod: Vod) => sum + vod.shorts.length, 0),
  );
</script>

<svelte:head>
  <title>milloclips - Clips de Elmillor editados con IA</title>
  <meta
    name="description"
    content="Clips de LoL de Elmillor seleccionados y editados 100% con IA."
  />
</svelte:head>

<div class="deck">
  <header class="bar">
    <a class="brand" href="/">milloclips</a>
    <p>
      Clips de LoL de
      <a
        href={data.catalog.channel.kickUrl}
        target="_blank"
        rel="noreferrer noopener">{data.catalog.channel.name}</a
      > seleccionados y editados 100% con IA.
    </p>
    <p class="tally">
      {processed}
      {processed === 1 ? "stream procesado" : "streams procesados"}: {longs}
      {longs === 1 ? "vídeo" : "vídeos"} y {shorts}
      {shorts === 1 ? "short editado" : "shorts editados"}
    </p>
  </header>

  <main>
    <ol class="streams">
      {#each data.vods as vod, i (vod.id)}
        <StreamRow {vod} index={i} />
      {/each}
    </ol>

    <footer>
      <p>
        El material original pertenece a {data.catalog.channel.name}.
      </p>
      <p class="footer-range">
        Este catálogo incluye streams del 17/09/2026 al 01/10/2026.
      </p>
    </footer>
  </main>
</div>

<style>
  .deck {
    --bg: #0a0b0d;
    --fg: #f2f4f7;
    --muted: #79818d;
    --meta: #c2cad6;
    --hot: #eaff20;
    --line: #1e242c;
    --edge: clamp(1rem, 4vw, 4rem);
    min-height: 100vh;
    background: var(--bg);
    color: var(--fg);
  }

  .bar {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: clamp(1rem, 4vw, 3rem);
    padding: 1.1rem var(--edge);
    border-bottom: 1px solid var(--line);
  }

  .brand {
    font-family: var(--display);
    font-size: clamp(1.4rem, 1rem + 1.4vw, 2rem);
    line-height: 1;
    letter-spacing: 0.005em;
    text-transform: uppercase;
    text-decoration: none;
  }

  .bar p {
    margin: 0;
    max-width: 52ch;
    color: var(--muted);
    font-size: 0.95rem;
  }

  .bar a:not(.brand) {
    color: var(--fg);
  }

  .tally {
    text-align: right;
    white-space: nowrap;
  }

  main {
    padding: 0 var(--edge) 5rem;
  }

  .streams {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .streams > :global(li:last-child) {
    border-bottom: 1px solid var(--line);
  }

  footer {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: clamp(1rem, 4vw, 3rem);
    padding: 2.5rem 0 0;
  }

  footer p {
    margin: 0;
    max-width: 52ch;
    color: var(--muted);
    font-size: 0.95rem;
  }

  .footer-range {
    text-align: right;
    white-space: nowrap;
  }

  @media (max-width: 60rem) {
    .bar {
      grid-template-columns: 1fr;
      gap: 0.5rem;
    }

    .tally {
      text-align: left;
      white-space: normal;
    }

    footer {
      grid-template-columns: 1fr;
      gap: 0.5rem;
    }

    .footer-range {
      text-align: left;
      white-space: normal;
    }
  }
</style>
