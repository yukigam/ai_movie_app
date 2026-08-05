export interface Episode {
  id: string;
  seriesId: string;
  episodeNumber: number;
  title: string;
  description: string;
  videoUrl: string;
  thumbnailUrl: string;
  duration: number;
  isFree: boolean;
  isLocked: boolean;
}

export interface Series {
  id: string;
  title: string;
  genre: Genre;
  description: string;
  posterUrl: string;
  bannerUrl: string;
  episodes: Episode[];
  playCount: number;
  episodeCount: number;
}

export type Genre = 'Sci-Fi' | 'Fantasy' | 'Romance' | 'Horror';

export interface UnlockedEpisodes {
  [episodeId: string]: boolean;
}
