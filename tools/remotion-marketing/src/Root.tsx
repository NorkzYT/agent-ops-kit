import React from 'react';
import {Composition} from 'remotion';
import {MarketingVideo} from './compositions/MarketingVideo';
import {
  DIMENSIONS,
  marketingVideoSchema,
  totalDurationInFrames,
  type MarketingVideoProps,
} from './schema';
import sampleProps from '../props/sample.json';

/**
 * Registers the reusable composition. Dimensions, fps and duration are derived
 * from props via `calculateMetadata`, so one composition id renders 16:9, 9:16
 * and 1:1 for any campaign without duplicated definitions.
 */
export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="MarketingVideo"
      component={MarketingVideo}
      schema={marketingVideoSchema}
      defaultProps={sampleProps as MarketingVideoProps}
      calculateMetadata={({props}: {props: MarketingVideoProps}) => {
        const dims = DIMENSIONS[props.aspectRatio];
        return {
          width: dims.width,
          height: dims.height,
          fps: props.fps,
          durationInFrames: totalDurationInFrames(props),
        };
      }}
    />
  );
};
