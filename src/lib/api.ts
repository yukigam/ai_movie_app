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

    const { data: episodeRows, error: epErr } = await supabase
      .from('episodes')
      .select('*')
      .in('series_id', ids)
      .order('episode_number', { ascending: true });

    if (epErr) return seriesRows.map((s) => mapSeries(s, []));

    const episodesBySeries: Record<string, Episode[]> = {};
    for (const ep of episodeRows ?? []) {
      if (!ep || !ep.series_id) continue;
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

    return mapSeries(row, (episodeRows ?? []).map(mapEpisode));
  } catch (e) {
    console.warn('fetchSeriesById failed:', e);
    return null;
  }
}
