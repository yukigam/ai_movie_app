import { useEvent, useEventListener } from 'expo';
import { useVideoPlayer, VideoView } from 'expo-video';
import { useEffect, useRef, useState } from 'react';
import { View, Text, StyleSheet } from 'react-native';

const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 1500;

interface VideoPlayerProps {
  uri: string;
  isActive: boolean;
}

function normalizeUrl(uri: string): string {
  const trimmed = uri.trim();
  if (/\/storage\/v1\/object\/(?:sign|authenticated)\//.test(trimmed)) {
    return trimmed.replace(
      /\/storage\/v1\/object\/(?:sign|authenticated)\//,
      '/storage/v1/object/public/'
    );
  }
  return trimmed;
}

export function VideoPlayer({ uri, isActive }: VideoPlayerProps) {
  const source = normalizeUrl(uri);
  const hasUri = source.length > 0;

  const player = useVideoPlayer(hasUri ? { uri: source } : null, (p) => {
    p.loop = true;
    p.muted = false;
  });

  const { status } = useEvent(player, 'statusChange', { status: player.status });
  const [retryCount, setRetryCount] = useState(0);
  const retryScheduled = useRef(false);
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEventListener(player, 'statusChange', ({ status: s, error }) => {
    if (s === 'readyToPlay') {
      setRetryCount(0);
    }
    if (s === 'error') {
      console.log('Video Playback Error:', error);
    }
  });

  useEffect(() => {
    if (status !== 'error' || !hasUri) return;
    if (retryScheduled.current || retryCount >= MAX_RETRIES) return;
    retryScheduled.current = true;
    console.log(
      `Video retry ${retryCount + 1}/${MAX_RETRIES}:`,
      source
    );
    retryTimer.current = setTimeout(() => {
      retryScheduled.current = false;
      player.replaceAsync({ uri: source });
      setRetryCount((c) => c + 1);
    }, RETRY_DELAY_MS);
    return () => {
      if (retryTimer.current) clearTimeout(retryTimer.current);
    };
  }, [status, retryCount, player, source, hasUri]);

  useEffect(() => {
    if (isActive) {
      player.play();
    } else {
      player.pause();
    }
  }, [isActive, player]);

  const isError = status === 'error';
  const isMissing = !hasUri;
  const isLoading =
    !isError && (status === 'idle' || status === 'loading' || status === undefined);
  const showRetrying = isError && retryCount < MAX_RETRIES;
  const showFailed = isError && retryCount >= MAX_RETRIES;

  return (
    <View style={styles.container}>
      {hasUri && (
        <VideoView
          style={styles.video}
          player={player}
          nativeControls={false}
          contentFit="cover"
        />
      )}
      {isMissing && (
        <View style={styles.loadingOverlay}>
          <Text style={styles.missingText}>
            Бичлэг олдсонгүй (Video URL missing)
          </Text>
        </View>
      )}
      {!isMissing && isLoading && (
        <View style={styles.loadingOverlay}>
          <Text style={styles.loadingText}>Loading...</Text>
        </View>
      )}
      {showRetrying && (
        <View style={styles.loadingOverlay}>
          <Text style={styles.retryText}>Retrying...</Text>
        </View>
      )}
      {showFailed && (
        <View style={styles.loadingOverlay}>
          <Text style={styles.errorText}>Бичлэг тоглуулж чадсангүй</Text>
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#000',
  },
  video: {
    flex: 1,
  },
  loadingOverlay: {
    ...StyleSheet.absoluteFill,
    backgroundColor: 'rgba(0,0,0,0.5)',
    justifyContent: 'center',
    alignItems: 'center',
  },
  loadingText: {
    color: '#fff',
    fontSize: 16,
  },
  missingText: {
    color: '#fff',
    fontSize: 14,
    textAlign: 'center',
    paddingHorizontal: 24,
  },
  retryText: {
    color: '#ffa500',
    fontSize: 14,
  },
  errorText: {
    color: '#E50914',
    fontSize: 14,
  },
});
