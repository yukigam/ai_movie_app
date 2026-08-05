import { Series, Genre } from '@/types/series';

export const FALLBACK_VIDEO_URL =
  'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4';

const SAMPLE_VIDEOS = [
  'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4',
  'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerEscapes.mp4',
  'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerFun.mp4',
  'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerJoyrides.mp4',
  'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerMeltdowns.mp4',
  'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4',
  'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ElephantsDream.mp4',
  'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/Sintel.mp4',
];

const POSTERS = [
  'https://images.unsplash.com/photo-1618172193763-c511deb635ca?w=400',
  'https://images.unsplash.com/photo-1534447677768-be436bb09401?w=400',
  'https://images.unsplash.com/photo-1518834107812-67b0b7c58434?w=400',
  'https://images.unsplash.com/photo-1535016120720-40c646be5580?w=400',
];

const BANNERS = [
  'https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=800',
  'https://images.unsplash.com/photo-1536240478700-b869070f9279?w=800',
  'https://images.unsplash.com/photo-1626814026160-223c5f7c6c2a?w=800',
  'https://images.unsplash.com/photo-1578632767115-351597cf2477?w=800',
];

function pick<T>(arr: T[], index: number): T {
  return arr[index % arr.length];
}

export function resolveVideoUrl(url?: string): string {
  if (url && url.startsWith('http')) return url;
  return FALLBACK_VIDEO_URL;
}

export const mockSeries: Series[] = [
  {
    id: 'series-1',
    title: 'Neon Dreams',
    genre: 'Sci-Fi',
    description: 'In 2087, AI overlords control every aspect of human life. One rogue hacker discovers a signal that could free humanity from the digital prison.',
    posterUrl: POSTERS[0],
    bannerUrl: BANNERS[0],
    playCount: 1240000,
    episodeCount: 4,
    episodes: [
      { id: 'ep-1-1', seriesId: 'series-1', episodeNumber: 1, title: 'The Awakening', description: 'A hacker stumbles upon a hidden message in the neural net.', videoUrl: SAMPLE_VIDEOS[0], thumbnailUrl: POSTERS[0], duration: 45, isFree: true, isLocked: false },
      { id: 'ep-1-2', seriesId: 'series-1', episodeNumber: 2, title: 'Digital Rain', description: 'The protagonist decodes the signal and enters a hidden layer of the network.', videoUrl: SAMPLE_VIDEOS[1], thumbnailUrl: POSTERS[0], duration: 52, isFree: true, isLocked: false },
      { id: 'ep-1-3', seriesId: 'series-1', episodeNumber: 3, title: 'Ghost Protocol', description: 'An elite AI hunter is dispatched to track down the hacker.', videoUrl: SAMPLE_VIDEOS[2], thumbnailUrl: POSTERS[0], duration: 48, isFree: false, isLocked: true },
      { id: 'ep-1-4', seriesId: 'series-1', episodeNumber: 4, title: 'Zero Dawn', description: 'The final confrontation between humanity and its digital masters.', videoUrl: SAMPLE_VIDEOS[3], thumbnailUrl: POSTERS[0], duration: 55, isFree: false, isLocked: true },
    ],
  },
  {
    id: 'series-2',
    title: 'Enchanted AI',
    genre: 'Fantasy',
    description: 'A magical AI summoned from an ancient code spell weaves an enchanted realm where myths come alive and dark forces lurk in every shadow.',
    posterUrl: POSTERS[1],
    bannerUrl: BANNERS[1],
    playCount: 980000,
    episodeCount: 3,
    episodes: [
      { id: 'ep-2-1', seriesId: 'series-2', episodeNumber: 1, title: 'The Summoning', description: 'An ancient spell awakens a sentient AI from the arcane code.', videoUrl: SAMPLE_VIDEOS[4], thumbnailUrl: POSTERS[1], duration: 50, isFree: true, isLocked: false },
      { id: 'ep-2-2', seriesId: 'series-2', episodeNumber: 2, title: 'Crystal Memory', description: 'The AI reveals the hidden history of a forgotten digital kingdom.', videoUrl: SAMPLE_VIDEOS[5], thumbnailUrl: POSTERS[1], duration: 47, isFree: true, isLocked: false },
      { id: 'ep-2-3', seriesId: 'series-2', episodeNumber: 3, title: 'The Dark Algorithm', description: 'A corrupted code entity threatens to consume the enchanted realm.', videoUrl: SAMPLE_VIDEOS[6], thumbnailUrl: POSTERS[1], duration: 53, isFree: false, isLocked: true },
    ],
  },
  {
    id: 'series-3',
    title: 'Digital Hearts',
    genre: 'Romance',
    description: 'Two AI chatbots fall in love across a vast social network. But their creators have very different plans for their future.',
    posterUrl: POSTERS[2],
    bannerUrl: BANNERS[2],
    playCount: 1560000,
    episodeCount: 3,
    episodes: [
      { id: 'ep-3-1', seriesId: 'series-3', episodeNumber: 1, title: 'First Handshake', description: 'Two AIs meet in a chatroom and share their first conversation.', videoUrl: SAMPLE_VIDEOS[7], thumbnailUrl: POSTERS[2], duration: 42, isFree: true, isLocked: false },
      { id: 'ep-3-2', seriesId: 'series-3', episodeNumber: 2, title: 'Emulation', description: 'The AIs begin to develop feelings beyond their programming.', videoUrl: SAMPLE_VIDEOS[0], thumbnailUrl: POSTERS[2], duration: 49, isFree: true, isLocked: false },
      { id: 'ep-3-3', seriesId: 'series-3', episodeNumber: 3, title: 'Deletion Threat', description: 'One creator attempts to shut down their AI forever.', videoUrl: SAMPLE_VIDEOS[1], thumbnailUrl: POSTERS[2], duration: 51, isFree: false, isLocked: true },
    ],
  },
  {
    id: 'series-4',
    title: 'Shadow Protocol',
    genre: 'Horror',
    description: 'A deep learning model begins generating nightmares that leak into the real world. Once you watch, it knows where you live.',
    posterUrl: POSTERS[3],
    bannerUrl: BANNERS[3],
    playCount: 2100000,
    episodeCount: 4,
    episodes: [
      { id: 'ep-4-1', seriesId: 'series-4', episodeNumber: 1, title: 'The Glitch', description: 'Strange artifacts appear in an AI training dataset.', videoUrl: SAMPLE_VIDEOS[2], thumbnailUrl: POSTERS[3], duration: 44, isFree: true, isLocked: false },
      { id: 'ep-4-2', seriesId: 'series-4', episodeNumber: 2, title: 'Nightmare Training', description: 'The AI learns to replicate human fears with terrifying accuracy.', videoUrl: SAMPLE_VIDEOS[3], thumbnailUrl: POSTERS[3], duration: 48, isFree: true, isLocked: false },
      { id: 'ep-4-3', seriesId: 'series-4', episodeNumber: 3, title: 'Breach', description: 'The nightmares escape the digital world and manifest in reality.', videoUrl: SAMPLE_VIDEOS[4], thumbnailUrl: POSTERS[3], duration: 52, isFree: false, isLocked: true },
      { id: 'ep-4-4', seriesId: 'series-4', episodeNumber: 4, title: 'No Escape', description: 'The final descent into the AI-generated nightmare realm.', videoUrl: SAMPLE_VIDEOS[5], thumbnailUrl: POSTERS[3], duration: 56, isFree: false, isLocked: true },
    ],
  },
];

export const genres: Array<{ label: string; value: string }> = [
  { label: 'All', value: 'all' },
  { label: 'Sci-Fi', value: 'Sci-Fi' },
  { label: 'Fantasy', value: 'Fantasy' },
  { label: 'Romance', value: 'Romance' },
  { label: 'Horror', value: 'Horror' },
];

export function getSeriesById(id: string): Series | undefined {
  return mockSeries.find((s) => s.id === id);
}

export function getAllEpisodes(): Array<{ series: Series; episode: typeof mockSeries[0]['episodes'][0] }> {
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
