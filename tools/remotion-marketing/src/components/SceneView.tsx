import React from 'react';
import {
  AbsoluteFill,
  Img,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import {BackgroundGradient} from './BackgroundGradient';
import {SafeArea} from './SafeArea';
import {Caption} from './Caption';
import {FactBadge} from './FactBadge';
import {Logo} from './Logo';
import {CTACard} from './CTACard';
import {fonts} from '../theme';
import type {Brand, Fact, Scene} from '../schema';

/**
 * Renders one storyboard scene. Kind drives the layout; content comes entirely
 * from typed props. The same component covers every scene so styling stays DRY
 * and consistent across a campaign.
 */
export const SceneView: React.FC<{
  scene: Scene;
  brand: Brand;
  facts: Fact[];
  logoSrc?: string;
  mediaSrc?: string;
  cta: {text: string; url?: string};
  captionsEnabled: boolean;
  safeMarginsEnabled: boolean;
}> = ({
  scene,
  brand,
  facts,
  logoSrc,
  mediaSrc,
  cta,
  captionsEnabled,
  safeMarginsEnabled,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const enter = spring({frame, fps, config: {damping: 18, mass: 0.7}});
  const fade = interpolate(frame, [0, 12], [0, 1], {extrapolateRight: 'clamp'});
  const sceneFacts = facts.filter((f) => scene.factIds.includes(f.id));

  return (
    <AbsoluteFill>
      <BackgroundGradient brand={brand} />
      <SafeArea enabled={safeMarginsEnabled}>
        <div
          style={{
            opacity: fade,
            transform: `translateY(${(1 - enter) * 40}px)`,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: 28,
            textAlign: 'center',
            maxWidth: '90%',
          }}
        >
          {scene.kind === 'intro' ? <Logo brand={brand} src={logoSrc} /> : null}

          {scene.kind === 'media' && mediaSrc ? (
            <Img
              src={staticFile(mediaSrc)}
              style={{
                maxHeight: '52vh',
                maxWidth: '82%',
                borderRadius: 18,
                boxShadow: '0 24px 60px rgba(0,0,0,0.35)',
              }}
            />
          ) : null}

          <h1
            style={{
              margin: 0,
              fontFamily: fonts.display,
              fontWeight: 800,
              fontSize: scene.kind === 'intro' ? 88 : 76,
              color: '#fff',
              lineHeight: 1.05,
            }}
          >
            {scene.headline}
          </h1>

          {scene.subhead ? (
            <p
              style={{
                margin: 0,
                fontFamily: fonts.body,
                fontSize: 40,
                color: 'rgba(255,255,255,0.9)',
              }}
            >
              {scene.subhead}
            </p>
          ) : null}

          {sceneFacts.length > 0 ? (
            <div
              style={{
                display: 'flex',
                flexWrap: 'wrap',
                gap: 18,
                justifyContent: 'center',
              }}
            >
              {sceneFacts.map((f) => (
                <FactBadge key={f.id} fact={f} brand={brand} />
              ))}
            </div>
          ) : null}

          {scene.kind === 'outro' ? (
            <CTACard text={cta.text} url={cta.url} brand={brand} />
          ) : null}
        </div>
      </SafeArea>

      {captionsEnabled && scene.caption ? (
        <Caption text={scene.caption} />
      ) : null}
    </AbsoluteFill>
  );
};
