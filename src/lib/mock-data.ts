import { Series } from '@/types/series';

export const FALLBACK_VIDEO_URL =
  'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4';

export function resolveVideoUrl(url?: string): string {
  if (url && url.startsWith('http')) return url;
  return FALLBACK_VIDEO_URL;
}

// Real series/episodes come from Supabase (see src/lib/api.ts).
// Kept empty on purpose — no test series should ever show up in the app.
export const mockSeries: Series[] = [];

export function getSeriesById(id: string): Series | undefined {
  return mockSeries.find((s) => s.id === id);
}

export function getAllEpisodes(): Array<{ series: Series; episode: Series['episodes'][0] }> {
  const result: Array<{ series: Series; episode: Series['episodes'][0] }> = [];
  for (const series of mockSeries) {
    for (const episode of series.episodes) {
      if (!episode.isLocked) {
        result.push({ series, episode });
      }
    }
  }
  return result;
}
