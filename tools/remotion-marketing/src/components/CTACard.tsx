import React from 'react';
import {spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {fonts} from '../theme';
import type {Brand} from '../schema';

/** Call-to-action pill used in the outro scene. */
export const CTACard: React.FC<{
  text: string;
  url?: string;
  brand: Brand;
}> = ({text, url, brand}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const scale = spring({frame, fps, config: {damping: 14, mass: 0.6}});
  return (
    <div
      style={{
        transform: `scale(${scale})`,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 18,
      }}
    >
      <div
        style={{
          padding: '22px 48px',
          borderRadius: 16,
          background: brand.accentColor,
          color: '#fff',
          fontFamily: fonts.display,
          fontWeight: 800,
          fontSize: 52,
        }}
      >
        {text}
      </div>
      {url ? (
        <span
          style={{
            fontFamily: fonts.body,
            fontSize: 32,
            color: 'rgba(255,255,255,0.9)',
          }}
        >
          {url}
        </span>
      ) : null}
    </div>
  );
};
