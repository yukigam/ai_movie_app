import { useCallback } from 'react';
import {
  View,
  Text,
  Modal,
  Pressable,
  ScrollView,
  StyleSheet,
  Dimensions,
} from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { Episode } from '@/types/series';

const { height: SCREEN_HEIGHT } = Dimensions.get('window');

interface EpisodeModalProps {
  visible: boolean;
  onClose: () => void;
  seriesTitle: string;
  episodes: Episode[];
  currentEpisodeId?: string;
  unlockedEpisodes: Record<string, boolean>;
  onSelectEpisode: (episode: Episode) => void;
  onWatchAd: (episode: Episode) => void;
}

export function EpisodeModal({
  visible,
  onClose,
  seriesTitle,
  episodes,
  currentEpisodeId,
  unlockedEpisodes,
  onSelectEpisode,
  onWatchAd,
}: EpisodeModalProps) {
  const insets = useSafeAreaInsets();

  const handlePress = useCallback(
    (episode: Episode) => {
      if (!episode.isLocked || unlockedEpisodes[episode.id]) {
        onSelectEpisode(episode);
        onClose();
      } else {
        onWatchAd(episode);
      }
    },
    [unlockedEpisodes, onSelectEpisode, onClose, onWatchAd]
  );

  return (
    <Modal
      visible={visible}
      animationType="slide"
      transparent
      onRequestClose={onClose}
    >
      <View style={styles.overlay}>
        <Pressable style={styles.backdrop} onPress={onClose} />
        <View style={[styles.sheet, { paddingBottom: insets.bottom + 20 }]}>
          <View style={styles.handle} />
          <Text style={styles.sheetTitle}>{seriesTitle}</Text>
          <Text style={styles.sheetSubtitle}>Episodes</Text>

          <ScrollView style={styles.list} showsVerticalScrollIndicator={false}>
            {(episodes || []).map((episode) => {
              const isCurrent = episode.id === currentEpisodeId;
              const isUnlocked = !episode.isLocked || unlockedEpisodes[episode.id];

              return (
                <Pressable
                  key={episode.id}
                  style={({ pressed }) => [
                    styles.episodeItem,
                    isCurrent && styles.episodeItemActive,
                    pressed && styles.episodeItemPressed,
                  ]}
                  onPress={() => handlePress(episode)}
                >
                  <View style={styles.episodeLeft}>
                    <Text style={styles.episodeNumber}>EP {episode.episodeNumber}</Text>
                    <View style={styles.episodeInfo}>
                      <Text style={styles.episodeTitle} numberOfLines={1}>
                        {episode.title}
                      </Text>
                      <Text style={styles.episodeDuration}>{episode.duration ?? 0} min</Text>
                    </View>
                  </View>

                  <View style={styles.episodeRight}>
                    {episode.isFree && isUnlocked && (
                      <View style={styles.freeBadge}>
                        <Text style={styles.freeBadgeText}>FREE</Text>
                      </View>
                    )}
                    {episode.isLocked && !isUnlocked && (
                      <View style={styles.lockedBadge}>
                        <Text style={styles.lockedBadgeText}>LOCKED</Text>
                      </View>
                    )}
                    {isUnlocked && !episode.isFree && (
                      <View style={styles.unlockedBadge}>
                        <Text style={styles.unlockedBadgeText}>UNLOCKED</Text>
                      </View>
                    )}
                  </View>
                </Pressable>
              );
            })}
          </ScrollView>
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  overlay: {
    flex: 1,
    justifyContent: 'flex-end',
  },
  backdrop: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.6)',
  },
  sheet: {
    backgroundColor: '#111',
    borderTopLeftRadius: 20,
    borderTopRightRadius: 20,
    maxHeight: SCREEN_HEIGHT * 0.65,
    paddingTop: 12,
    paddingHorizontal: 20,
  },
  handle: {
    width: 36,
    height: 4,
    borderRadius: 2,
    backgroundColor: '#333',
    alignSelf: 'center',
    marginBottom: 16,
  },
  sheetTitle: {
    color: '#fff',
    fontSize: 20,
    fontWeight: '700',
  },
  sheetSubtitle: {
    color: '#888',
    fontSize: 14,
    marginTop: 4,
    marginBottom: 16,
  },
  list: {
    flexGrow: 0,
  },
  episodeItem: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingVertical: 14,
    paddingHorizontal: 12,
    borderRadius: 12,
    marginBottom: 8,
    backgroundColor: '#1A1A1A',
  },
  episodeItemActive: {
    borderColor: '#E50914',
    borderWidth: 1,
  },
  episodeItemPressed: {
    opacity: 0.7,
  },
  episodeLeft: {
    flexDirection: 'row',
    alignItems: 'center',
    flex: 1,
    gap: 12,
  },
  episodeNumber: {
    color: '#E50914',
    fontSize: 13,
    fontWeight: '700',
    minWidth: 32,
  },
  episodeInfo: {
    flex: 1,
  },
  episodeTitle: {
    color: '#fff',
    fontSize: 15,
    fontWeight: '500',
  },
  episodeDuration: {
    color: '#666',
    fontSize: 12,
    marginTop: 2,
  },
  episodeRight: {
    flexDirection: 'row',
    alignItems: 'center',
  },
  freeBadge: {
    backgroundColor: '#00C853',
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 4,
  },
  freeBadgeText: {
    color: '#000',
    fontSize: 11,
    fontWeight: '700',
  },
  lockedBadge: {
    backgroundColor: '#333',
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 4,
  },
  lockedBadgeText: {
    color: '#888',
    fontSize: 11,
    fontWeight: '700',
  },
  unlockedBadge: {
    backgroundColor: '#6B46C1',
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 4,
  },
  unlockedBadgeText: {
    color: '#fff',
    fontSize: 11,
    fontWeight: '700',
  },
});
