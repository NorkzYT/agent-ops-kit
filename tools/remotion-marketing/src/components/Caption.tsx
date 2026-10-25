import React from 'react';
import {fonts} from '../theme';

/**
 * Lower-third caption. Present on every scene when `captions` is enabled so the
 * video stays legible with sound off. QA treats the caption track as the
 * accessibility signal.
 */
export const Caption: React.FC<{text: string}> = ({text}) => (
  <div
    style={{
      position: 'absolute',
      bottom: '4%',
      left: '50%',
      transform: 'translateX(-50%)',
      maxWidth: '86%',
      padding: '10px 22px',
      borderRadius: 12,
      background: 'rgba(0,0,0,0.55)',
      color: '#fff',
      fontFamily: fonts.body,
      fontSize: 30,
      lineHeight: 1.3,
      textAlign: 'center',
    }}
  >
    {text}
  </div>
);
