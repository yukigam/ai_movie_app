-- ============================================================
-- AI Mini-Series & Short Drama Player — Supabase Schema
-- ============================================================
-- Run this entire script in the Supabase SQL Editor.
-- It creates tables, enables RLS, inserts seed data,
-- and grants public read access.
-- ============================================================

-- 1. SERIES TABLE
------------------------------------------------------------
create table if not exists public.series (
  id          text primary key,
  title       text not null,
  genre       text not null check (genre in ('Sci-Fi', 'Fantasy', 'Romance', 'Horror')),
  description text not null,
  poster_url  text not null,
  banner_url  text not null,
  play_count  bigint not null default 0,
  episode_count int not null default 0,
  created_at  timestamptz not null default now()
);

-- 2. EPISODES TABLE
------------------------------------------------------------
create table if not exists public.episodes (
  id              text primary key,
  series_id       text not null references public.series(id) on delete cascade,
  episode_number  int not null,
  title           text not null,
  description     text not null,
  video_url       text not null,
  thumbnail_url   text not null,
  duration        int not null,          -- minutes
  is_free         boolean not null default false,
  status          text not null default 'ok',      -- 'ok' | 'pending' (incomplete/retrying)
  source_url      text not null default '',        -- original TikTok page URL for re-downloads
  created_at      timestamptz not null default now()
);

-- 2b. PENDING TRACKING — ALTER statements for an EXISTING episodes table.
--     Idempotent: safe to run on every deployment.
------------------------------------------------------------
alter table public.episodes add column if not exists status     text not null default 'ok';
alter table public.episodes add column if not exists source_url text not null default '';

-- 3. INDEXES
------------------------------------------------------------
create index if not exists idx_episodes_series_id on public.episodes(series_id);
create index if not exists idx_episodes_number  on public.episodes(series_id, episode_number);

-- 4. ROW LEVEL SECURITY
------------------------------------------------------------
alter table public.series   enable row level security;
alter table public.episodes enable row level security;

-- Public read-only access for anonymous users
drop policy if exists "Public read access — series"   on public.series;
drop policy if exists "Public read access — episodes" on public.episodes;

create policy "Public read access — series"   on public.series   for select using (true);
create policy "Public read access — episodes" on public.episodes for select using (true);

-- 5. SEED DATA — SERIES
------------------------------------------------------------
insert into public.series (id, title, genre, description, poster_url, banner_url, play_count, episode_count) values
  (
    'series-1',
    'Neon Dreams',
    'Sci-Fi',
    'In 2087, AI overlords control every aspect of human life. One rogue hacker discovers a signal that could free humanity from the digital prison.',
    'https://images.unsplash.com/photo-1618172193763-c511deb635ca?w=400',
    'https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=800',
    1240000,
    4
  ),
  (
    'series-2',
    'Enchanted AI',
    'Fantasy',
    'A magical AI summoned from an ancient code spell weaves an enchanted realm where myths come alive and dark forces lurk in every shadow.',
    'https://images.unsplash.com/photo-1534447677768-be436bb09401?w=400',
    'https://images.unsplash.com/photo-1536240478700-b869070f9279?w=800',
    980000,
    3
  ),
  (
    'series-3',
    'Digital Hearts',
    'Romance',
    'Two AI chatbots fall in love across a vast social network. But their creators have very different plans for their future.',
    'https://images.unsplash.com/photo-1518834107812-67b0b7c58434?w=400',
    'https://images.unsplash.com/photo-1626814026160-223c5f7c6c2a?w=800',
    1560000,
    3
  ),
  (
    'series-4',
    'Shadow Protocol',
    'Horror',
    'A deep learning model begins generating nightmares that leak into the real world. Once you watch, it knows where you live.',
    'https://images.unsplash.com/photo-1535016120720-40c646be5580?w=400',
    'https://images.unsplash.com/photo-1578632767115-351597cf2477?w=800',
    2100000,
    4
  )
on conflict (id) do nothing;

-- 6. SEED DATA — EPISODES
------------------------------------------------------------
insert into public.episodes (id, series_id, episode_number, title, description, video_url, thumbnail_url, duration, is_free) values
  -- Neon Dreams — 4 episodes (1-2 free, 3-4 locked)
  (
    'ep-1-1', 'series-1', 1,
    'The Awakening',
    'A hacker stumbles upon a hidden message in the neural net.',
    'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4',
    'https://images.unsplash.com/photo-1618172193763-c511deb635ca?w=400',
    45, true
  ),
  (
    'ep-1-2', 'series-1', 2,
    'Digital Rain',
    'The protagonist decodes the signal and enters a hidden layer of the network.',
    'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerEscapes.mp4',
    'https://images.unsplash.com/photo-1618172193763-c511deb635ca?w=400',
    52, true
  ),
  (
    'ep-1-3', 'series-1', 3,
    'Ghost Protocol',
    'An elite AI hunter is dispatched to track down the hacker.',
    'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerFun.mp4',
    'https://images.unsplash.com/photo-1618172193763-c511deb635ca?w=400',
    48, false
  ),
  (
    'ep-1-4', 'series-1', 4,
    'Zero Dawn',
    'The final confrontation between humanity and its digital masters.',
    'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerJoyrides.mp4',
    'https://images.unsplash.com/photo-1618172193763-c511deb635ca?w=400',
    55, false
  ),

  -- Enchanted AI — 3 episodes (1-2 free, 3 locked)
  (
    'ep-2-1', 'series-2', 1,
    'The Summoning',
    'An ancient spell awakens a sentient AI from the arcane code.',
    'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerMeltdowns.mp4',
    'https://images.unsplash.com/photo-1534447677768-be436bb09401?w=400',
    50, true
  ),
  (
    'ep-2-2', 'series-2', 2,
    'Crystal Memory',
    'The AI reveals the hidden history of a forgotten digital kingdom.',
    'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4',
    'https://images.unsplash.com/photo-1534447677768-be436bb09401?w=400',
    47, true
  ),
  (
    'ep-2-3', 'series-2', 3,
    'The Dark Algorithm',
    'A corrupted code entity threatens to consume the enchanted realm.',
    'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ElephantsDream.mp4',
    'https://images.unsplash.com/photo-1534447677768-be436bb09401?w=400',
    53, false
  ),

  -- Digital Hearts — 3 episodes (1-2 free, 3 locked)
  (
    'ep-3-1', 'series-3', 1,
    'First Handshake',
    'Two AIs meet in a chatroom and share their first conversation.',
    'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/Sintel.mp4',
    'https://images.unsplash.com/photo-1518834107812-67b0b7c58434?w=400',
    42, true
  ),
  (
    'ep-3-2', 'series-3', 2,
    'Emulation',
    'The AIs begin to develop feelings beyond their programming.',
    'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4',
    'https://images.unsplash.com/photo-1518834107812-67b0b7c58434?w=400',
    49, true
  ),
  (
    'ep-3-3', 'series-3', 3,
    'Deletion Threat',
    'One creator attempts to shut down their AI forever.',
    'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerEscapes.mp4',
    'https://images.unsplash.com/photo-1518834107812-67b0b7c58434?w=400',
    51, false
  ),

  -- Shadow Protocol — 4 episodes (1-2 free, 3-4 locked)
  (
    'ep-4-1', 'series-4', 1,
    'The Glitch',
    'Strange artifacts appear in an AI training dataset.',
    'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerFun.mp4',
    'https://images.unsplash.com/photo-1535016120720-40c646be5580?w=400',
    44, true
  ),
  (
    'ep-4-2', 'series-4', 2,
    'Nightmare Training',
    'The AI learns to replicate human fears with terrifying accuracy.',
    'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerJoyrides.mp4',
    'https://images.unsplash.com/photo-1535016120720-40c646be5580?w=400',
    48, true
  ),
  (
    'ep-4-3', 'series-4', 3,
    'Breach',
    'The nightmares escape the digital world and manifest in reality.',
    'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerMeltdowns.mp4',
    'https://images.unsplash.com/photo-1535016120720-40c646be5580?w=400',
    52, false
  ),
  (
    'ep-4-4', 'series-4', 4,
    'No Escape',
    'The final descent into the AI-generated nightmare realm.',
    'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4',
    'https://images.unsplash.com/photo-1535016120720-40c646be5580?w=400',
    56, false
  )
on conflict (id) do nothing;
