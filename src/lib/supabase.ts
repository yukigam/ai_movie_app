import { createClient, SupabaseClient } from '@supabase/supabase-js';

const supabaseUrl = process.env.EXPO_PUBLIC_SUPABASE_URL ?? '';
const supabaseAnonKey = process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY ?? '';

function buildClient(throwOnError = false): SupabaseClient {
  try {
    return createClient(supabaseUrl || 'https://placeholder.supabase.co', supabaseAnonKey || 'placeholder');
  } catch (e) {
    console.warn('Supabase client init failed:', e);
    if (throwOnError) throw e;
    return createClient('https://placeholder.supabase.co', 'placeholder');
  }
}

export const supabase: SupabaseClient = buildClient();

export type Tables = {
  series: {
    Row: {
      id: string;
      title: string;
      genre: string;
      description: string;
      poster_url: string;
      banner_url: string;
      play_count: number;
      episode_count: number;
      created_at: string;
    };
  };
  episodes: {
    Row: {
      id: string;
      series_id: string;
      episode_number: number;
      title: string;
      description: string;
      video_url: string;
      thumbnail_url: string;
      duration: number;
      is_free: boolean;
      created_at: string;
    };
  };
};

