import { useEffect, useMemo, useState } from 'react';
import {
  View,
  Text,
  FlatList,
  Pressable,
  ScrollView,
  StyleSheet,
  Dimensions,
  ListRenderItemInfo,
  ActivityIndicator,
} from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { Series, Genre } from '@/types/series';
import { supabase } from '@/lib/supabase';

const { width: SCREEN_WIDTH } = Dimensions.get('window');
const CARD_WIDTH = (SCREEN_WIDTH - 48) / 2;
const BANNER_HEIGHT = 180;

function formatCount(count: number): string {
  if (count >= 1000000) return `${(count / 1000000).toFixed(1)}M`;
  if (count >= 1000) return `${(count / 1000).toFixed(1)}K`;
  return String(count);
}

const GENRES: Array<{ label: string; value: string }> = [
  { label: 'All', value: 'all' },
  { label: 'Sci-Fi', value: 'Sci-Fi' },
  { label: 'Fantasy', value: 'Fantasy' },
  { label: 'Romance', value: 'Romance' },
  { label: 'Horror', value: 'Horror' },
];

export default function ExploreScreen() {
  const insets = useSafeAreaInsets();
  const [seriesList, setSeriesList] = useState<Series[]>([]);
  const [selectedGenre, setSelectedGenre] = useState<string>('all');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const { data, error: err } = await supabase
          .from('series')
          .select('*')
          .order('created_at', { ascending: false });
        if (err) throw err;
        const rows = (data ?? []).map((row: any) => {
          const episodes: any[] = [];
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
          } as Series;
        });
        setSeriesList(rows);
      } catch (e) {
        setError("Failed to load series");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const filteredSeries = useMemo(() => {
    if (selectedGenre === 'all') return seriesList;
    return seriesList.filter((s) => s.genre === selectedGenre);
  }, [selectedGenre, seriesList]);

  if (loading) {
    return (
      <View style={[styles.container, styles.centered, { paddingTop: insets.top }]}>
        <ActivityIndicator color="#E50914" size="large" />
        <Text style={{ color: '#888', marginTop: 16 }}>Loading series...</Text>
      </View>
    );
  }

  if (error) {
    return (
      <View style={[styles.container, styles.centered, { paddingTop: insets.top }]}>
        <Text style={{ color: '#E50914', fontSize: 16, fontWeight: '700' }}>{error}</Text>
      </View>
    );
  }

  const renderSeriesCard = ({ item }: ListRenderItemInfo<Series>) => (
    <Pressable style={({ pressed }) => [styles.card, pressed && styles.cardPressed]}>
      <View style={styles.cardPoster}>
        <View style={styles.cardPosterPlaceholder}>
          <Text style={styles.posterEmoji}>
            {item.genre === 'Sci-Fi' ? '🚀' : item.genre === 'Fantasy' ? '✨' : item.genre === 'Romance' ? '💜' : '👻'}
          </Text>
        </View>
        <View style={styles.cardBadge}>
          <Text style={styles.cardBadgeText}>{item.episodeCount ?? 0} EP</Text>
        </View>
      </View>

      <View style={styles.cardInfo}>
        <Text style={styles.cardTitle} numberOfLines={1}>
          {item.title}
        </Text>
        <View style={styles.cardMeta}>
          <Text style={styles.cardGenre}>{item.genre}</Text>
          <Text style={styles.cardPlays}>▶ {formatCount(item.playCount)}</Text>
        </View>
      </View>
    </Pressable>
  );

  return (
    <View style={[styles.container, { paddingTop: insets.top }]}>
      <View style={styles.header}>
        <Text style={styles.headerTitle}>Explore</Text>
        <Text style={styles.headerSubtitle}>AI Mini-Series</Text>
      </View>

      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        style={styles.genreScroll}
        contentContainerStyle={styles.genreContainer}
      >
        {GENRES.map((genre) => (
          <Pressable
            key={genre.value}
            style={[
              styles.genreChip,
              selectedGenre === genre.value && styles.genreChipActive,
            ]}
            onPress={() => setSelectedGenre(genre.value)}
          >
            <Text
              style={[
                styles.genreChipText,
                selectedGenre === genre.value && styles.genreChipTextActive,
              ]}
            >
              {genre.label}
            </Text>
          </Pressable>
        ))}
      </ScrollView>

      <FlatList
        data={filteredSeries}
        renderItem={renderSeriesCard}
        keyExtractor={(item) => item.id}
        numColumns={2}
        columnWrapperStyle={styles.row}
        contentContainerStyle={styles.grid}
        showsVerticalScrollIndicator={false}
        ListEmptyComponent={
          <View style={styles.emptyContainer}>
            <Text style={styles.emptyText}>No series found</Text>
          </View>
        }
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#000',
  },
  centered: {
    justifyContent: 'center',
    alignItems: 'center',
  },
  header: {
    paddingHorizontal: 20,
    paddingTop: 8,
    paddingBottom: 4,
  },
  headerTitle: {
    color: '#fff',
    fontSize: 28,
    fontWeight: '800',
  },
  headerSubtitle: {
    color: '#E50914',
    fontSize: 14,
    fontWeight: '600',
    marginTop: 2,
  },
  genreScroll: {
    maxHeight: 44,
    marginTop: 12,
  },
  genreContainer: {
    paddingHorizontal: 20,
    gap: 10,
    alignItems: 'center',
  },
  genreChip: {
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 20,
    backgroundColor: '#1A1A1A',
  },
  genreChipActive: {
    backgroundColor: '#E50914',
  },
  genreChipText: {
    color: '#888',
    fontSize: 14,
    fontWeight: '600',
  },
  genreChipTextActive: {
    color: '#fff',
  },
  grid: {
    padding: 16,
    paddingBottom: 100,
  },
  row: {
    gap: 12,
    marginBottom: 12,
  },
  card: {
    width: CARD_WIDTH,
    borderRadius: 12,
    backgroundColor: '#111',
    overflow: 'hidden',
  },
  cardPressed: {
    opacity: 0.8,
  },
  cardPoster: {
    width: CARD_WIDTH,
    height: CARD_WIDTH * 1.4,
    backgroundColor: '#1A1A2E',
    justifyContent: 'center',
    alignItems: 'center',
    position: 'relative',
  },
  cardPosterPlaceholder: {
    justifyContent: 'center',
    alignItems: 'center',
  },
  posterEmoji: {
    fontSize: 48,
  },
  cardBadge: {
    position: 'absolute',
    top: 8,
    right: 8,
    backgroundColor: 'rgba(0,0,0,0.7)',
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 4,
  },
  cardBadgeText: {
    color: '#fff',
    fontSize: 10,
    fontWeight: '700',
  },
  cardInfo: {
    padding: 10,
    gap: 4,
  },
  cardTitle: {
    color: '#fff',
    fontSize: 14,
    fontWeight: '700',
  },
  cardMeta: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
  },
  cardGenre: {
    color: '#E50914',
    fontSize: 11,
    fontWeight: '600',
  },
  cardPlays: {
    color: '#666',
    fontSize: 11,
  },
  emptyContainer: {
    paddingTop: 60,
    alignItems: 'center',
  },
  emptyText: {
    color: '#666',
    fontSize: 16,
  },
});
