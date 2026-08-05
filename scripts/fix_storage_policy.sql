-- ============================================================
-- Fix Supabase Storage — ensure `videos` bucket exists, is
-- public, and has RLS policies allowing public access.
-- ============================================================
-- Run this in the Supabase SQL Editor (Dashboard > SQL Editor).
-- ============================================================

-- 1. Ensure bucket exists and is public
------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('videos', 'videos', true)
on conflict (id) do update set public = true;

-- 2. Drop existing policies on storage.objects for videos
------------------------------------------------------------
drop policy if exists "Public Access Videos — SELECT" on storage.objects;
drop policy if exists "Public Access Videos — INSERT" on storage.objects;
drop policy if exists "Public Access Videos — UPDATE" on storage.objects;
drop policy if exists "Public Access Videos — DELETE" on storage.objects;
drop policy if exists "Public Access Videos" on storage.objects;

-- 3. Allow public SELECT (read) on any file in the videos bucket
------------------------------------------------------------
create policy "Public Access Videos — SELECT"
  on storage.objects for select
  using (bucket_id = 'videos');

-- 4. Allow INSERT for authenticated/anonymous users (service_role bypasses RLS,
--    but anon key uploads need this policy)
------------------------------------------------------------
create policy "Public Access Videos — INSERT"
  on storage.objects for insert
  with check (bucket_id = 'videos');

-- 5. Allow UPDATE (used by upsert)
------------------------------------------------------------
create policy "Public Access Videos — UPDATE"
  on storage.objects for update
  using (bucket_id = 'videos')
  with check (bucket_id = 'videos');

-- 6. Allow DELETE
------------------------------------------------------------
create policy "Public Access Videos — DELETE"
  on storage.objects for delete
  using (bucket_id = 'videos');
