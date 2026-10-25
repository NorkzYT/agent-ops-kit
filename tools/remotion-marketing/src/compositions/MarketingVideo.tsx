import React from 'react';
import {AbsoluteFill, Audio, Sequence, staticFile} from 'remotion';
import {SceneView} from '../components/SceneView';
import type {MarketingVideoProps} from '../schema';

/**
 * The reusable marketing composition. It is pure: everything it draws comes
 * from typed props, so the same component renders any campaign in any aspect
 * ratio. Media, logo and music are resolved from `assetPaths` (built by the
 * pipeline from the rights-checked provenance manifest).
 */
export const MarketingVideo: React.FC<MarketingVideoProps> = (props) => {
  const {
    scenes,
    brand,
    facts,
    cta,
    music,
    captions,
    safeMargins,
    assetPaths,
  } = props;

  const logoSrc = brand.logoAssetId ? assetPaths[brand.logoAssetId] : undefined;
  const musicSrc =
    music.mode !== 'none' && music.assetId
      ? assetPaths[music.assetId]
      : undefined;

  let cursor = 0;
  return (
    <AbsoluteFill style={{backgroundColor: '#000'}}>
      {scenes.map((scene) => {
        const from = cursor;
        cursor += scene.durationInFrames;
        const mediaSrc = scene.mediaAssetId
          ? assetPaths[scene.mediaAssetId]
          : undefined;
        return (
          <Sequence
            key={scene.id}
            from={from}
            durationInFrames={scene.durationInFrames}
            name={`${scene.kind}:${scene.id}`}
          >
            <SceneView
              scene={scene}
              brand={brand}
              facts={facts}
              logoSrc={logoSrc}
              mediaSrc={mediaSrc}
              cta={cta}
              captionsEnabled={captions}
              safeMarginsEnabled={safeMargins}
            />
          </Sequence>
        );
      })}

      {musicSrc ? <Audio src={staticFile(musicSrc)} volume={1} /> : null}
    </AbsoluteFill>
  );
};
