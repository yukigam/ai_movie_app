import { useEffect, useMemo, useRef, useState } from 'react';
import {
  View,
  Text,
  FlatList,
  Pressable,
  TextInput,
  StyleSheet,
  Dimensions,
  ListRenderItemInfo,
  ActivityIndicator,
} from 'react-native';
import { Image } from 'expo-image';
import { useRouter } from 'expo-router';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { Series } from '@/types/series';
import { supabase } from '@/lib/supabase';

const { width: SCREEN_WIDTH } = Dimensions.get('window');
const CARD_WIDTH = (SCREEN_WIDTH - 48) / 2;

function formatCount(count: number): string {
  if (count >= 1000000) return `${(count / 1000000).toFixed(1)}M`;
  if (count >= 1000) return `${(count / 1000).toFixed(1)}K`;
  return String(count);
}

function genreEmoji(genre: string): string {
  switch (genre) {
    case 'Sci-Fi':
      return '🚀';
    case 'Fantasy':
      return '✨';
    case 'Romance':
      return '💜';
    case 'Horror':
      return '👻';
    default:
      return '🎬';
  }
}

function SeriesPoster({ series }: { series: Series }) {
  const [imageFailed, setImageFailed] = useState(false);
  const showImage = !!series.posterUrl && !imageFailed;

  return (
    <View style={styles.cardPoster}>
      {showImage ? (
        <Image
          source={series.posterUrl}
          style={styles.posterImage}
          contentFit="cover"
          transition={200}
          onError={() => setImageFailed(true)}
        />
      ) : (
        <View style={styles.posterPlaceholder}>
          <Text style={styles.posterEmoji}>{genreEmoji(series.genre)}</Text>
        </View>
      )}
      <View style={styles.cardBadge}>
        <Text style={styles.cardBadgeText}>{series.episodeCount ?? 0} EP</Text>
      </View>
    </View>
  );
}

export default function HomeScreen() {
  const insets = useSafeAreaInsets();
  const router = useRouter();
  const navCount = useRef(0);
  const [seriesList, setSeriesList] = useState<Series[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchText, setSearchText] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');

  // Debounce the search query by ~300ms so filtering happens in real-time
  // without re-rendering the grid on every keystroke.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(searchText.trim()), 300);
    return () => clearTimeout(timer);
  }, [searchText]);

  useEffect(() => {
    (async () => {
      try {
        const { data, error: err } = await supabase
          .from('series')
          .select('*')
          .order('created_at', { ascending: false });
        if (err) throw err;
        const rows = (data ?? []).map((row: any) => ({
          id: row.id,
          title: row.title,
          genre: row.genre,
          description: row.description,
          posterUrl: row.poster_url,
          bannerUrl: row.banner_url,
          playCount: row.play_count,
          episodeCount: row.episode_count,
          episodes: [],
        }) as Series);
        setSeriesList(rows);
      } catch {
        setError('Failed to load series');
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  // Case-insensitive title filter, applied to the already-loaded list.
  const filteredSeries = useMemo(() => {
    const query = debouncedQuery.toLowerCase();
    if (!query) return seriesList;
    return seriesList.filter((s) => s.title.toLowerCase().includes(query));
  }, [debouncedQuery, seriesList]);

  const handleOpenSeries = (series: Series) => {
    navCount.current += 1;
    router.push({
      pathname: '/feed',
      params: {
        seriesId: series.id,
        startEp: '1',
        t: String(navCount.current),
      },
    });
  };

  const renderSeriesCard = ({ item }: ListRenderItemInfo<Series>) => (
    <Pressable
      style={({ pressed }) => [styles.card, pressed && styles.cardPressed]}
      onPress={() => handleOpenSeries(item)}
    >
      <SeriesPoster series={item} />
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

  return (
    <View style={[styles.container, { paddingTop: insets.top }]}>
      <View style={styles.header}>
        <Text style={styles.headerTitle}>Home</Text>
        <Text style={styles.headerSubtitle}>All series</Text>
      </View>

      <View style={styles.searchBar}>
        <Text style={styles.searchIcon}>🔍</Text>
        <TextInput
          style={styles.searchInput}
          value={searchText}
          onChangeText={setSearchText}
          placeholder="Search movies or series..."
          placeholderTextColor="#666"
          autoCapitalize="none"
          autoCorrect={false}
          returnKeyType="search"
        />
        {searchText.length > 0 && (
          <Pressable
            style={styles.searchClear}
            onPress={() => setSearchText('')}
            hitSlop={8}
          >
            <Text style={styles.searchClearIcon}>✕</Text>
          </Pressable>
        )}
      </View>

      <FlatList
        data={filteredSeries}
        renderItem={renderSeriesCard}
        keyExtractor={(item) => item.id}
        numColumns={2}
        columnWrapperStyle={styles.row}
        contentContainerStyle={styles.grid}
        showsVerticalScrollIndicator={false}
        keyboardShouldPersistTaps="handled"
        keyboardDismissMode="on-drag"
        ListEmptyComponent={
          debouncedQuery ? (
            <View style={styles.emptyContainer}>
              <Text style={styles.emptyTitle}>No movies found</Text>
              <Text style={styles.emptyText}>
                No movies found matching “{debouncedQuery}”
              </Text>
            </View>
          ) : (
            <View style={styles.emptyContainer}>
              <Text style={styles.emptyTitle}>No series yet</Text>
              <Text style={styles.emptyText}>
                Use the Telegram bot to add videos.
              </Text>
            </View>
          )
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
  searchBar: {
    flexDirection: 'row',
    alignItems: 'center',
    marginHorizontal: 20,
    marginTop: 10,
    paddingHorizontal: 12,
    height: 44,
    borderRadius: 10,
    backgroundColor: '#1A1A1A',
  },
  searchIcon: {
    fontSize: 15,
    color: '#666',
    marginRight: 8,
  },
  searchInput: {
    flex: 1,
    color: '#fff',
    fontSize: 15,
    paddingVertical: 0,
  },
  searchClear: {
    padding: 4,
  },
  searchClearIcon: {
    color: '#888',
    fontSize: 16,
    fontWeight: '700',
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
  posterImage: {
    width: '100%',
    height: '100%',
  },
  posterPlaceholder: {
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
  emptyTitle: {
    color: '#fff',
    fontSize: 16,
    fontWeight: '700',
  },
  emptyText: {
    color: '#666',
    fontSize: 14,
    marginTop: 8,
  },
});
