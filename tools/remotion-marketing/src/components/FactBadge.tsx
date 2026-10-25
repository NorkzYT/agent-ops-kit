import React from 'react';
import {fonts} from '../theme';
import type {Brand, Fact} from '../schema';

/**
 * Renders a supported fact. Only facts that exist in the brief's facts file are
 * ever passed here (the claims checker enforces it), so anything shown in a
 * FactBadge has cited evidence behind it.
 */
export const FactBadge: React.FC<{fact: Fact; brand: Brand}> = ({
  fact,
  brand,
}) => (
  <div
    style={{
      display: 'inline-flex',
      alignItems: 'center',
      gap: 14,
      padding: '14px 26px',
      borderRadius: 999,
      background: 'rgba(255,255,255,0.14)',
      border: `2px solid ${brand.accentColor}`,
      color: '#fff',
      fontFamily: fonts.body,
      fontSize: 34,
      fontWeight: 600,
    }}
  >
    <span
      style={{
        width: 12,
        height: 12,
        borderRadius: 999,
        background: brand.accentColor,
      }}
    />
    {fact.claim}
  </div>
);
