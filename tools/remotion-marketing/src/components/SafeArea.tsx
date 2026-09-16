import React from 'react';
import {useVideoConfig} from 'remotion';
import {safeMarginPx} from '../theme';

/**
 * Constrains children to the title/action-safe zone. Every text element in the
 * system renders inside a SafeArea so captions and headlines never clip on
 * cropped players. QA asserts the margin ratio matches `SAFE_MARGIN_RATIO`.
 */
export const SafeArea: React.FC<{
  enabled?: boolean;
  children: React.ReactNode;
}> = ({enabled = true, children}) => {
  const {width, height} = useVideoConfig();
  const margin = safeMarginPx(width, height);
  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        padding: enabled ? `${margin.y}px ${margin.x}px` : 0,
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'center',
        alignItems: 'center',
      }}
    >
      {children}
    </div>
  );
};
