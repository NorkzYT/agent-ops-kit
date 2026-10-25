import React from 'react';
import {Img, staticFile} from 'remotion';
import type {Brand} from '../schema';
import {fonts} from '../theme';

/**
 * Renders the brand logo from a resolved asset path, or a generated wordmark
 * fallback (brand initial + name) when no logo asset is supplied. The fallback
 * keeps the system rights-clean for demos that ship no external image.
 */
export const Logo: React.FC<{brand: Brand; src?: string; size?: number}> = ({
  brand,
  src,
  size = 96,
}) => {
  if (src) {
    return (
      <Img
        src={staticFile(src)}
        style={{height: size, width: 'auto', objectFit: 'contain'}}
      />
    );
  }
  const initial = brand.name.trim().charAt(0).toUpperCase() || '•';
  return (
    <div style={{display: 'flex', alignItems: 'center', gap: size * 0.25}}>
      <div
        style={{
          width: size,
          height: size,
          borderRadius: size * 0.24,
          background: brand.accentColor,
          color: '#fff',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontFamily: fonts.display,
          fontWeight: 800,
          fontSize: size * 0.5,
        }}
      >
        {initial}
      </div>
      <span
        style={{
          fontFamily: fonts.display,
          fontWeight: 700,
          fontSize: size * 0.45,
          color: '#fff',
        }}
      >
        {brand.name}
      </span>
    </div>
  );
};
