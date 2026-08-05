import { useCallback, useEffect, useRef, useState } from 'react';

export interface UseRewardedAdResult {
  isLoaded: boolean;
  isEarnedReward: boolean;
  showAd: () => void;
  consumeReward: () => void;
  isShowingAd: boolean;
  adCountdown: number;
}

const AD_DURATION_SECONDS = 4;

export function useRewardedAd(): UseRewardedAdResult {
  const [isLoaded] = useState(true);
  const [isEarnedReward, setIsEarnedReward] = useState(false);
  const [isShowingAd, setIsShowingAd] = useState(false);
  const [adCountdown, setAdCountdown] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const consumedRef = useRef(false);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []);

  const showAd = useCallback(() => {
    if (isShowingAd) return;

    consumedRef.current = false;
    setIsEarnedReward(false);
    setIsShowingAd(true);
    setAdCountdown(AD_DURATION_SECONDS);

    let remaining = AD_DURATION_SECONDS;
    timerRef.current = setInterval(() => {
      remaining--;
      if (remaining <= 0) {
        if (timerRef.current) clearInterval(timerRef.current);
        timerRef.current = null;
        setIsShowingAd(false);
        setIsEarnedReward(true);
        setAdCountdown(0);
      } else {
        setAdCountdown(remaining);
      }
    }, 1000);
  }, [isShowingAd]);

  const consumeReward = useCallback(() => {
    if (consumedRef.current) return;
    consumedRef.current = true;
    setIsEarnedReward(false);
  }, []);

  return { isLoaded, isEarnedReward, showAd, consumeReward, isShowingAd, adCountdown };
}
