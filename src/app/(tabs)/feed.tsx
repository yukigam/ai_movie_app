import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocalSearchParams } from 'expo-router';
import {
  View,
  Text,
  FlatList,
  Pressable,
  Share,
  Dimensions,
  StyleSheet,
  ListRenderItemInfo,
  Modal,
  ActivityIndicator,
} from 'react-native';
import Slider from '@react-native-community/slider';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { Episode, Series } from '@/types/series';
import { VideoPlayer, VideoPlayerHandle } from '@/components/video-player';
import { EpisodeModal } from '@/components/episode-modal';
import { useRewardedAd } from '@/hooks/use-rewarded-ad';
import { fetchFeed } from '@/lib/api';

const { width: SCREEN_WIDTH, height: SCREEN_HEIGHT } = Dimensions.get('window');

interface FeedItem {
  seriesId: string;
  episode: Episode;
}

export default function FeedScreen() {
  const insets = useSafeAreaInsets();
  const params = useLocalSearchParams<{ seriesId?: string; startEp?: string; t?: string }>();
  const [feedItems, setFeedItems] = useState<FeedItem[]>([]);
  const [seriesMap, setSeriesMap] = useState<Record<string, Series>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [isPlaying, setIsPlaying] = useState(true);
  const [showControls, setShowControls] = useState(false);
  const [position, setPosition] = useState(0);
  const [duration, setDuration] = useState(0);
  const [isSeeking, setIsSeeking] = useState(false);
  const [seekPosition, setSeekPosition] = useState(0);
  const [likedEpisodes, setLikedEpisodes] = useState<Record<string, boolean>>({});
  const [unlockedEpisodes, setUnlockedEpisodes] = useState<Record<string, boolean>>({});
  const [modalVisible, setModalVisible] = useState(false);
  const adDataRef = useRef<{ seriesId: string; episodeId: string } | null>(null);
  const videoRefs = useRef<Record<string, VideoPlayerHandle | null>>({});

  useEffect(() => {
    (async () => {
      try {
        const feed = await fetchFeed();
        const items: FeedItem[] = [];
        const map: Record<string, Series> = {};
        for (const f of feed) {
          map[f.series.id] = f.series;
          items.push({ seriesId: f.series.id, episode: f.episode });
        }
        setFeedItems(items);
        setSeriesMap(map);
      } catch {
        setError("Failed to load feed");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const { isLoaded, isEarnedReward, showAd, consumeReward, isShowingAd, adCountdown } = useRewardedAd();

  const visibleItems = useMemo(() => {
    if (!params.seriesId) return feedItems;
    return feedItems.filter((item) => item.seriesId === params.seriesId);
  }, [feedItems, params.seriesId]);

  const activeItem = visibleItems[activeIndex];
  const activeSeries = activeItem ? seriesMap[activeItem.seriesId] ?? null : null;
  const activeSeriesEpisodes = activeSeries?.episodes ?? [];

  const handleViewableItemsChanged = useCallback(
    ({ changed }: { changed: { index: number | null; isViewable: boolean }[] }) => {
      for (const item of changed) {
        if (item.isViewable && item.index !== null) {
          setActiveIndex(item.index);
        }
      }
    },
    []
  );

  const viewabilityConfig = {
    itemVisiblePercentThreshold: 70,
  };

  const handleLike = useCallback((episodeId: string) => {
    setLikedEpisodes((prev) => ({ ...prev, [episodeId]: !prev[episodeId] }));
  }, []);

  const handleShare = useCallback(
    (seriesTitle: string, episodeNumber?: number) => {
      try {
        Share.share({
          message: `Энэ киног үзээрэй: ${seriesTitle} (EP ${episodeNumber ?? '?'})\nЛинкаар орж үзэх: https://ai-movie-app.onrender.com`,
        });
      } catch (error) {
        console.log((error as Error).message);
      }
    },
    []
  );

  const handleSelectEpisode = useCallback(
    (episode: Episode) => {
      const seriesIndex = visibleItems.findIndex(
        (item) => item.episode.id === episode.id
      );
      if (seriesIndex !== -1) {
        flatListRef.current?.scrollToIndex({ index: seriesIndex, animated: true });
      }
    },
    [visibleItems]
  );

  const handleWatchAd = useCallback(
    (episode: Episode) => {
      adDataRef.current = { seriesId: episode.seriesId, episodeId: episode.id };
      showAd();
    },
    [showAd]
  );

  useEffect(() => {
    setIsPlaying(true);
  }, [activeIndex]);

  const controlsTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (controlsTimer.current) clearTimeout(controlsTimer.current);
    if (showControls) {
      controlsTimer.current = setTimeout(() => setShowControls(false), 3000);
    }
    return () => {
      if (controlsTimer.current) clearTimeout(controlsTimer.current);
    };
  }, [showControls]);

  const resetControlsTimer = useCallback(() => {
    if (controlsTimer.current) clearTimeout(controlsTimer.current);
    controlsTimer.current = setTimeout(() => setShowControls(false), 3000);
  }, []);

  const handleToggleControls = useCallback(() => {
    setShowControls((prev) => !prev);
  }, []);

  const handleTogglePlay = useCallback(() => {
    const handle = activeItem?.episode.id ? videoRefs.current[activeItem.episode.id] : null;
    if (!handle) return;
    if (isPlaying) {
      handle.pause();
    } else {
      handle.play();
    }
    setIsPlaying((prev) => !prev);
    resetControlsTimer();
  }, [activeItem, isPlaying, resetControlsTimer]);

  const handleSeekTo = useCallback(
    (seconds: number) => {
      const handle = activeItem?.episode.id ? videoRefs.current[activeItem.episode.id] : null;
      if (!handle) return;
      handle.seekTo(seconds);
      setPosition(seconds);
      resetControlsTimer();
    },
    [activeItem, resetControlsTimer]
  );

  useEffect(() => {
    if (!showControls) return;
    const handle = activeItem?.episode.id ? videoRefs.current[activeItem.episode.id] : null;
    if (!handle) return;
    const id = setInterval(() => {
      setPosition(handle.getCurrentTime());
      setDuration(handle.getDuration());
    }, 250);
    return () => clearInterval(id);
  }, [showControls, activeItem?.episode.id]);

  useEffect(() => {
    if (isEarnedReward && adDataRef.current) {
      const { episodeId } = adDataRef.current;
      setUnlockedEpisodes((prev) => ({ ...prev, [episodeId]: true }));
      adDataRef.current = null;
      consumeReward();
    }
  }, [isEarnedReward, consumeReward]);

  const flatListRef = useRef<FlatList<FeedItem>>(null);

  useEffect(() => {
    if (!params.seriesId || visibleItems.length === 0) return;
    const startEp = Number(params.startEp ?? '1') || 1;
    let index = visibleItems.findIndex(
      (item) => item.episode.episodeNumber === startEp
    );
    if (index === -1) index = 0;
    const timer = setTimeout(() => {
      flatListRef.current?.scrollToIndex({ index, animated: false });
      setActiveIndex(index);
    }, 80);
    return () => clearTimeout(timer);
  }, [visibleItems, params.seriesId, params.startEp, params.t]);

  const formatCount = (count: number): string => {
    if (count >= 1000000) return `${(count / 1000000).toFixed(1)}M`;
    if (count >= 1000) return `${(count / 1000).toFixed(1)}K`;
    return String(count);
  };

  const formatTime = (seconds: number): string => {
    if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
    const s = Math.floor(seconds);
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, '0')}`;
  };

  useEffect(() => {
    const ep = activeItem?.episode;
    if (ep) {
      console.log('Current Video URL:', ep.videoUrl);
    }
  }, [activeItem]);

  const renderItem = useCallback(
    ({ item, index }: ListRenderItemInfo<FeedItem>) => {
      const series = seriesMap[item.seriesId];
      if (!series) return null;

      const isActive = index === activeIndex;
      const isLiked = likedEpisodes[item.episode.id] ?? false;
      const isUnlocked = !item.episode.isLocked || unlockedEpisodes[item.episode.id];

      const ep = item.episode;
      return (
        <View style={styles.feedItem}>
          <VideoPlayer
            ref={(r) => {
              videoRefs.current[item.episode.id] = r;
            }}
            uri={ep.videoUrl || ''}
            isActive={isActive}
          />

          <Pressable style={styles.tapLayer} onPress={handleToggleControls} />

          {showControls && (
            <View style={[styles.topBar, { paddingTop: insets.top + 12 }]} pointerEvents="none">
              <Text style={styles.brandText}>AI Series</Text>
            </View>
          )}

          {showControls && (
            <Pressable style={styles.centerPlayButton} onPress={handleTogglePlay}>
              <Text style={styles.centerPlayIcon}>{isPlaying ? '⏸' : '▶'}</Text>
            </Pressable>
          )}

          {showControls && (
            <View style={styles.controlsBottom}>
              <View style={styles.seekRow}>
                <Text style={styles.timeText}>{formatTime(isSeeking ? seekPosition : position)}</Text>
                <Slider
                  style={styles.seekBar}
                  minimumValue={0}
                  maximumValue={Math.max(duration, 1)}
                  value={isSeeking ? seekPosition : position}
                  onSlidingStart={() => setIsSeeking(true)}
                  onValueChange={setSeekPosition}
                  onSlidingComplete={(v) => {
                    setIsSeeking(false);
                    handleSeekTo(v);
                  }}
                  minimumTrackTintColor="#FF0000"
                  maximumTrackTintColor="rgba(255,255,255,0.35)"
                  thumbTintColor="#FF0000"
                />
                <Text style={styles.timeText}>{formatTime(duration)}</Text>
              </View>

            <View style={styles.bottomSection}>
              <View style={styles.infoArea}>
                <Text style={styles.seriesTitle}>{series.title || ''}</Text>
                <View style={styles.episodeRow}>
                  <Text style={styles.episodeTag}>
                    EP {ep.episodeNumber ?? '?'}
                  </Text>
                  <Text style={styles.episodeTitle}>{ep.title || ''}</Text>
                </View>
                <Text style={styles.description} numberOfLines={2}>
                  {ep.description || ''}
                </Text>

                {item.episode.isLocked && !isUnlocked && (
                  <Pressable
                    style={({ pressed }) => [
                      styles.unlockButton,
                      pressed && styles.unlockButtonPressed,
                    ]}
                    onPress={() => handleWatchAd(item.episode)}
                  >
                    <Text style={styles.unlockButtonText}>
                      {isLoaded ? 'WATCH AD TO UNLOCK' : 'LOADING AD...'}
                    </Text>
                  </Pressable>
                )}
              </View>
            </View>
            </View>
          )}

          <View
            style={[styles.actionRail, { bottom: showControls ? 220 : 60 }]}
            pointerEvents="box-none"
          >
            <Pressable
              style={styles.actionButton}
              onPress={() => handleLike(item.episode.id)}
            >
              <Text style={[styles.actionIcon, isLiked && styles.actionIconActive]}>
                {isLiked ? '♥' : '♡'}
              </Text>
              <Text style={styles.actionLabel}>
                {isLiked ? 'Liked' : 'Like'}
              </Text>
            </Pressable>

            <Pressable
              style={styles.actionButton}
              onPress={() => handleShare(series.title, ep.episodeNumber)}
            >
              <Text style={styles.actionIcon}>↗</Text>
              <Text style={styles.actionLabel}>Share</Text>
            </Pressable>

            <Pressable
              style={styles.actionButton}
              onPress={() => setModalVisible(true)}
            >
              <Text style={styles.actionIcon}>☰</Text>
              <Text style={styles.actionLabel}>Episodes</Text>
            </Pressable>

            <View style={styles.actionButton}>
              <Text style={styles.actionIcon}>▶</Text>
              <Text style={styles.actionLabel}>
                {formatCount(series.playCount)}
              </Text>
            </View>
          </View>
        </View>
      );
    },
    [
      activeIndex,
      likedEpisodes,
      unlockedEpisodes,
      insets.top,
      handleLike,
      handleShare,
      handleWatchAd,
      handleTogglePlay,
      handleSeekTo,
      handleToggleControls,
      isPlaying,
      isLoaded,
      showControls,
      position,
      duration,
      isSeeking,
      seekPosition,
      seriesMap,
    ]
  );

  const keyExtractor = useCallback(
    (item: FeedItem) => `${item.seriesId}-${item.episode.id}`,
    []
  );

  const getItemLayout = useCallback(
    (_: unknown, index: number) => ({
      length: SCREEN_HEIGHT,
      offset: SCREEN_HEIGHT * index,
      index,
    }),
    []
  );

  if (loading) {
    return (
      <View style={[styles.container, styles.centered]}>
        <ActivityIndicator color="#E50914" size="large" />
        <Text style={{ color: '#888', marginTop: 16 }}>Loading feed...</Text>
      </View>
    );
  }

  if (error || !activeSeries) {
    return (
      <View style={[styles.container, styles.centered]}>
        <Text style={{ color: '#E50914', fontSize: 16, fontWeight: '700' }}>
          {error || 'No content'}
        </Text>
        <Text style={{ color: '#666', marginTop: 8, textAlign: 'center', paddingHorizontal: 32 }}>
          {error ? 'Pull down to retry.' : 'Use the Telegram bot to add videos.'}
        </Text>
      </View>
    );
  }

  return (
    <View style={styles.container}>
      <FlatList
        ref={flatListRef}
        data={visibleItems}
        renderItem={renderItem}
        keyExtractor={keyExtractor}
        getItemLayout={getItemLayout}
        onScrollToIndexFailed={({ index }) => {
          setTimeout(() => {
            flatListRef.current?.scrollToIndex({ index, animated: false });
          }, 250);
        }}
        pagingEnabled
        showsVerticalScrollIndicator={false}
        snapToInterval={SCREEN_HEIGHT}
        snapToAlignment="start"
        decelerationRate="fast"
        viewabilityConfig={viewabilityConfig}
        onViewableItemsChanged={handleViewableItemsChanged}
        removeClippedSubviews
        initialNumToRender={3}
        maxToRenderPerBatch={3}
        windowSize={3}
      />

      <EpisodeModal
        visible={modalVisible}
        onClose={() => setModalVisible(false)}
        seriesTitle={activeSeries.title}
        episodes={activeSeriesEpisodes}
        currentEpisodeId={activeItem?.episode.id}
        unlockedEpisodes={unlockedEpisodes}
        onSelectEpisode={handleSelectEpisode}
        onWatchAd={handleWatchAd}
      />

      <Modal visible={isShowingAd} transparent animationType="fade">
        <View style={styles.adOverlay}>
          <View style={styles.adContainer}>
            <Text style={styles.adLabel}>Sponsored</Text>
            <View style={styles.adPlaceholder}>
              <Text style={styles.adEmoji}>▶</Text>
            </View>
            <Text style={styles.adTimer}>{adCountdown}</Text>
            <Text style={styles.adHint}>
              {adCountdown > 0
                ? 'Watch ad to unlock this episode...'
                : 'Rewarding...'}
            </Text>
          </View>
        </View>
      </Modal>
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
  feedItem: {
    width: SCREEN_WIDTH,
    height: SCREEN_HEIGHT,
    backgroundColor: '#000',
  },
  tapLayer: {
    ...StyleSheet.absoluteFill,
  },
  topBar: {
    paddingHorizontal: 16,
    paddingBottom: 8,
  },
  controlsBottom: {
    position: 'absolute',
    left: 0,
    right: 0,
    bottom: 0,
  },
  seekRow: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 16,
    paddingBottom: 4,
    gap: 8,
  },
  seekBar: {
    flex: 1,
    height: 32,
    marginVertical: -10,
  },
  timeText: {
    color: '#fff',
    fontSize: 12,
    fontVariant: ['tabular-nums'],
  },
  actionRail: {
    position: 'absolute',
    right: 12,
    alignItems: 'center',
    gap: 16,
  },
  centerPlayButton: {
    position: 'absolute',
    top: '50%',
    left: '50%',
    marginTop: -36,
    marginLeft: -36,
    width: 72,
    height: 72,
    borderRadius: 36,
    backgroundColor: 'rgba(0,0,0,0.55)',
    borderWidth: 1,
    borderColor: 'rgba(255,255,255,0.35)',
    justifyContent: 'center',
    alignItems: 'center',
  },
  centerPlayIcon: {
    color: '#fff',
    fontSize: 34,
    marginLeft: 4,
  },
  brandText: {
    color: '#E50914',
    fontSize: 18,
    fontWeight: '800',
    letterSpacing: 1,
  },
  bottomSection: {
    flexDirection: 'row',
    paddingHorizontal: 16,
    paddingBottom: 40,
    alignItems: 'flex-end',
  },
  infoArea: {
    flex: 1,
    gap: 6,
  },
  seriesTitle: {
    color: '#fff',
    fontSize: 22,
    fontWeight: '800',
  },
  episodeRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
  },
  episodeTag: {
    color: '#E50914',
    fontSize: 12,
    fontWeight: '700',
    backgroundColor: 'rgba(229,9,20,0.15)',
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 3,
    overflow: 'hidden',
  },
  episodeTitle: {
    color: '#ddd',
    fontSize: 16,
    fontWeight: '600',
  },
  description: {
    color: '#aaa',
    fontSize: 13,
    lineHeight: 18,
    marginTop: 2,
  },
  unlockButton: {
    backgroundColor: '#E50914',
    paddingHorizontal: 16,
    paddingVertical: 10,
    borderRadius: 8,
    alignSelf: 'flex-start',
    marginTop: 8,
  },
  unlockButtonPressed: {
    opacity: 0.7,
  },
  unlockButtonText: {
    color: '#fff',
    fontSize: 12,
    fontWeight: '700',
    letterSpacing: 0.5,
  },
  actionButton: {
    alignItems: 'center',
    gap: 4,
  },
  actionIcon: {
    color: '#fff',
    fontSize: 26,
  },
  actionIconActive: {
    color: '#E50914',
  },
  actionLabel: {
    color: '#fff',
    fontSize: 11,
  },
  adOverlay: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.85)',
    justifyContent: 'center',
    alignItems: 'center',
  },
  adContainer: {
    backgroundColor: '#111',
    borderRadius: 16,
    padding: 32,
    alignItems: 'center',
    gap: 16,
    width: SCREEN_WIDTH * 0.8,
  },
  adLabel: {
    color: '#E50914',
    fontSize: 12,
    fontWeight: '700',
    letterSpacing: 2,
    textTransform: 'uppercase',
  },
  adPlaceholder: {
    width: 120,
    height: 80,
    backgroundColor: '#1A1A1A',
    borderRadius: 8,
    justifyContent: 'center',
    alignItems: 'center',
  },
  adEmoji: {
    fontSize: 32,
    color: '#E50914',
  },
  adTimer: {
    color: '#fff',
    fontSize: 36,
    fontWeight: '800',
  },
  adHint: {
    color: '#888',
    fontSize: 13,
    textAlign: 'center',
  },
});
