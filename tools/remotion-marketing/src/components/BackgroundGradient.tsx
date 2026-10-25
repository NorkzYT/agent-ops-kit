import React from 'react';
import {interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import type {Brand} from '../schema';
import {brandGradient} from '../theme';

/**
 * Brand-coloured gradient backdrop with slow, fully generated geometric motion
 * (drifting accent blobs). No third-party imagery is involved, so it is always
 * rights-clean and works as the default background for any scene.
 */
export const BackgroundGradient: React.FC<{brand: Brand}> = ({brand}) => {
  const frame = useCurrentFrame();
  const {width, height, durationInFrames} = useVideoConfig();
  const drift = interpolate(frame, [0, durationInFrames], [0, 1], {
    extrapolateRight: 'clamp',
  });
  const blob = (cx: number, cy: number, r: number, phase: number) => {
    const dx = Math.sin((drift + phase) * Math.PI * 2) * width * 0.05;
    const dy = Math.cos((drift + phase) * Math.PI * 2) * height * 0.05;
    return (
      <circle
        cx={cx + dx}
        cy={cy + dy}
        r={r}
        fill={brand.accentColor}
        opacity={0.18}
      />
    );
  };
  return (
    <div style={{position: 'absolute', inset: 0}}>
      <div
        style={{
          position: 'absolute',
          inset: 0,
          background: brandGradient(brand),
        }}
      />
      <svg
        width={width}
        height={height}
        style={{position: 'absolute', inset: 0}}
      >
        {blob(width * 0.2, height * 0.3, width * 0.18, 0)}
        {blob(width * 0.8, height * 0.7, width * 0.22, 0.5)}
      </svg>
    </div>
  );
};
