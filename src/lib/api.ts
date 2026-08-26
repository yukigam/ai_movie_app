import { supabase } from './supabase';
import { Series, Episode } from '@/types/series';

function mapEpisode(row: any): Episode {
  return {
    id: row.id,
    seriesId: row.series_id,
    episodeNumber: row.episode_number,
    title: row.title,
    description: row.description,
    videoUrl: row.video_url,
    thumbnailUrl: row.thumbnail_url,
    duration: row.duration,
    isFree: row.is_free,
    isLocked: !row.is_free,
  };
}

function mapSeries(row: any, episodes: Episode[]): Series {
  return {
    id: row.id,
    title: row.title,
    genre: row.genre,
    description: row.description,
    posterUrl: row.poster_url,
    bannerUrl: row.banner_url,
    playCount: row.play_count,
    episodeCount: row.episode_count,
    episodes,
  };
}

// Supabase caps every query server-side (default max_rows = 1000).  With
// many imported series a single fetch silently truncates and the newest
// series appear with missing episodes.  Page through instead.
async function fetchEpisodesForSeries(ids: string[]): Promise<any[]> {
  const all: any[] = [];
  const PAGE = 500;
  let from = 0;
  for (;;) {
    const { data, error } = await supabase
      .from('episodes')
      .select('*')
      .in('series_id', ids)
      .order('episode_number', { ascending: true })
      .range(from, from + PAGE - 1);
    if (error) break;
    const rows = data ?? [];
    all.push(...rows);
    if (rows.length < PAGE) break;
    from += PAGE;
  }
  return all;
}

export async function fetchSeries(): Promise<Series[]> {
  try {
    const { data: seriesRows, error: seriesErr } = await supabase
      .from('series')
      .select('*')
      .order('created_at', { ascending: false });

    if (seriesErr) return [];
    if (!seriesRows?.length) return [];

    const ids = seriesRows.map((s) => s.id).filter(Boolean);
    if (!ids.length) return [];

    const episodeRows = await fetchEpisodesForSeries(ids);

    const episodesBySeries: Record<string, Episode[]> = {};
    for (const ep of episodeRows ?? []) {
      if (!ep || !ep.series_id) continue;
      // Never render placeholder rows: pending episodes have no playable
      // video yet — showing them makes the series look incomplete.
      if (!ep.video_url || (ep.status && ep.status !== 'ok')) continue;
      if (!episodesBySeries[ep.series_id]) episodesBySeries[ep.series_id] = [];
      episodesBySeries[ep.series_id].push(mapEpisode(ep));
    }

    return seriesRows.map((s) => mapSeries(s, episodesBySeries[s.id] ?? []));
  } catch (e) {
    console.warn('fetchSeries failed:', e);
    return [];
  }
}

export async function fetchFeed(): Promise<Array<{ series: Series; episode: Episode }>> {
  const series = await fetchSeries();
  const result: Array<{ series: Series; episode: Episode }> = [];
  for (const s of series) {
    for (const ep of s.episodes) {
      result.push({ series: s, episode: ep });
    }
  }
  return result;
}

export async function fetchSeriesById(id: string): Promise<Series | null> {
  try {
    if (!id) return null;
    const { data: row, error } = await supabase
      .from('series')
      .select('*')
      .eq('id', id)
      .single();

    if (error || !row) return null;

    const { data: episodeRows } = await supabase
      .from('episodes')
      .select('*')
      .eq('series_id', id)
      .order('episode_number', { ascending: true });

    // Hide pending/placeholder episodes — only fully playable ones.
    const playable = (episodeRows ?? []).filter(
      (ep) => ep && ep.video_url && (!ep.status || ep.status === 'ok'),
    );
    return mapSeries(row, playable.map(mapEpisode));
  } catch (e) {
    console.warn('fetchSeriesById failed:', e);
    return null;
  }
}
