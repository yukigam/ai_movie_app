import { useMemo, useRef } from 'react';
import { PanResponder, StyleSheet, View } from 'react-native';

interface SeekBarProps {
  position: number;
  duration: number;
  onSeek: (seconds: number) => void;
  onSeekingChange: (seeking: boolean) => void;
}

/**
 * Drag/tap seekbar built from core React Native components only —
 * no native modules, so it works in Expo Go.
 */
export function SeekBar({ position, duration, onSeek, onSeekingChange }: SeekBarProps) {
  const widthRef = useRef(1);
  const stateRef = useRef({ duration, onSeek, onSeekingChange });
  stateRef.current = { duration, onSeek, onSeekingChange };

  const seekFromX = (x: number) => {
    const frac = Math.max(0, Math.min(1, x / widthRef.current));
    stateRef.current.onSeek(frac * stateRef.current.duration);
  };

  const panResponder = useMemo(
    () =>
      PanResponder.create({
        onStartShouldSetPanResponder: () => true,
        onMoveShouldSetPanResponder: () => true,
        onPanResponderGrant: (evt) => {
          stateRef.current.onSeekingChange(true);
          seekFromX(evt.nativeEvent.locationX);
        },
        onPanResponderMove: (evt) => {
          seekFromX(evt.nativeEvent.locationX);
        },
        onPanResponderRelease: () => {
          stateRef.current.onSeekingChange(false);
        },
        onPanResponderTerminate: () => {
          stateRef.current.onSeekingChange(false);
        },
      }),
    []
  );

  const frac = duration > 0 ? Math.min(1, Math.max(0, position / duration)) : 0;
  const fillWidth = `${frac * 100}%` as `${number}%`;
  const thumbLeft = `${frac * 100}%` as `${number}%`;

  return (
    <View
      style={styles.touchArea}
      onLayout={(e) => {
        widthRef.current = e.nativeEvent.layout.width;
      }}
      {...panResponder.panHandlers}
    >
      <View style={styles.track}>
        <View style={[styles.fill, { width: fillWidth }]} />
      </View>
      <View style={[styles.thumb, { left: thumbLeft }]} />
    </View>
  );
}

const styles = StyleSheet.create({
  touchArea: {
    flex: 1,
    height: 32,
    justifyContent: 'center',
  },
  track: {
    height: 3,
    borderRadius: 1.5,
    backgroundColor: 'rgba(255,255,255,0.35)',
    overflow: 'hidden',
  },
  fill: {
    height: 3,
    backgroundColor: '#FF0000',
  },
  thumb: {
    position: 'absolute',
    top: '50%',
    marginTop: -5,
    marginLeft: -5,
    width: 10,
    height: 10,
    borderRadius: 5,
    backgroundColor: '#FF0000',
  },
});
